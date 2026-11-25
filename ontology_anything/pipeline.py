"""Corpus -> draft ontology.

Stages
  1. tables   -> object types, link types and instances (deterministic)
  2. enrich   -> labels, extra object types and roles from the text (LLM)
  3. extract  -> actions, rules, facts per document chunk (LLM)
  4. verify   -> every quote must appear verbatim in its source document
  5. validate -> every action and rule must compile under nano-onto
  6. conflict -> rules that disagree (e.g. different thresholds) are paired up

Nothing produced here is trusted yet: the result is a *draft* that a person
reviews and approves (see :mod:`ontology_anything.review`).
"""

from __future__ import annotations

import ast
import copy
import json
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from nano_onto import Ontology, SchemaError
from nano_onto.schema import PRIMITIVE_TYPES

from . import prompts
from .documents import Corpus, looks_normative, normalize_date
from .llm import LLM, LLMError, parse_json
from .tables import infer_tables

Progress = Callable[[str], None]


@dataclass
class Draft:
    name: str
    ontology: dict[str, Any]
    seed: dict[str, Any]
    items: dict[str, dict[str, Any]] = field(default_factory=dict)
    facts: list[dict[str, Any]] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    documents: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Draft":
        return cls(**data)


# ---------------------------------------------------------------- helpers


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    return re.sub(r"\s+", "", text)


def quote_found(quote: str | None, source: str) -> bool:
    if not quote or len(_norm(quote)) < 4:
        return False
    return _norm(quote) in _norm(source)


def _snake(text: str) -> str:
    text = re.sub(r"[^0-9A-Za-z_一-鿿]+", "_", str(text)).strip("_")
    return text.lower() or "item"


def _dict(value: Any) -> dict[str, Any]:
    """Model output is untrusted in shape as well as content."""
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _compile_errors(data: dict[str, Any]) -> list[str]:
    """Errors nano-onto reports for ``data``; an empty list means it compiles."""
    try:
        Ontology.from_dict(data)
    except SchemaError as exc:
        return list(exc.errors)
    except Exception as exc:  # malformed structure the schema parser did not expect
        return [f"malformed definition ({type(exc).__name__}: {exc})"]
    return []


def _ask(llm: LLM, key: str, system: str, user: str) -> tuple[dict[str, Any], str | None]:
    """The model's answer as a dict, plus a note when it could not be used."""
    try:
        data = parse_json(llm.complete(key, system, user))
    except LLMError as exc:
        return {}, f"LLM call {key} failed: {exc}"
    if not isinstance(data, dict):
        return {}, f"LLM call {key} returned {type(data).__name__}, expected an object"
    return data, None


# --------------------------------------------------------------- pipeline


def build(
    corpus: Corpus,
    llm: LLM | None,
    name: str = "ontology",
    progress: Progress | None = None,
    today: str | None = None,
    workers: int = 1,
    skip_irrelevant: bool = False,
) -> Draft:
    """Build a draft ontology.

    ``workers`` > 1 sends that many extraction calls at a time. Chunks in the
    same batch do not see the actions the others find, only those of earlier
    batches; with ``workers=1`` every chunk sees everything before it.
    ``skip_irrelevant`` does not send chunks without any rule-like wording
    (see :func:`ontology_anything.documents.looks_normative`); they are listed
    in the notes.
    """
    say = progress or (lambda _msg: None)
    workers = max(1, int(workers))
    notes: list[str] = list(corpus.skipped) + list(corpus.notes)
    items: dict[str, dict[str, Any]] = {}

    # 1. tables ---------------------------------------------------------
    ts = infer_tables(corpus.tables)
    notes.extend(ts.notes)
    object_types = copy.deepcopy(ts.object_types)
    link_types = copy.deepcopy(ts.link_types)
    for type_name, table_id in ts.provenance.items():
        items[f"object_type:{type_name}"] = {
            "kind": "object_type", "status": "proposed", "sources": [{"table": table_id}],
        }
    say(f"tables: {len(object_types)} object types, {len(link_types)} links, "
        f"{sum(len(v) for v in ts.objects.values())} records")

    roles: dict[str, dict[str, Any]] = {}
    actions: dict[str, dict[str, Any]] = {}
    rules: list[dict[str, Any]] = []
    facts: list[dict[str, Any]] = []
    documents = {d.doc_id: {"meta": d.meta, "chunks": len(d.chunks)} for d in corpus.documents}
    doc_text = {d.doc_id: d.text for d in corpus.documents}

    if llm is not None and corpus.documents:
        # 2. enrich -----------------------------------------------------
        excerpts = [(d.doc_id, d.text[:2000]) for d in corpus.documents[:12]]
        data, problem = _ask(llm, "enrich", *prompts.enrich_prompt(object_types, link_types, excerpts))
        if problem:
            notes.append(problem)
        _apply_enrichment(data, object_types, roles, items, notes)
        say(f"enrich: {len(roles)} roles, {len(object_types)} object types")

        # 3. extract ----------------------------------------------------
        jobs = [(doc, chunk) for doc in corpus.documents for chunk in doc.chunks]
        if skip_irrelevant:
            skipped = [chunk.key for _, chunk in jobs if not looks_normative(chunk.text)]
            jobs = [(doc, chunk) for doc, chunk in jobs if looks_normative(chunk.text)]
            if skipped:
                notes.append(f"skipped {len(skipped)} chunk(s) with no rule-like wording: {', '.join(skipped)}")
        last_chunk = {doc.doc_id: chunk.key for doc, chunk in jobs}
        pool = ThreadPoolExecutor(max_workers=workers) if workers > 1 else None
        try:
            for start in range(0, len(jobs), workers):
                batch = jobs[start:start + workers]
                # every call of a batch gets the same view of what is known so far
                known = {n: list(a.get("parameters", {})) for n, a in actions.items()}
                role_list = [dict(r) for r in roles.values()]
                calls = [
                    (f"extract:{chunk.key}",
                     *prompts.extract_prompt(doc.doc_id, chunk.text, object_types, link_types, role_list, known))
                    for doc, chunk in batch
                ]
                answers = (list(pool.map(lambda call: _ask(llm, *call), calls)) if pool
                           else [_ask(llm, *call) for call in calls])
                for (doc, chunk), (data, problem) in zip(batch, answers):  # merge in document order
                    if problem:
                        notes.append(problem)
                    _collect(data, doc, roles, actions, rules, facts, notes)
                    if last_chunk[doc.doc_id] == chunk.key:
                        say(f"extract: {doc.doc_id}" + (f" ({len(doc.chunks)} chunks)" if len(doc.chunks) > 1 else ""))
        finally:
            if pool:
                pool.shutdown(wait=True, cancel_futures=True)
    elif corpus.documents:
        notes.append("no LLM configured: documents were loaded but not analysed")

    # roles that actions and rules rely on must exist, even if the model never listed them
    used = [r for a in actions.values() for r in a.get("allowed_roles") or []]
    used += [r["then"]["require_approval"] for r in rules
             if isinstance(r["then"], dict) and isinstance(r["then"].get("require_approval"), str)]
    for rid in used:
        if rid not in roles:
            roles[rid] = {"id": rid, "label": None, "sources": []}
            notes.append(f"role {rid} is used by an action or rule but was never described; added without a label")

    # 4. provenance -----------------------------------------------------
    for role_id, role in roles.items():
        items[f"role:{role_id}"] = _item("role", role.get("sources", []), doc_text)
    for action_name, action in actions.items():
        items[f"action:{action_name}"] = _item("action", action.pop("_sources", []), doc_text)
    for rule in rules:
        items[f"rule:{rule['id']}"] = _item("rule", [rule["source"]], doc_text)
    for i, fact in enumerate(facts):
        fact["id_"] = f"fact:{i + 1}"
        items[fact["id_"]] = _item("fact", [fact["source"]], doc_text)

    ontology = {
        "name": name,
        "version": "0.1.0",
        "roles": {r: {"label": v.get("label")} for r, v in roles.items()},
        "object_types": object_types,
        "link_types": link_types,
        "action_types": {},
    }
    for action_name, action in actions.items():
        spec = {k: v for k, v in action.items() if k in ("label", "description", "allowed_roles", "parameters", "effects")}
        spec["rules"] = []
        ontology["action_types"][action_name] = spec
    for rule in rules:
        target = ontology["action_types"].get(rule["action"])
        if target is None:
            items[f"rule:{rule['id']}"]["errors"].append(f"refers to unknown action {rule['action']!r}")
            items[f"rule:{rule['id']}"]["status"] = "invalid"
            target = ontology.setdefault("x-orphan-rules", {}).setdefault(rule["action"], {"rules": []})
        target["rules"].append({k: rule[k] for k in ("id", "when", "then", "message", "source") if k in rule})

    # 5. validate -------------------------------------------------------
    _validate(ontology, items, notes)
    _check_facts(facts, ontology, ts.objects, items)

    # 6. conflicts ------------------------------------------------------
    conflicts = find_conflicts(ontology, documents, today)
    for c in conflicts:
        for rid in c["rules"]:
            items[f"rule:{rid}"].setdefault("conflicts", []).append(c["id"])
    say(f"draft: {len(actions)} actions, {len(rules)} rules, {len(facts)} facts, {len(conflicts)} conflicts")

    return Draft(
        name=name,
        ontology=ontology,
        seed={"objects": ts.objects, "links": ts.links},
        items=items,
        facts=facts,
        conflicts=conflicts,
        notes=notes,
        documents=documents,
    )


def _item(kind: str, sources: list[dict[str, Any]], doc_text: dict[str, str]) -> dict[str, Any]:
    checked = []
    for src in sources:
        src = dict(src)
        if "doc" in src:
            src["verified"] = quote_found(src.get("quote"), doc_text.get(src["doc"], ""))
        checked.append(src)
    verified = any(s.get("verified") for s in checked)
    if kind == "role":  # roles are names, not claims; keep them even without a verbatim quote
        return {"kind": kind, "status": "proposed", "sources": checked, "errors": [], "verified": verified}
    return {
        "kind": kind,
        "status": "proposed" if verified else "unverified",
        "sources": checked,
        "errors": [] if verified else ["quote not found verbatim in the source document"],
    }


def _apply_enrichment(data, object_types, roles, items, notes) -> None:
    for type_name, info in _dict(data.get("object_types")).items():
        ot = object_types.get(type_name)
        if ot is None or not isinstance(info, dict):
            continue
        for key in ("label", "description"):
            if _text(info.get(key)):
                ot[key] = info[key]
        for prop, pinfo in _dict(info.get("properties")).items():
            if prop in ot["properties"] and isinstance(pinfo, dict) and _text(pinfo.get("label")):
                ot["properties"][prop]["label"] = pinfo["label"]
    for type_name, info in _dict(data.get("new_object_types")).items():
        if type_name in object_types or not isinstance(info, dict):
            continue
        if not re.fullmatch(r"[A-Za-z_一-鿿][0-9A-Za-z_一-鿿]*", type_name):
            notes.append(f"new object type {type_name!r} ignored: not a usable type name")
            continue
        props = {}
        for prop, pinfo in _dict(info.get("properties")).items():
            ptype = pinfo.get("type") if isinstance(pinfo, dict) else pinfo
            props[prop] = {"type": ptype if isinstance(ptype, str) and ptype in PRIMITIVE_TYPES else "string"}
        pk = info.get("primary_key") if info.get("primary_key") in props else None
        if pk is None:
            pk = "id"
            props = {"id": {"type": "string", "required": True}, **props}
        object_types[type_name] = {"label": _text(info.get("label")), "primary_key": pk, "properties": props}
        items[f"object_type:{type_name}"] = {
            "kind": "object_type", "status": "proposed",
            "sources": [{"doc": _text(info.get("doc")), "quote": _text(info.get("quote"))}], "errors": [],
        }
        notes.append(f"new object type {type_name} proposed from text (no records yet)")
    for role in _list(data.get("roles")):
        _add_role(role, roles, role.get("doc") if isinstance(role, dict) else None)


def _add_role(role: Any, roles: dict[str, dict[str, Any]], doc_id: str | None) -> None:
    if not isinstance(role, dict) or not _text(role.get("id")):
        return
    rid = _snake(role["id"])
    entry = roles.setdefault(rid, {"id": rid, "label": _text(role.get("label")), "sources": []})
    entry["label"] = entry.get("label") or _text(role.get("label"))
    if _text(role.get("quote")) and _text(doc_id):
        entry["sources"].append({"doc": doc_id, "quote": role["quote"]})


def _collect(data, doc, roles, actions, rules, facts, notes) -> None:
    for role in _list(data.get("roles")):
        _add_role(role, roles, doc.doc_id)
    for action in _list(data.get("actions")):
        if not isinstance(action, dict) or not _text(action.get("name")):
            continue
        name = _snake(action["name"])
        source = {"doc": doc.doc_id, "quote": _text(action.get("quote"))}
        # "amount": "number" is a common shorthand for {"type": "number"}
        params = {
            str(p): ({"type": spec} if isinstance(spec, str) else spec)
            for p, spec in _dict(action.get("parameters")).items()
            if isinstance(spec, (str, dict))
        }
        if name in actions:
            actions[name]["_sources"].append(source)
            if params and params != actions[name].get("parameters"):
                notes.append(f"action {name}: {doc.doc_id} describes different parameters; kept the first")
            continue
        allowed = action.get("allowed_roles")
        allowed = [allowed] if isinstance(allowed, str) else _list(allowed)
        actions[name] = {
            "label": _text(action.get("label")),
            "description": _text(action.get("description")),
            "allowed_roles": [_snake(r) for r in allowed if _text(r)] or None,
            "parameters": params,
            "effects": [e for e in _list(action.get("effects")) if isinstance(e, dict)],
            "_sources": [source],
        }
    existing = {r["id"] for r in rules}
    for rule in _list(data.get("rules")):
        if not isinstance(rule, dict) or not _text(rule.get("when")) or not _text(rule.get("action")):
            continue
        rid = _snake(rule.get("id") or f"{rule['action']}_rule")
        base, n = rid, 2
        while rid in existing:
            rid, n = f"{base}_{n}", n + 1
        existing.add(rid)
        then = rule.get("then", "deny")
        if isinstance(then, dict) and _text(then.get("require_approval")):
            then = {"require_approval": _snake(then["require_approval"])}
        rules.append({
            "id": rid,
            "action": _snake(rule["action"]),
            "when": rule["when"],
            "then": then,
            "message": _text(rule.get("message")),
            "source": {
                "doc": doc.doc_id,
                "quote": _text(rule.get("quote")),
                # only a real date may drive scheduling; anything else falls back to the document's
                "effective_date": normalize_date(_text(rule.get("effective_date"))) or doc.meta.get("effective_date"),
            },
        })
    for fact in _list(data.get("facts")):
        if (isinstance(fact, dict) and _text(fact.get("type")) and isinstance(fact.get("id"), (str, int))
                and fact.get("id") != "" and isinstance(fact.get("set"), dict) and fact["set"]):
            facts.append({
                "type": fact["type"], "id": str(fact["id"]), "set": fact["set"],
                "source": {"doc": doc.doc_id, "quote": _text(fact.get("quote"))},
            })


def _base(ontology: dict[str, Any]) -> dict[str, Any]:
    return {k: copy.deepcopy(v) for k, v in ontology.items() if k != "action_types" and not k.startswith("x-")}


def _validate(ontology: dict[str, Any], items: dict[str, dict[str, Any]], notes: list[str]) -> None:
    base = _base(ontology)
    errors = _compile_errors({**base, "action_types": {}})
    if errors:
        notes.extend(f"schema: {e}" for e in errors)
        return
    for name, spec in ontology["action_types"].items():
        bare = {k: v for k, v in spec.items() if k != "rules"}
        errors = _compile_errors({**base, "action_types": {name: bare}})
        if errors:
            item = items[f"action:{name}"]
            item["status"] = "invalid"
            item["errors"].extend(errors)
            for rule in spec["rules"]:
                ritem = items[f"rule:{rule['id']}"]
                ritem["status"] = "invalid"
                ritem["errors"].append(f"its action {name} is invalid")
            continue
        for rule in spec["rules"]:
            errors = _compile_errors({**base, "action_types": {name: {**bare, "rules": [rule]}}})
            if errors:
                ritem = items[f"rule:{rule['id']}"]
                ritem["status"] = "invalid"
                ritem["errors"].extend(errors)


def _check_facts(facts, ontology, objects, items) -> None:
    for fact in facts:
        item = items[fact["id_"]]
        ot = ontology["object_types"].get(fact["type"])
        problem = None
        if ot is None:
            problem = f"unknown object type {fact['type']!r}"
        else:
            pk = ot["primary_key"]
            record = next((o for o in objects.get(fact["type"], []) if str(o.get(pk)) == fact["id"]), None)
            unknown = [p for p in fact["set"] if p not in ot["properties"]]
            wrong = [f"{p}={v!r} is not {ot['properties'][p].get('type', 'string')}"
                     for p, v in fact["set"].items()
                     if p not in unknown and not _value_fits(v, ot["properties"][p])]
            if record is None:
                problem = f"{fact['type']} {fact['id']!r} is not in the data"
            elif unknown:
                problem = f"unknown properties {unknown}"
            elif pk in fact["set"]:
                problem = f"a fact may not change the primary key {pk!r}"
            elif wrong:
                problem = "; ".join(wrong)
            else:
                fact["before"] = {p: record.get(p) for p in fact["set"]}
        if problem:
            item["status"] = "invalid"
            item["errors"].append(problem)


def _value_fits(value: Any, prop: dict[str, Any]) -> bool:
    if value is None:
        return not prop.get("required")
    if prop.get("enum") and value not in prop["enum"]:
        return False
    kind = prop.get("type", "string")
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind == "date":
        return isinstance(value, str) and normalize_date(value) == value
    return isinstance(value, str)


# ---------------------------------------------------------------- conflicts

_FLIP = {ast.Gt: ast.Lt, ast.GtE: ast.LtE, ast.Lt: ast.Gt, ast.LtE: ast.GtE}
_OPS = {ast.Gt: ">", ast.GtE: ">=", ast.Lt: "<", ast.LtE: "<="}


def _threshold(when: str) -> tuple[str, str, float] | None:
    """``params.amount > 50000`` -> ("params.amount", ">", 50000)."""
    try:
        node = ast.parse(when, mode="eval").body
    except SyntaxError:
        return None
    if not (isinstance(node, ast.Compare) and len(node.ops) == 1 and type(node.ops[0]) in _OPS):
        return None
    left, op, right = node.left, type(node.ops[0]), node.comparators[0]
    if isinstance(left, ast.Constant) and not isinstance(right, ast.Constant):
        left, right, op = right, left, _FLIP[op]
    if not (isinstance(right, ast.Constant) and isinstance(right.value, (int, float))):
        return None
    return ast.unparse(left), _OPS[op], float(right.value)


def _outcome_key(then: Any) -> str:
    if isinstance(then, dict) and "require_approval" in then:
        return f"approval:{then['require_approval']}"
    return str(then)


def _rule_date(rule: dict[str, Any], documents: dict[str, dict[str, Any]]) -> str | None:
    src = rule.get("source") or {}
    meta = documents.get(src.get("doc"), {}).get("meta", {})
    return src.get("effective_date") or meta.get("effective_date") or meta.get("latest_date_mentioned")


def find_conflicts(ontology: dict[str, Any], documents: dict[str, dict[str, Any]], today: str | None = None) -> list[dict[str, Any]]:
    from datetime import date

    today = today or date.today().isoformat()
    conflicts: list[dict[str, Any]] = []
    for action_name, spec in ontology.get("action_types", {}).items():
        groups: dict[tuple, list[dict[str, Any]]] = {}
        for rule in spec.get("rules", []):
            th = _threshold(rule["when"]) if isinstance(rule.get("when"), str) else None
            if th is None:
                continue
            direction = th[1].rstrip("=")
            groups.setdefault((th[0], direction, _outcome_key(rule.get("then"))), []).append({**rule, "_th": th})
        for (path, direction, outcome), members in groups.items():
            values = {m["_th"][2] for m in members}
            if len(members) < 2:
                continue
            dated = sorted(members, key=lambda m: _rule_date(m, documents) or "")
            newest = dated[-1]
            newest_date = _rule_date(newest, documents)
            kind = "threshold_mismatch" if len(values) > 1 else "duplicate"
            conflict = {
                "id": f"C{len(conflicts) + 1}",
                "kind": kind,
                "action": action_name,
                "rules": [m["id"] for m in dated],
                "detail": "; ".join(
                    f"{m['id']}: {m['when']} -> {outcome} (from {m['source'].get('doc')}, "
                    f"effective {_rule_date(m, documents) or 'unknown'})" for m in dated
                ),
            }
            if kind == "duplicate":
                conflict["suggestion"] = f"keep:{newest['id']}"
                conflict["why"] = "same rule stated twice; keep one"
            elif newest_date and newest_date > today and all(_rule_date(m, documents) for m in dated):
                conflict["suggestion"] = "schedule"
                conflict["why"] = (f"{newest['id']} only takes effect on {newest_date}; keep both and switch "
                                   f"over on that date")
            elif newest_date:
                conflict["suggestion"] = f"keep:{newest['id']}"
                conflict["why"] = f"{newest['id']} comes from the most recent document ({newest_date})"
            else:
                conflict["why"] = "no dates found; a person must decide"
            conflicts.append(conflict)
    return conflicts


def save(draft: Draft, path) -> None:
    from pathlib import Path

    Path(path).write_text(json.dumps(draft.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
