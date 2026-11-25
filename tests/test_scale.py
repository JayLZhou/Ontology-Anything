"""Tables inside documents, and long-corpus behaviour: batching, skipping, chunk size."""

import json
import threading
import time
from pathlib import Path

import pytest

from ontology_anything import approve, build, infer_tables, load_corpus
from ontology_anything.documents import extract_doc_tables, html_to_text, looks_like_records, looks_normative
from ontology_anything.llm import ReplayLLM

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "procurement"

POLICY = """# 采购管理制度

第三条 审批权限见下表。

表1 审批权限

| 金额 | 审批人 |
| --- | --- |
| 1万元以上 | 部门经理 |
| 5万元以上 | 财务总监 |

## 附件一：合格供应商名录

| 编号 | 名称 | 评级 |
|:---|---|:-:|
| S-001 | 联华办公 | A |
| S-002 | 星辰 \\| 网络 | B |
| S-003 | 华达电子 | C |
"""


# ------------------------------------------------ tables inside documents

def test_pipe_tables_are_found_and_named():
    matrix, suppliers = extract_doc_tables("p.md", Path("p.md"), POLICY)
    assert (matrix.table_id, matrix.name, matrix.columns) == ("p.md#table1", "审批权限", ["金额", "审批人"])
    assert (suppliers.name, len(suppliers.rows)) == ("合格供应商名录", 3)
    assert suppliers.rows[1] == {"编号": "S-002", "名称": "星辰 | 网络", "评级": "B"}
    assert looks_like_records(suppliers) and not looks_like_records(matrix)


def test_text_with_pipes_is_not_a_table():
    assert extract_doc_tables("d", Path("d"), "a | b\nc | d\n\n| only a header |\n") == []


@pytest.mark.parametrize("mode, expected", [("auto", ["合格供应商名录"]), ("all", ["审批权限", "合格供应商名录"]), ("off", [])])
def test_only_record_like_tables_become_data(tmp_path, mode, expected):
    (tmp_path / "p.md").write_text(POLICY, encoding="utf-8")
    corpus = load_corpus([tmp_path], doc_tables=mode)
    assert [t.name for t in corpus.tables] == expected
    assert "| 5万元以上 | 财务总监 |" in corpus.documents[0].text  # the rule table stays readable either way
    if mode == "auto":
        assert any("p.md#table1: table kept as text only" in n for n in corpus.notes)
        draft = build(corpus, None)
        supplier = draft.ontology["object_types"]["合格供应商名录"]
        assert supplier["primary_key"] == "编号" and supplier["id_prefix"] == "S-"
        assert len(draft.seed["objects"]["合格供应商名录"]) == 3
        assert draft.items["object_type:合格供应商名录"]["sources"] == [{"table": "p.md#table2"}]
        assert any("p.md#table2" in n for n in draft.notes)


def test_document_tables_link_to_spreadsheets(tmp_path):
    (tmp_path / "policy.md").write_text(
        "# Policy\n\n## Approved suppliers\n\n| supplier_id | name |\n|---|---|\n| S-1 | Acme |\n| S-2 | Bolt |\n",
        encoding="utf-8")
    (tmp_path / "orders.csv").write_text("order_id,supplier_id,amount\nO-1,S-1,10\nO-2,S-2,20\nO-3,S-1,5\n", encoding="utf-8")
    schema = infer_tables(load_corpus([tmp_path]).tables)
    assert set(schema.object_types) == {"Order", "ApprovedSupplier"}
    assert [(lt["from"], lt["to"]) for lt in schema.link_types.values()] == [("Order", "ApprovedSupplier")]
    assert len(schema.links) == 3


def test_html_tables_and_entities():
    text = html_to_text(
        "<html><head><title>t</title><style>p{}</style></head><body><h2>Staff &amp; roles</h2>"
        "<p>Orders over 500<br>need approval.</p>"
        "<table><tr><th>emp_id</th><th>name</th></tr><tr><td>E1</td><td>Ann <b>Lee</b></td></tr>"
        "<tr><td>E2</td><td>Bo</td></tr></table><ul><li>one</li></ul><script>x()</script></body></html>")
    assert text.startswith("## Staff & roles") and "p{}" not in text and "x()" not in text
    assert "| E1 | Ann Lee |" in text
    table, = extract_doc_tables("s.html", Path("s.html"), text)
    assert table.name == "Staff & roles" and looks_like_records(table)


def test_docx_tables_are_read_in_place(tmp_path):
    docx = pytest.importorskip("docx")
    document = docx.Document()
    document.add_heading("Procurement policy", level=1)
    document.add_paragraph("Purchases need approval as follows.")
    grid = document.add_table(rows=3, cols=2)
    for row, cells in zip(grid.rows, [("Amount", "Approver"), ("over 10000", "manager"), ("over 50000", "CFO")]):
        for cell, value in zip(row.cells, cells):
            cell.text = value
    document.add_heading("Suppliers", level=2)
    listing = document.add_table(rows=3, cols=2)
    for row, cells in zip(listing.rows, [("supplier_id", "name"), ("S-1", "Acme"), ("S-2", "Bolt")]):
        for cell, value in zip(row.cells, cells):
            cell.text = value
    document.add_paragraph("Blacklisted suppliers may not be used.")
    document.save(tmp_path / "policy.docx")

    corpus = load_corpus([tmp_path])
    text = corpus.documents[0].text
    assert text.index("# Procurement policy") < text.index("| over 50000 | CFO |") < text.index("## Suppliers")
    assert text.index("| S-2 | Bolt |") < text.index("Blacklisted suppliers")
    assert [t.name for t in corpus.tables] == ["Suppliers"]


# ---------------------------------------------------------- long corpora

class _Probe:
    """Answers nothing useful, but remembers how it was called."""

    def __init__(self, delay=0.0, answers=None):
        self.delay, self.answers = delay, answers or {}
        self.prompts, self.active, self.peak = {}, 0, 0
        self.lock = threading.Lock()

    def complete(self, key, system, user):
        with self.lock:
            self.prompts[key] = user
            self.active += 1
            self.peak = max(self.peak, self.active)
        time.sleep(self.delay)
        with self.lock:
            self.active -= 1
        return json.dumps(self.answers.get(key, {}))


def _long_corpus(tmp_path, paragraphs=12):
    body = "\n\n".join(f"Article {i}. Orders in class {i} must be approved by the manager." for i in range(paragraphs))
    (tmp_path / "long.md").write_text("# Long policy\n\n" + body, encoding="utf-8")
    return load_corpus([tmp_path], window=150)


def test_chunks_are_sent_in_parallel_batches(tmp_path):
    corpus = _long_corpus(tmp_path)
    chunks = len(corpus.documents[0].chunks)
    assert chunks >= 6
    probe = _Probe(delay=0.05)
    build(corpus, probe, workers=4)
    assert len(probe.prompts) == chunks + 1 and 1 < probe.peak <= 4
    serial = _Probe()
    build(corpus, serial, workers=1)
    assert serial.peak == 1 and set(serial.prompts) == set(probe.prompts)


def test_later_batches_see_actions_found_by_earlier_ones(tmp_path):
    corpus = _long_corpus(tmp_path)
    first = {"actions": [{"name": "approve_order", "parameters": {"amount": "number"}}]}
    answers = {"extract:long.md#0": first}
    serial = _Probe(answers=answers)
    build(corpus, serial, workers=1)
    assert "approve_order" not in serial.prompts["extract:long.md#0"]
    assert '"approve_order": ["amount"]' in serial.prompts["extract:long.md#1"]
    batched = _Probe(answers=answers)
    build(corpus, batched, workers=2)
    assert "approve_order" not in batched.prompts["extract:long.md#1"]  # same batch as chunk 0
    assert '"approve_order": ["amount"]' in batched.prompts["extract:long.md#2"]


def test_worker_count_does_not_change_a_replayed_draft():
    corpus = load_corpus([EXAMPLE / "docs", EXAMPLE / "data"])
    drafts = [build(corpus, ReplayLLM(EXAMPLE / "cassette.json"), today="2026-11-24", workers=n).to_dict() for n in (1, 8)]
    assert drafts[0] == drafts[1]
    approve(build(corpus, ReplayLLM(EXAMPLE / "cassette.json"), today="2026-11-24", workers=8), accept_suggestions=True)


def test_errors_in_a_worker_are_not_swallowed(tmp_path):
    class Broken(_Probe):
        def complete(self, key, system, user):
            if key.endswith("#3"):
                raise RuntimeError("boom")
            return super().complete(key, system, user)

    with pytest.raises(RuntimeError, match="boom"):
        build(_long_corpus(tmp_path), Broken(), workers=4)


def test_rule_wording_detector():
    assert looks_normative("单笔采购金额超过10000元的，须经部门经理审批。")
    assert looks_normative("Purchases over 500 require approval.")
    assert looks_normative("供应商华达电子即日起列入黑名单")
    assert not looks_normative("公司成立于1990年，总部位于上海。")
    assert not looks_normative("Our company was founded in 1990 and has three offices.")


def test_skip_irrelevant_chunks(tmp_path):
    (tmp_path / "handbook.md").write_text(
        "# Handbook\n\nOur company was founded in 1990 and has three offices.\n\n"
        "Purchases over 500 must be approved by a manager.\n\nThe canteen is on the second floor.",
        encoding="utf-8")
    corpus = load_corpus([tmp_path], window=60)
    probe = _Probe()
    draft = build(corpus, probe, skip_irrelevant=True)
    sent = [k for k in probe.prompts if k.startswith("extract:")]
    assert len(sent) == 1 and "must be approved" in probe.prompts[sent[0]]
    assert any(n.startswith(f"skipped {len(corpus.documents[0].chunks) - 1} chunk(s)") for n in draft.notes)
    everything = _Probe()
    build(corpus, everything)
    assert len(everything.prompts) == len(corpus.documents[0].chunks) + 1
