import json
from pathlib import Path

import pytest

from nano_onto import Actor, Engine, Ontology, Store
from ontology_anything import approve, build, load_corpus, quote_found, render_review
from ontology_anything.cli import main
from ontology_anything.documents import Table, chunk_text, normalize_date
from ontology_anything.llm import ReplayLLM, parse_json
from ontology_anything.review import ReviewError
from ontology_anything.tables import infer_column_type, infer_tables, type_name_for

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "procurement"


@pytest.fixture(scope="module")
def draft():
    corpus = load_corpus([EXAMPLE / "docs", EXAMPLE / "data"])
    return build(corpus, ReplayLLM(EXAMPLE / "cassette.json"), name="procurement", today="2026-11-24")


# ------------------------------------------------------------------ tables

def test_type_names():
    assert type_name_for("purchase_orders") == "PurchaseOrder"
    assert type_name_for("companies") == "Company"
    assert type_name_for("address") == "Address"
    assert type_name_for("供应商") == "供应商"


@pytest.mark.parametrize("values, expected", [
    (["1", "2", "30"], "number"),
    (["001", "002"], "string"),
    (["1.5", "2"], "number"),
    (["true", "false", ""], "boolean"),
    (["是", "否"], "boolean"),
    (["2026-01-01", "2026-02-03"], "date"),
    (["a", "1"], "string"),
])
def test_column_types(values, expected):
    assert infer_column_type(values) == expected


def test_count_columns_are_integers():
    assert infer_column_type(["1", "2"], "quantity") == "integer"


def test_foreign_keys_become_links():
    suppliers = Table("s.csv", Path("s.csv"), "suppliers", ["supplier_id", "name"],
                      [{"supplier_id": f"S-{i}", "name": f"n{i}"} for i in range(3)])
    orders = Table("o.csv", Path("o.csv"), "orders", ["order_id", "supplier_id", "qty"],
                   [{"order_id": f"O-{i}", "supplier_id": f"S-{i % 2}", "qty": "5"} for i in range(4)])
    ts = infer_tables([suppliers, orders])
    assert ts.link_types == {"supplier": {"from": "Order", "to": "Supplier", "cardinality": "many_to_one"}}
    assert "supplier_id" not in ts.object_types["Order"]["properties"]
    assert {"type": "supplier", "from": "O-3", "to": "S-1"} in ts.links
    assert ts.object_types["Order"]["id_prefix"] == "O-"


def test_numeric_value_columns_are_not_mistaken_for_keys():
    a = Table("a", Path("a"), "items", ["item_id", "price"], [{"item_id": "1", "price": "2"}, {"item_id": "2", "price": "1"}])
    b = Table("b", Path("b"), "boxes", ["box_id", "size"], [{"box_id": "1", "size": "1"}, {"box_id": "2", "size": "2"}])
    assert infer_tables([a, b]).link_types == {}


# -------------------------------------------------------------- documents

def test_dates_and_chunks():
    assert normalize_date("自2026年12月1日起") == "2026-12-01"
    assert normalize_date("2025-1-3") == "2025-01-03"
    text = "\n\n".join(["段落" * 50] * 10)
    chunks = chunk_text("d", text, window=300)
    assert len(chunks) > 1
    assert all(text[c.start:c.end] == c.text for c in chunks)


def test_quote_matching_ignores_whitespace_and_width():
    source = "单笔采购金额超过 10000 元的，须经部门经理审批。"
    assert quote_found("单笔采购金额超过10000元的,须经部门经理审批。", source)
    assert not quote_found("单笔采购金额超过20000元的", source)
    assert not quote_found("", source)


def test_parse_json_tolerates_fences():
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json('Sure! {"a": 2} hope this helps') == {"a": 2}


# ---------------------------------------------------------------- pipeline

def test_draft_contents(draft):
    assert set(draft.ontology["object_types"]) == {"Supplier", "PurchaseOrder"}
    assert draft.ontology["object_types"]["Supplier"]["label"] == "供应商"
    assert set(draft.ontology["roles"]) == {"employee", "dept_manager", "cfo"}
    assert set(draft.ontology["action_types"]) == {"submit_purchase_order", "cancel_purchase_order", "blacklist_supplier"}


def test_hallucinated_quote_is_flagged(draft):
    item = draft.items["rule:warn_rating_c"]
    assert item["status"] == "unverified"


def test_rule_with_unknown_property_is_invalid(draft):
    item = draft.items["rule:no_purchase_from_suspended"]
    assert item["status"] == "invalid"
    assert "suspended" in item["errors"][0]


def test_threshold_conflict_detected_with_schedule_suggestion(draft):
    assert len(draft.conflicts) == 1
    c = draft.conflicts[0]
    assert c["kind"] == "threshold_mismatch"
    assert c["rules"] == ["cfo_over_50k", "cfo_over_30k"]
    assert c["suggestion"] == "schedule"


def test_conflict_suggests_keep_newest_once_in_force():
    corpus = load_corpus([EXAMPLE / "docs", EXAMPLE / "data"])
    later = build(corpus, ReplayLLM(EXAMPLE / "cassette.json"), today="2027-01-01")
    assert later.conflicts[0]["suggestion"] == "keep:cfo_over_30k"


def test_fact_records_before_value(draft):
    assert draft.facts[0]["before"] == {"blacklisted": False}


def test_review_report_mentions_everything(draft):
    text = render_review(draft)
    for needle in ("C1", "UNVERIFIED", "INVALID", "S-003", "自2026年12月1日起"):
        assert needle in text


# ------------------------------------------------------------------ review

def test_approve_requires_conflict_resolution(draft):
    with pytest.raises(ReviewError, match="unresolved conflicts"):
        approve(draft)


def test_approve_keep_one(draft):
    onto, seed, log = approve(draft, resolutions={"C1": "keep:cfo_over_30k"})
    rules = {r["id"] for r in onto["action_types"]["submit_purchase_order"]["rules"]}
    assert "cfo_over_30k" in rules and "cfo_over_50k" not in rules
    assert "warn_rating_c" not in rules and "no_purchase_from_suspended" not in rules
    s003 = next(s for s in seed["objects"]["Supplier"] if s["supplier_id"] == "S-003")
    assert s003["blacklisted"] is True


def test_approve_include_unverified_and_drop(draft):
    onto, seed, _ = approve(draft, accept_suggestions=True, include_unverified=True, drop=["fact:1"])
    rules = {r["id"] for r in onto["action_types"]["submit_purchase_order"]["rules"]}
    assert "warn_rating_c" in rules
    s003 = next(s for s in seed["objects"]["Supplier"] if s["supplier_id"] == "S-003")
    assert s003["blacklisted"] is False


def test_bad_resolution(draft):
    with pytest.raises(ReviewError):
        approve(draft, resolutions={"C1": "keep:nope"})
    with pytest.raises(ReviewError):
        approve(draft, resolutions={"C9": "schedule"})


def test_scheduled_rules_run_in_nano_onto(draft, monkeypatch):
    onto, seed, _ = approve(draft, accept_suggestions=True)
    engine = Engine(Ontology.from_dict(onto), Store())
    engine.load_seed(seed)
    alice = Actor("alice", "employee")
    params = {"title": "服务器", "amount": 40000, "supplier": "S-002"}
    monkeypatch.setenv("NANO_ONTO_TODAY", "2026-11-30")
    assert engine.explain("submit_purchase_order", params, alice).required_approvals == ["dept_manager"]
    monkeypatch.setenv("NANO_ONTO_TODAY", "2026-12-01")
    assert engine.explain("submit_purchase_order", params, alice).required_approvals == ["cfo", "dept_manager"]
    res = engine.execute("submit_purchase_order", {**params, "supplier": "S-003"}, alice)
    assert res.status == "denied"
    created = engine.execute("submit_purchase_order", {"title": "笔", "amount": 99.5, "supplier": "S-001"}, alice)
    assert created.status == "executed"  # decimal amount works: column inferred as number


def test_cli_build_and_approve(tmp_path, capsys):
    out = tmp_path / "out"
    assert main(["build", str(EXAMPLE / "docs"), str(EXAMPLE / "data"), "-o", str(out),
                 "--llm", f"replay:{EXAMPLE / 'cassette.json'}", "--today", "2026-11-24"]) == 0
    assert (out / "review.md").exists() and (out / "draft.json").exists()
    assert main(["approve", str(out)]) == 2
    assert main(["approve", str(out), "--resolve", "C1=schedule"]) == 0
    Ontology.load(out / "ontology.yaml")
    assert json.loads((out / "seed.json").read_text(encoding="utf-8"))["links"]


def test_missing_cassette_key_is_reported(tmp_path):
    cassette = tmp_path / "c.json"
    cassette.write_text("{}", encoding="utf-8")
    corpus = load_corpus([EXAMPLE / "docs", EXAMPLE / "data"])
    d = build(corpus, ReplayLLM(cassette))
    assert any("no answer for 'enrich'" in n for n in d.notes)
    assert d.ontology["action_types"] == {}


def test_xlsx_tables(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["emp_id", "name", "dept"])
    ws.append(["E1", "张三", "研发"])
    ws.append(["E2", "李四", "研发"])
    path = tmp_path / "employees.xlsx"
    wb.save(path)
    corpus = load_corpus([path])
    ts = infer_tables(corpus.tables)
    assert "Employee" in ts.object_types
    assert ts.object_types["Employee"]["primary_key"] == "emp_id"


# ------------------------------------------------------------ robustness

class _FakeLLM:
    """Recorded answers with some keys overridden."""

    def __init__(self, **patch):
        self.answers = {**json.loads((EXAMPLE / "cassette.json").read_text(encoding="utf-8")), **patch}

    def complete(self, key, system, user):
        answer = self.answers[key]
        return answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False)


POLICY = "extract:docs/采购管理制度.md#0"


def _build(**patch):
    corpus = load_corpus([EXAMPLE / "docs", EXAMPLE / "data"])
    return build(corpus, _FakeLLM(**patch), today="2026-11-24")


def test_document_ids_do_not_depend_on_how_paths_are_spelled(monkeypatch):
    monkeypatch.chdir(EXAMPLE)
    relative = load_corpus(["docs", "data"])
    absolute = load_corpus([EXAMPLE / "docs", EXAMPLE / "data"])
    assert [d.doc_id for d in relative.documents] == [d.doc_id for d in absolute.documents]
    assert all(d.doc_id.startswith("docs/") for d in relative.documents)
    with pytest.raises(FileNotFoundError):
        load_corpus(["no-such-folder"])


@pytest.mark.parametrize("patch", [
    {"enrich": {"object_types": [], "new_object_types": {"Dept": {"properties": ["a"]}, "X y": {}}, "roles": "x"}},
    {"enrich": "[1, 2]", POLICY: "sorry, I cannot help with that"},
    {POLICY: {"actions": {"a": 1}, "rules": {"r": 1}, "facts": {"f": 1}, "roles": [None, {"id": None}]}},
    {POLICY: {"actions": [None, {"name": "do x", "parameters": ["a"], "effects": "none", "allowed_roles": "cfo"}]}},
    {POLICY: {"actions": [{"name": "do_x", "parameters": {"a": "number", "b": {"type": "object_ref"}},
                           "effects": [{"create": "Nope"}, 5, {"update": 3, "set": []}]}]}},
    {POLICY: {"actions": [{"name": "do_x", "parameters": {"a": {"type": "number"}}}],
              "rules": [{"id": 5, "action": "do_x", "when": True, "then": ["deny"], "effective_date": 20260101},
                        {"id": "r2", "action": "do_x", "when": "params.a >", "then": {"require_approval": 7}},
                        {"id": "r3", "action": ["do_x"], "when": "params.a > 1"},
                        {"id": "r4", "action": "do_x", "when": "params.a > 1", "then": ["deny"]},
                        {"id": "r5", "action": "do_x", "when": "__import__('os').getcwd()", "then": "explode"}]}},
    {POLICY: {"facts": [None, {"type": ["Supplier"], "id": 1, "set": {"x": 1}}, {"type": "Supplier", "id": "S-001"}]}},
])
def test_malformed_model_output_never_crashes(patch):
    d = _build(**patch)
    render_review(d)
    onto, seed, _ = approve(d, accept_suggestions=True, include_unverified=True)
    Engine(Ontology.from_dict(onto), Store()).load_seed(seed)  # whatever survives still runs
    assert not {"do_x", "do x"} & {a for a, spec in onto["action_types"].items() if spec.get("effects")}
    for rid in ("r2", "r4", "r5"):
        if f"rule:{rid}" in d.items:
            assert d.items[f"rule:{rid}"]["status"] == "invalid"


def test_roles_used_but_never_described_are_declared():
    d = _build(enrich={})
    assert set(d.ontology["roles"]) == {"employee", "dept_manager", "cfo"}
    assert d.items["action:submit_purchase_order"]["status"] == "proposed"
    assert any("role cfo" in n for n in d.notes)


def test_facts_are_type_checked():
    d = _build(**{POLICY: {"facts": [
        {"type": "Supplier", "id": "S-001", "set": {"blacklisted": "maybe"}, "quote": "不得向列入黑名单的供应商采购。"},
        {"type": "Supplier", "id": "S-001", "set": {"supplier_id": "S-002"}, "quote": "不得向列入黑名单的供应商采购。"},
        {"type": "Supplier", "id": "S-999", "set": {"blacklisted": True}, "quote": "不得向列入黑名单的供应商采购。"},
        {"type": "Supplier", "id": "S-001", "set": {"rating": "Z"}, "quote": "不得向列入黑名单的供应商采购。"},
    ]}})
    mine = [f for f in d.facts if f["source"]["doc"] == "docs/采购管理制度.md"]
    errors = [d.items[f["id_"]]["errors"] for f in mine]
    assert "not boolean" in errors[0][0] and "primary key" in errors[1][0] and "not in the data" in errors[2][0]
    assert all(d.items[f["id_"]]["status"] == "invalid" for f in mine[:3])
    has_enum = bool(d.ontology["object_types"]["Supplier"]["properties"]["rating"].get("enum"))
    assert (d.items[mine[3]["id_"]]["status"] == "invalid") == has_enum


def test_conflict_is_moot_when_one_side_is_left_out(draft):
    onto, _, log = approve(draft, drop=["rule:cfo_over_30k"])  # no --resolve needed any more
    rules = {r["id"]: r for r in onto["action_types"]["submit_purchase_order"]["rules"]}
    assert rules["cfo_over_50k"]["when"] == "params.amount > 50000"  # not left with an expiry date
    assert any(line.startswith("C1: nothing to resolve") for line in log)
    with pytest.raises(ReviewError, match="unknown item id"):
        approve(draft, accept_suggestions=True, drop=["rule:typo"])


def test_cli_errors_are_messages_not_tracebacks(tmp_path, capsys):
    assert main(["build", str(tmp_path / "missing"), "-o", str(tmp_path / "o")]) == 1
    assert main(["build", str(EXAMPLE / "docs"), "-o", str(tmp_path / "o"), "--llm", f"replay:{tmp_path / 'no.json'}"]) == 1
    assert main(["approve", str(tmp_path / "empty")]) == 1
    err = capsys.readouterr().err
    assert "no such file or folder" in err and "cannot read cassette" in err and "no draft.json" in err


def test_demo_runs_and_keeps_existing_files(tmp_path, capsys):
    out = tmp_path / "out"
    out.mkdir()
    (out / "mine.txt").write_text("keep me", encoding="utf-8")
    assert main(["demo", "--out", str(out)]) == 0
    assert (out / "mine.txt").read_text(encoding="utf-8") == "keep me"
    text = capsys.readouterr().out
    assert "['cfo', 'dept_manager']" in text and "denied" in text


def test_html_documents(tmp_path):
    (tmp_path / "p.html").write_text(
        "<html><style>p{}</style><body><h1>Policy</h1><p>Purchases over 500 need approval.</p></body></html>",
        encoding="utf-8")
    doc = load_corpus([tmp_path]).documents[0]
    assert "Purchases over 500 need approval." in doc.text and "p{}" not in doc.text


# ------------------------------------------------------- examples and config

@pytest.mark.parametrize("script, expect", [
    ("procurement_example.py", "['cfo', 'dept_manager']"),
    ("tables_only_example.py", "link supplier: PurchaseOrder -> Supplier"),
    ("custom_llm_example.py", "both drafts are identical"),
])
def test_example_scripts_run(script, expect, tmp_path):
    import subprocess
    import sys

    done = subprocess.run([sys.executable, str(EXAMPLE.parent / script)], cwd=tmp_path,
                          capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert done.returncode == 0, done.stderr
    assert expect in done.stdout


def test_dotenv_fills_in_but_never_overrides(tmp_path, monkeypatch):
    from ontology_anything.cli import load_dotenv

    env = tmp_path / ".env"
    env.write_text("# comment\nOA_MODEL=from-file\nexport OA_BASE_URL=\"http://x/v1\"  \n"
                   "OA_API_KEY=abc # trailing comment\nnot a pair\n", encoding="utf-8")
    monkeypatch.setenv("OA_MODEL", "from-shell")
    monkeypatch.delenv("OA_BASE_URL", raising=False)
    monkeypatch.delenv("OA_API_KEY", raising=False)
    import os

    try:
        assert sorted(load_dotenv(env)) == ["OA_API_KEY", "OA_BASE_URL"]
        assert os.environ["OA_MODEL"] == "from-shell"
        assert os.environ["OA_BASE_URL"] == "http://x/v1" and os.environ["OA_API_KEY"] == "abc"
    finally:
        os.environ.pop("OA_BASE_URL", None)
        os.environ.pop("OA_API_KEY", None)
    assert load_dotenv(tmp_path / "missing") == []


def test_python_dash_m_entry_point():
    import subprocess
    import sys

    done = subprocess.run([sys.executable, "-m", "ontology_anything", "--help"], capture_output=True, text=True)
    assert done.returncode == 0 and "build" in done.stdout
