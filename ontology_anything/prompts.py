"""Prompts. Each returns ``(system, user)``; every answer must be one JSON object."""

from __future__ import annotations

import json
from typing import Any

DSL_GUIDE = """\
Rule and effect expressions use a small Python-like language:
- roots: params.<parameter>, actor.id, actor.role, and aliases created by earlier effects
- object parameters expose their properties and links: params.supplier.blacklisted, params.po.supplier.rating
- operators: and or not, == != < <= > >=, in / not in, + - * / %, constants 'text' 10000 True False None
- functions: len abs min max lower upper, and today() which returns 'YYYY-MM-DD'
- string literals MUST be quoted inside the expression: "'approved'", not "approved"
Rule outcomes ("then"): "deny", "warn", or {"require_approval": "<role id>"}.
Effects: {"create": "<Type>", "as": "<alias>", "set": {"<prop>": "<expr>"}},
         {"update": "<expr resolving to an object, e.g. params.po>", "set": {...}},
         {"link": "<link type>", "from": "<expr>", "to": "<expr>"}."""

SYSTEM = (
    "You are an ontology engineer. You turn business documents into an executable ontology: "
    "object types, roles, and action types guarded by rules. Be literal: extract only what the text "
    "states, and copy the exact sentence you relied on into \"quote\" (verbatim, no paraphrase). "
    "Reply with a single JSON object and nothing else."
)


def schema_summary(object_types: dict[str, Any], link_types: dict[str, Any]) -> str:
    lines = []
    for name, ot in object_types.items():
        props = ", ".join(
            f"{p}:{s.get('type', 'string')}" + (f"{s['enum']}" if s.get("enum") else "")
            for p, s in ot.get("properties", {}).items()
        )
        lines.append(f"- {name} (key {ot.get('primary_key')}): {props}")
    for name, lt in link_types.items():
        lines.append(f"- link {name}: {lt['from']} -> {lt['to']} ({lt.get('cardinality', 'many_to_one')})")
    return "\n".join(lines) or "(no types yet)"


def enrich_prompt(object_types, link_types, excerpts: list[tuple[str, str]]) -> tuple[str, str]:
    docs = "\n\n".join(f"<doc id=\"{doc_id}\">\n{text}\n</doc>" for doc_id, text in excerpts)
    user = f"""Current types, inferred from data tables:
{schema_summary(object_types, link_types)}

Document excerpts:
{docs}

Return JSON:
{{
  "object_types": {{"<existing type>": {{"label": "<human name in the documents' language>",
                     "description": "...", "properties": {{"<prop>": {{"label": "..."}}}}}}}},
  "new_object_types": {{"<TypeName>": {{"label": "...", "primary_key": "<prop>",
                        "properties": {{"<prop>": {{"type": "string|number|integer|boolean|date"}}}},
                        "quote": "<verbatim sentence>", "doc": "<doc id>"}}}},
  "roles": [{{"id": "<snake_case>", "label": "<name used in the documents>", "quote": "...", "doc": "<doc id>"}}]
}}
Only add new object types the documents clearly describe and the tables do not cover."""
    return SYSTEM, user


def extract_prompt(doc_id: str, text: str, object_types, link_types, roles, known_actions) -> tuple[str, str]:
    actions = json.dumps(known_actions, ensure_ascii=False) if known_actions else "(none yet)"
    role_list = ", ".join(f"{r['id']} ({r.get('label', '')})" for r in roles) or "(none yet)"
    user = f"""Schema:
{schema_summary(object_types, link_types)}

Roles: {role_list}
Actions already extracted from other documents (reuse these names when the text refers to them): {actions}

{DSL_GUIDE}

Document "{doc_id}":
<<<
{text}
>>>

Extract from THIS document only. Return JSON:
{{
  "actions": [{{"name": "<verb_object snake_case>", "label": "...", "description": "...",
               "allowed_roles": ["<role id>"],
               "parameters": {{"<name>": {{"type": "string|number|integer|boolean|date|object_ref",
                                           "object_type": "<Type, for object_ref>"}}}},
               "effects": [ ... ], "quote": "<verbatim>"}}],
  "rules": [{{"id": "<snake_case unique id>", "action": "<action name>", "when": "<expression>",
             "then": "deny" | "warn" | {{"require_approval": "<role id>"}},
             "message": "<short message in the documents' language>",
             "effective_date": "<YYYY-MM-DD if the text gives one, else null>",
             "quote": "<verbatim sentence>"}}],
  "facts": [{{"type": "<Type>", "id": "<primary key value>", "set": {{"<prop>": <value>}},
             "quote": "<verbatim sentence>"}}],
  "roles": [{{"id": "<snake_case>", "label": "...", "quote": "..."}}]
}}
"facts" are statements that change specific records (e.g. a named supplier is blacklisted)."""
    return SYSTEM, user
