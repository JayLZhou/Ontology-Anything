"""Human review: render the draft as a report, then approve it into a final
ontology that nano-onto can load.

Approval is explicit. Invalid items are always left out; unverified items
(quote not found in the source) are left out unless you include them; and every
conflict must be resolved -- ``keep:<rule id>``, ``schedule`` (keep both, switch
on the newer rule's effective date) or ``suggested``.
"""

from __future__ import annotations

import copy
from typing import Any

from .pipeline import Draft, _compile_errors, _rule_date


class ReviewError(ValueError):
    pass


def _then_text(then: Any) -> str:
    if isinstance(then, dict) and "require_approval" in then:
        return f"needs approval: {then['require_approval']}"
    return str(then)


def _mark(item: dict[str, Any] | None) -> str:
    if item is None:
        return ""
    return {"proposed": "OK", "unverified": "UNVERIFIED", "invalid": "INVALID"}.get(item["status"], item["status"])


def render_review(draft: Draft) -> str:
    onto = draft.ontology
    items = draft.items
    count = lambda kind, status=None: sum(  # noqa: E731
        1 for i in items.values() if i["kind"] == kind and (status is None or i["status"] == status)
    )
    out = [
        f"# Ontology draft: {draft.name}",
        "",
        "Review this before approving. Every rule and fact below came from a model reading your documents;",
        "OK means the quoted sentence was found verbatim in the source and the rule compiles.",
        "",
        "| | proposed | unverified | invalid |",
        "|---|---|---|---|",
    ]
    for kind in ("object_type", "role", "action", "rule", "fact"):
        out.append(f"| {kind} | {count(kind, 'proposed')} | {count(kind, 'unverified')} | {count(kind, 'invalid')} |")
    out += ["", f"Conflicts to resolve: **{len(draft.conflicts)}**", ""]

    if draft.conflicts:
        out += ["## Conflicts", ""]
        for c in draft.conflicts:
            out.append(f"### {c['id']} -- {c['kind']} in `{c['action']}`")
            out.append("")
            for part in c["detail"].split("; "):
                out.append(f"- {part}")
            if c.get("suggestion"):
                out.append(f"- **Suggested:** `{c['suggestion']}` -- {c.get('why', '')}")
            else:
                out.append(f"- {c.get('why', '')}")
            out.append(f"- Resolve with `--resolve {c['id']}=keep:<rule id>` or `--resolve {c['id']}=schedule`")
            out.append("")

    out += ["## Object types", ""]
    for name, ot in onto["object_types"].items():
        label = f" ({ot['label']})" if ot.get("label") else ""
        src = items.get(f"object_type:{name}", {}).get("sources", [{}])
        where = src[0].get("table") or src[0].get("doc") or "?"
        out.append(f"- **{name}**{label} -- key `{ot['primary_key']}`, from `{where}`")
        props = ", ".join(
            f"{p}:{s['type']}" + (f"({s['label']})" if s.get("label") else "") + (f" {s['enum']}" if s.get("enum") else "")
            for p, s in ot["properties"].items()
        )
        out.append(f"  - {props}")
    for name, lt in onto["link_types"].items():
        out.append(f"- link **{name}**: {lt['from']} -> {lt['to']}")
    out += ["", "## Roles", ""]
    for rid, role in onto["roles"].items():
        out.append(f"- `{rid}` {role.get('label') or ''}")

    out += ["", "## Actions and rules", ""]
    for name, spec in onto["action_types"].items():
        item = items.get(f"action:{name}")
        out.append(f"### [{_mark(item)}] `{name}` {spec.get('label') or ''}")
        out.append("")
        if spec.get("description"):
            out.append(spec["description"])
            out.append("")
        params = ", ".join(
            f"{p}: {s.get('object_type') or s.get('type', 'string')}" for p, s in (spec.get("parameters") or {}).items()
            if isinstance(s, dict)
        )
        out.append(f"- parameters: {params or '(none)'}")
        out.append(f"- allowed roles: {', '.join(spec.get('allowed_roles') or ['any'])}")
        for err in (item or {}).get("errors", []):
            out.append(f"- error: {err}")
        for rule in spec.get("rules", []):
            ritem = items.get(f"rule:{rule['id']}")
            src = rule.get("source") or {}
            out.append(f"- [{_mark(ritem)}] **{rule['id']}**: `{rule['when']}` -> {_then_text(rule.get('then'))}"
                       + (f" -- {rule['message']}" if rule.get("message") else ""))
            out.append(f"  - source: {src.get('doc')}: \"{src.get('quote')}\"")
            for err in (ritem or {}).get("errors", []):
                out.append(f"  - error: {err}")
            if ritem and ritem.get("conflicts"):
                out.append(f"  - in conflict: {', '.join(ritem['conflicts'])}")
        out.append("")

    if draft.facts:
        out += ["## Facts (record updates found in text)", ""]
        for fact in draft.facts:
            item = items.get(fact["id_"])
            before = fact.get("before")
            change = ", ".join(
                f"{k}: {before.get(k) if before else '?'} -> {v}" for k, v in fact["set"].items()
            )
            out.append(f"- [{_mark(item)}] `{fact['id_']}` {fact['type']} {fact['id']}: {change}")
            out.append(f"  - source: {fact['source']['doc']}: \"{fact['source'].get('quote')}\"")
            for err in (item or {}).get("errors", []):
                out.append(f"  - error: {err}")
        out.append("")

    if draft.notes:
        out += ["## Notes", ""] + [f"- {n}" for n in draft.notes] + [""]
    out += [
        "## Next step",
        "",
        "```bash",
        "ontology-anything approve <out dir> --accept-suggestions      # or --resolve C1=keep:<rule id>",
        "```",
        "",
    ]
    return "\n".join(out)


def approve(
    draft: Draft,
    resolutions: dict[str, str] | None = None,
    accept_suggestions: bool = False,
    drop: list[str] | None = None,
    include_unverified: bool = False,
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """Return ``(ontology, seed, log)``; raises :class:`ReviewError` if unresolved."""
    resolutions = dict(resolutions or {})
    drop_set = set(drop or [])
    log: list[str] = []
    onto = copy.deepcopy(draft.ontology)
    onto.pop("x-orphan-rules", None)

    def left_out(item_id: str) -> str | None:
        """Why an item will not make it into the approved ontology, or None if it will."""
        item = draft.items.get(item_id)
        if item_id in drop_set:
            return f"dropped {item_id} (by request)"
        if item is None:
            return None
        if item["status"] == "invalid":
            return f"left out {item_id}: invalid ({'; '.join(item['errors'])[:160]})"
        if item["status"] == "unverified" and not include_unverified:
            return f"left out {item_id}: quote not verified (use --include-unverified to keep)"
        return None

    def keep(item_id: str) -> bool:
        reason = left_out(item_id)
        if reason:
            log.append(reason)
        return reason is None

    unknown = sorted(drop_set - set(draft.items))
    if unknown:
        raise ReviewError(f"unknown item id(s) in --drop: {', '.join(unknown)}")

    # conflicts first: they decide which rules survive and how
    unresolved = []
    rule_overrides: dict[str, str | None] = {}  # rule id -> new 'when' (None = remove)
    for c in draft.conflicts:
        choice = resolutions.pop(c["id"], None)
        # a conflict only matters between rules that will actually be approved
        live = [rid for rid in c["rules"] if left_out(f"rule:{rid}") is None]
        if len(live) < 2:
            log.append(f"{c['id']}: nothing to resolve, " + (f"only {live[0]} remains" if live else "no rule remains"))
            continue
        if choice in (None, "suggested") and (accept_suggestions or choice == "suggested"):
            choice = c.get("suggestion")
        if not choice:
            unresolved.append(c)
            continue
        if choice.startswith("keep:"):
            winner = choice.split(":", 1)[1]
            if winner not in c["rules"]:
                raise ReviewError(f"{c['id']}: {winner!r} is not one of {c['rules']}")
            if winner not in live:
                raise ReviewError(f"{c['id']}: cannot keep {winner}, it is being {left_out(f'rule:{winner}')}")
            for rid in live:
                if rid != winner:
                    rule_overrides[rid] = None
            log.append(f"{c['id']}: kept {winner}")
        elif choice == "schedule":
            rules = _rules_by_id(onto, c["action"])
            ordered = sorted((rules[r] for r in live), key=lambda r: _rule_date(r, draft.documents) or "")
            dates = [_rule_date(r, draft.documents) for r in ordered]
            if not all(dates):
                raise ReviewError(f"{c['id']}: cannot schedule, some rules have no effective date")
            for i, rule in enumerate(ordered):
                guards = []
                if i > 0:
                    guards.append(f"today() >= '{dates[i]}'")
                if i < len(ordered) - 1:
                    guards.append(f"today() < '{dates[i + 1]}'")
                rule_overrides[rule["id"]] = f"({rule['when']}) and " + " and ".join(guards)
            log.append(f"{c['id']}: scheduled " + " -> ".join(f"{r['id']}@{d}" for r, d in zip(ordered, dates)))
        else:
            raise ReviewError(f"{c['id']}: unknown resolution {choice!r}")
    if resolutions:
        raise ReviewError(f"unknown conflict id(s): {', '.join(resolutions)}")
    if unresolved:
        lines = [f"  {c['id']}: {c['detail']}" + (f"\n      suggested: {c['suggestion']}" if c.get("suggestion") else "")
                 for c in unresolved]
        raise ReviewError("unresolved conflicts:\n" + "\n".join(lines)
                          + "\nresolve with --resolve <id>=keep:<rule>|schedule, or --accept-suggestions")

    # filter items
    onto["object_types"] = {k: v for k, v in onto["object_types"].items() if keep(f"object_type:{k}")}
    onto["roles"] = {k: v for k, v in onto["roles"].items() if keep(f"role:{k}")}
    actions = {}
    for name, spec in onto["action_types"].items():
        if not keep(f"action:{name}"):
            continue
        kept_rules = []
        for rule in spec.get("rules", []):
            if rule_overrides.get(rule["id"], rule["when"]) is None:
                continue
            if not keep(f"rule:{rule['id']}"):
                continue
            if rule["id"] in rule_overrides:
                rule = {**rule, "when": rule_overrides[rule["id"]]}
            kept_rules.append(rule)
        actions[name] = {**spec, "rules": kept_rules}
    onto["action_types"] = actions

    seed = copy.deepcopy(draft.seed)
    seed["objects"] = {t: rows for t, rows in seed["objects"].items() if t in onto["object_types"]}
    seed["links"] = [
        ln for ln in seed["links"]
        if ln["type"] in onto["link_types"]
        and onto["link_types"][ln["type"]]["from"] in onto["object_types"]
        and onto["link_types"][ln["type"]]["to"] in onto["object_types"]
    ]
    for fact in draft.facts:
        if not keep(fact["id_"]):
            continue
        ot = onto["object_types"].get(fact["type"])
        if ot is None:
            continue
        for row in seed["objects"].get(fact["type"], []):
            if str(row.get(ot["primary_key"])) == fact["id"]:
                row.update(fact["set"])
                log.append(f"applied {fact['id_']}: {fact['type']} {fact['id']} {fact['set']}")

    errors = _compile_errors(onto)
    if errors:
        raise ReviewError("approved ontology does not compile:\n  " + "\n  ".join(errors))
    return onto, seed, log


def _rules_by_id(onto: dict[str, Any], action: str) -> dict[str, dict[str, Any]]:
    return {r["id"]: r for r in onto["action_types"][action].get("rules", [])}
