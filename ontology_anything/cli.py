"""``ontology-anything`` command line.

    ontology-anything build  <files or folders> -o out/ --llm openai
    ontology-anything approve out/ --accept-suggestions
    ontology-anything demo
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

import yaml

from . import llm as llm_mod
from .documents import load_corpus
from .pipeline import Draft, build, save
from .review import ReviewError, approve, render_review

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "procurement"  # source checkout


def _write_outputs(draft: Draft, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    save(draft, out / "draft.json")
    (out / "ontology.draft.yaml").write_text(
        yaml.safe_dump(draft.ontology, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    (out / "review.md").write_text(render_review(draft), encoding="utf-8")


def cmd_build(args) -> int:
    try:
        corpus = load_corpus(args.inputs, parser=args.parser)
    except FileNotFoundError as exc:
        print(f"error: no such file or folder: {exc}", file=sys.stderr)
        return 1
    if not corpus.documents and not corpus.tables:
        print("error: no supported documents or tables found in the inputs", file=sys.stderr)
        for line in corpus.skipped:
            print(f"  skipped {line}", file=sys.stderr)
        return 1
    print(f"loaded {len(corpus.documents)} documents, {len(corpus.tables)} tables"
          + (f", skipped {len(corpus.skipped)}" if corpus.skipped else ""))
    model = None if args.llm == "none" else llm_mod.from_spec(args.llm, args.model)
    draft = build(corpus, model, name=args.name, progress=lambda m: print(f"  {m}"), today=args.today)
    out = Path(args.out)
    _write_outputs(draft, out)
    print(f"wrote {out / 'review.md'}, {out / 'ontology.draft.yaml'}, {out / 'draft.json'}")
    if draft.conflicts:
        print(f"{len(draft.conflicts)} conflict(s) need a decision -- see review.md")
    return 0


def _parse_resolutions(pairs: list[str] | None) -> dict[str, str]:
    out = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise SystemExit(f"--resolve expects ID=choice, got {pair!r}")
        key, value = pair.split("=", 1)
        out[key.strip()] = value.strip()
    return out


def cmd_approve(args) -> int:
    out = Path(args.out)
    if not (out / "draft.json").exists():
        print(f"error: no draft.json in {out}; run `ontology-anything build ... -o {out}` first", file=sys.stderr)
        return 1
    draft = Draft.from_dict(json.loads((out / "draft.json").read_text(encoding="utf-8")))
    try:
        onto, seed, log = approve(
            draft,
            resolutions=_parse_resolutions(args.resolve),
            accept_suggestions=args.accept_suggestions,
            drop=args.drop,
            include_unverified=args.include_unverified,
        )
    except ReviewError as exc:
        print(f"not approved: {exc}", file=sys.stderr)
        return 2
    (out / "ontology.yaml").write_text(yaml.safe_dump(onto, allow_unicode=True, sort_keys=False), encoding="utf-8")
    (out / "seed.json").write_text(json.dumps(seed, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "approval.log").write_text("\n".join(log) + "\n", encoding="utf-8")
    for line in log:
        print(f"  {line}")
    rules = sum(len(a["rules"]) for a in onto["action_types"].values())
    print(f"approved: {len(onto['object_types'])} object types, {len(onto['action_types'])} actions, {rules} rules")
    print(f"next: nano-onto init --schema {out / 'ontology.yaml'} --db {out / 'nano_onto.db'} --seed {out / 'seed.json'}")
    return 0


def cmd_demo(args) -> int:
    from nano_onto import Actor, Engine, Ontology, Store

    if not (EXAMPLE / "cassette.json").exists():
        print(f"error: the demo needs the examples/ folder of a source checkout (looked in {EXAMPLE});\n"
              "       git clone the repository and `pip install -e .` there", file=sys.stderr)
        return 1
    work = Path(args.out or tempfile.mkdtemp(prefix="ontology-anything-demo-"))
    print(f"== 1. build from {EXAMPLE / 'docs'} and {EXAMPLE / 'data'} (recorded LLM answers)")
    rc = cmd_build(argparse.Namespace(
        inputs=[str(EXAMPLE / "docs"), str(EXAMPLE / "data")], out=str(work), parser="builtin",
        llm=f"replay:{EXAMPLE / 'cassette.json'}", model=None, name="procurement", today=args.today,
    ))
    if rc:
        return rc
    print("\n== 2. approve without resolving conflicts (expected to stop)")
    sys.stdout.flush()
    cmd_approve(argparse.Namespace(out=str(work), resolve=None, accept_suggestions=False, drop=None,
                                   include_unverified=False))
    sys.stderr.flush()
    print("\n== 3. approve with the suggested resolutions")
    rc = cmd_approve(argparse.Namespace(out=str(work), resolve=None, accept_suggestions=True, drop=None,
                                        include_unverified=False))
    if rc:
        return rc
    print("\n== 4. run the result with nano-onto")
    engine = Engine(Ontology.load(work / "ontology.yaml"), Store())
    engine.load_seed(json.loads((work / "seed.json").read_text(encoding="utf-8")))
    alice = Actor("alice", "employee")
    for today in ("2026-11-24", "2026-12-02"):
        os.environ["NANO_ONTO_TODAY"] = today
        res = engine.explain("submit_purchase_order",
                             {"title": "服务器", "amount": 40000, "supplier": "S-002"}, alice)
        print(f"  {today}: 40000 元采购 -> {res.status} {res.required_approvals}")
    os.environ.pop("NANO_ONTO_TODAY", None)
    res = engine.explain("submit_purchase_order", {"title": "电容", "amount": 3000, "supplier": "S-003"}, alice)
    print(f"  向 S-003（通知里拉黑的供应商）采购 -> {'; '.join(res.messages) or res.status}")
    print(f"\noutputs in {work}: review.md, ontology.yaml, seed.json")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ontology-anything", description="Documents and tables -> executable ontology")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("build", help="build a draft ontology from files and folders")
    p.add_argument("inputs", nargs="+")
    p.add_argument("-o", "--out", default="onto-out")
    p.add_argument("--llm", default=os.environ.get("OA_LLM", "openai"),
                   help="openai | replay:<cassette.json> | record:<cassette.json> | none")
    p.add_argument("--model", help="model name (default $OA_MODEL)")
    p.add_argument("--parser", default="builtin", choices=["builtin", "docling"])
    p.add_argument("--name", default="ontology")
    p.add_argument("--today", help="YYYY-MM-DD used to judge effective dates (default: today)")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("approve", help="turn a reviewed draft into ontology.yaml + seed.json")
    p.add_argument("out")
    p.add_argument("--resolve", action="append", help="CONFLICT=keep:<rule id> | schedule | suggested")
    p.add_argument("--accept-suggestions", action="store_true")
    p.add_argument("--drop", action="append", help="item id to leave out, e.g. rule:foo or fact:2")
    p.add_argument("--include-unverified", action="store_true")
    p.set_defaults(func=cmd_approve)

    p = sub.add_parser("demo", help="run the bundled procurement example end to end")
    p.add_argument("--out", help="output folder (default: a temp folder)")
    p.add_argument("--today", default="2026-11-24")
    p.set_defaults(func=cmd_demo)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except llm_mod.LLMError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
