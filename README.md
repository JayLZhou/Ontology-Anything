<div align="center">

# Ontology-Anything

**Documents and tables in. Executable ontology out.**
Policies, SOPs, contracts and spreadsheets → object types, links, roles and *rule-checked actions* -- every rule backed by a verbatim quote, every conflict surfaced, nothing live until a person approves it.

[中文](README_CN.md) · MIT · Python ≥ 3.10 · outputs run on [nano-onto](https://github.com/JayLZhou/nano-ontology)

</div>

---

Knowledge-graph builders extract **facts** ("S-003 is a supplier"). Businesses run on **rules** ("purchases over ¥50,000 need CFO approval") scattered across documents that disagree with each other and change over time. Ontology-Anything extracts both -- and refuses to trust the model:

```
 docs/*.md|pdf|docx ─┐                                          ┌─► review.md        (for humans)
                     ├─► 1 tables   → types, keys, links, rows ──┤
 data/*.csv|xlsx ────┘   2 enrich   → labels, roles       (LLM)  ├─► draft.json       (machine-readable)
                         3 extract  → actions, rules, facts (LLM)│
                         4 verify   → quote must exist verbatim  └─► approve ─► ontology.yaml + seed.json
                         5 validate → must compile under nano-onto                     │
                         6 conflict → same rule, different numbers?                    ▼
                                                                     nano-onto serve-mcp  (agents act on it)
```

| Problem with LLM-built ontologies | What Ontology-Anything does |
|---|---|
| Hallucinated rules | Every rule and fact carries a **quote**; quotes not found verbatim in the source are marked UNVERIFIED and left out by default |
| Rules that reference fields that don't exist | Every action and rule is **compiled by nano-onto**; anything that doesn't type-check is INVALID |
| Malformed model output | The answer's *shape* is untrusted too: wrong types, missing keys and non-JSON replies become notes or INVALID items, never a crash |
| Documents contradict each other | **Conflict detection** pairs rules with the same condition and different thresholds, and suggests a fix from document dates |
| "The new policy starts next month" | `schedule` resolution keeps both rules and switches over on the effective date with `today()` guards |
| Tables already contain the schema | Types, primary keys, ID prefixes, enums and **foreign keys → links** are inferred deterministically, no LLM |
| Text that changes records ("S-003 is blacklisted") | Extracted as **facts**, checked against the data (record exists, property exists, value has the right type) and applied to the seed on approval |
| Nobody owns the ontology | Output is a draft + a review report; `approve` is an explicit, logged step |

## Install

```bash
git clone https://github.com/JayLZhou/Ontology-Anything && cd Ontology-Anything
pip install -e .            # also installs nano-onto from its GitHub repository
pip install -e ".[office]"  # optional: .xlsx, .pdf and .docx inputs
```

## Demo (no API key needed)

```bash
ontology-anything demo
```

The demo reads a procurement policy, a later notice that changes one threshold and blacklists a supplier, and two CSV files (all in [`examples/procurement`](examples/procurement)). The LLM answers are **recorded** in `cassette.json` -- including one deliberately hallucinated rule and one that references a non-existent field -- so the run is deterministic:

```
== 1. build from examples/procurement/docs and examples/procurement/data (recorded LLM answers)
loaded 2 documents, 2 tables
  tables: 2 object types, 1 links, 18 records
  enrich: 3 roles, 2 object types
  draft: 3 actions, 8 rules, 1 facts, 1 conflicts

== 2. approve without resolving conflicts (expected to stop)
not approved: unresolved conflicts:
  C1: cfo_over_50k: params.amount > 50000 -> approval:cfo (from docs/采购管理制度.md, effective 2025-01-01);
      cfo_over_30k: params.amount > 30000 -> approval:cfo (from docs/关于调整采购审批权限的通知.md, effective 2026-12-01)
      suggested: schedule

== 3. approve with the suggested resolutions
  C1: scheduled cfo_over_50k@2025-01-01 -> cfo_over_30k@2026-12-01
  left out rule:no_purchase_from_suspended: invalid (... Supplier has no property or link 'suspended')
  left out rule:warn_rating_c: quote not verified (use --include-unverified to keep)
  applied fact:1: Supplier S-003 {'blacklisted': True}
approved: 2 object types, 3 actions, 6 rules

== 4. run the result with nano-onto
  2026-11-24: 40000 元采购 -> pending_approval ['dept_manager']
  2026-12-02: 40000 元采购 -> pending_approval ['cfo', 'dept_manager']
  向 S-003（通知里拉黑的供应商）采购 -> denied: 不得向黑名单供应商采购
```

(Output trimmed. Add `--out demo-out` to keep `review.md`, `ontology.yaml` and `seed.json`.)

## Use it on your own files

```bash
export OA_API_KEY=sk-...            # any OpenAI-compatible endpoint
export OA_BASE_URL=https://api.deepseek.com/v1  OA_MODEL=deepseek-chat   # optional

# 1. build a draft (inputs are files and/or folders)
ontology-anything build ./policies ./exports -o out --name procurement --llm record:out/cassette.json

# 2. read out/review.md, then approve
ontology-anything approve out --resolve C1=keep:cfo_over_30k --drop rule:some_rule

# 3. hand the result to nano-onto
nano-onto init --schema out/ontology.yaml --db out/nano_onto.db --seed out/seed.json
nano-onto serve-mcp --schema out/ontology.yaml --db out/nano_onto.db --actor alice --role employee
```

**`build`**

| Option | Meaning |
|---|---|
| `--llm openai` (default) | call the model at `OA_BASE_URL` (default OpenAI) with `OA_MODEL` (default `gpt-4o-mini`) |
| `--llm record:<file>` | call the model and save every answer to a cassette |
| `--llm replay:<file>` | re-run from a cassette without calling anything -- for tests, CI and demos |
| `--llm none` | tables only: types, keys and links, no actions or rules |
| `--parser docling` | route PDF/DOCX/PPTX through [Docling](https://github.com/docling-project/docling) for layout-aware parsing (`pip install ".[docling]"`). Built-ins cover .md/.txt/.csv/.tsv/.xlsx/.pdf/.docx/.html |
| `--today YYYY-MM-DD` | the date used to decide whether a newer rule is already in force (default: today) |

**`approve`**

| Option | Meaning |
|---|---|
| `--resolve C1=keep:<rule id>` | keep one rule of a conflict, remove the others |
| `--resolve C1=schedule` | keep all of them, each guarded by its effective date |
| `--resolve C1=suggested` / `--accept-suggestions` | take the suggestion for one conflict / for all |
| `--drop rule:<id>` (also `action:`, `fact:`, `role:`, `object_type:`) | leave an item out |
| `--include-unverified` | keep items whose quotes could not be matched (e.g. OCR noise) -- read them first |

Approval always leaves out INVALID items, refuses to proceed while a conflict is unresolved, and checks that the final ontology compiles. A conflict whose other side is being left out anyway needs no decision. Everything it did is written to `out/approval.log`.

**Files in the output folder**

| File | Written by | What it is |
|---|---|---|
| `review.md` | `build` | the report a person reads: every item with status, source quote and errors |
| `draft.json` | `build` | the full draft, the input to `approve` |
| `ontology.draft.yaml` | `build` | the unapproved ontology, for diffing |
| `ontology.yaml`, `seed.json` | `approve` | what nano-onto loads |
| `approval.log` | `approve` | what was kept, dropped, scheduled and applied |

## Python API

```python
from ontology_anything import load_corpus, build, approve, render_review
from ontology_anything.llm import OpenAICompatLLM

draft = build(load_corpus(["./policies", "./exports"]), OpenAICompatLLM(), name="procurement")
print(render_review(draft))
ontology, seed, log = approve(draft, accept_suggestions=True)
```

Any object with `complete(key, system, user) -> str` works as the model, so you can plug in your own client.

## Limits

- Every chunk of every document is sent to the model; long corpora cost accordingly (see roadmap).
- Conflict detection covers single-comparison thresholds (`params.amount > 50000`) with the same outcome. Overlapping compound conditions are not detected.
- Dates are recognised in `YYYY-MM-DD` and `2026年12月1日` forms; "effective from" detection knows Chinese (`自…起`) and English (`effective from …`).
- Quote verification ignores whitespace and full-/half-width differences only. A paraphrased quote is UNVERIFIED by design.

## Roadmap

- [ ] Long documents: retrieval of relevant chunks per action instead of every chunk
- [ ] Rule conflicts beyond thresholds (overlapping conditions, deny vs. approve), via an SMT check
- [ ] Incremental rebuilds: diff a new document against an approved ontology, propose a patch
- [ ] Ontology evolution from agent traces (repeated denials / manual overrides → rule review)
- [ ] Web review UI (accept / edit / reject per item, side-by-side with the source)
- [ ] Benchmark: rule extraction accuracy on public policy corpora

## Tests

```bash
pip install -e ".[dev]" && pytest
```

The suite needs no network or API key: model calls are replayed from the cassette or served by a local stub.

MIT License.
