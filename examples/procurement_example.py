"""End-to-end example: policies + spreadsheets -> approved ontology -> nano-onto.

Runs without an API key: the model's answers are replayed from
``examples/procurement/cassette.json``. Pass ``--live`` to call a real model
instead (needs OA_API_KEY, see ``.env.example``).

    python examples/procurement_example.py
    python examples/procurement_example.py --live --out my-out
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import yaml
from nano_onto import Actor, Engine, Ontology, Store

from ontology_anything import ReviewError, approve, build, load_corpus, render_review
from ontology_anything.llm import OpenAICompatLLM, ReplayLLM

HERE = Path(__file__).resolve().parent / "procurement"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--live", action="store_true", help="call a real model instead of replaying")
    parser.add_argument("--out", default="procurement-out", help="output folder")
    args = parser.parse_args()

    # 1. load: folders may mix documents (.md/.pdf/.docx/.html) and tables (.csv/.xlsx)
    corpus = load_corpus([HERE / "docs", HERE / "data"])
    print(f"loaded {len(corpus.documents)} documents and {len(corpus.tables)} tables")

    # 2. build a draft; nothing in it is trusted yet
    llm = OpenAICompatLLM() if args.live else ReplayLLM(HERE / "cassette.json")
    draft = build(corpus, llm, name="procurement", today="2026-11-24", progress=print)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "review.md").write_text(render_review(draft), encoding="utf-8")
    print(f"\nreview report: {out / 'review.md'}")
    for item_id, item in draft.items.items():
        if item["status"] != "proposed":
            print(f"  {item['status'].upper():10} {item_id}: {item['errors'][0]}")

    # 3. approval refuses to guess: conflicts need a decision
    try:
        approve(draft)
    except ReviewError as exc:
        print(f"\napprove() without decisions stops:\n{exc}")

    ontology, seed, log = approve(draft, accept_suggestions=True)
    print("\napproved with the suggested resolutions:")
    for line in log:
        print(f"  {line}")
    (out / "ontology.yaml").write_text(yaml.safe_dump(ontology, allow_unicode=True, sort_keys=False), encoding="utf-8")
    (out / "seed.json").write_text(json.dumps(seed, ensure_ascii=False, indent=2), encoding="utf-8")

    # 4. the result is executable: nano-onto enforces the rules outside the model
    engine = Engine(Ontology.from_dict(ontology), Store())
    engine.load_seed(seed)
    alice = Actor("alice", "employee")
    order = {"title": "服务器", "amount": 40000, "supplier": "S-002"}
    print("\na 40,000 purchase by an employee:")
    for today in ("2026-11-24", "2026-12-02"):  # the notice lowers the CFO threshold on 2026-12-01
        os.environ["NANO_ONTO_TODAY"] = today
        result = engine.explain("submit_purchase_order", order, alice)
        print(f"  on {today}: {result.status}, approvals needed: {result.required_approvals}")
    os.environ.pop("NANO_ONTO_TODAY", None)
    small = {"title": "电容", "amount": 3000, "supplier": "S-003"}  # the notice blacklists S-003
    result = engine.explain("submit_purchase_order", small, alice)
    print(f"a 3,000 purchase from S-003: {'; '.join(result.messages) or result.status}")


if __name__ == "__main__":
    main()
