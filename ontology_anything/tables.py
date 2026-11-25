"""Deterministic schema induction from tables (no LLM).

For each table: an object type with typed properties, a primary key, an ID
prefix, enum detection -- and foreign keys turned into link types by checking
that a column's values are a subset of another table's primary keys.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from .documents import Table

_BOOL_TRUE = {"true", "yes", "y", "是", "真"}
_BOOL_FALSE = {"false", "no", "n", "否", "假"}
_ID_HINT = re.compile(r"(^id$|_id$|^id_|编号|代码|code$|_no$|^no$)", re.I)
_PREFIX = re.compile(r"^([A-Za-z]+[-_]?)(\d+)$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}")
_COUNT_HINT = re.compile(r"(qty|quantity|count|num|number_of|数量|件数|个数|次数)", re.I)


@dataclass
class TableSchema:
    object_types: dict[str, dict[str, Any]] = field(default_factory=dict)
    link_types: dict[str, dict[str, Any]] = field(default_factory=dict)
    objects: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    links: list[dict[str, str]] = field(default_factory=list)
    provenance: dict[str, str] = field(default_factory=dict)  # type name -> table id
    notes: list[str] = field(default_factory=list)


def type_name_for(table_name: str) -> str:
    """``purchase_orders`` -> ``PurchaseOrder``; non-ASCII names are kept as-is."""
    name = re.sub(r"[^0-9A-Za-z_一-鿿]+", "_", table_name).strip("_") or "Record"
    if not name.isascii():
        return name
    words = [w for w in name.split("_") if w]
    if words:
        last = words[-1]
        if last.endswith("ies") and len(last) > 4:
            last = last[:-3] + "y"
        elif last.endswith(("ses", "xes", "ches", "shes")):
            last = last[:-2]
        elif last.endswith("s") and not last.endswith("ss") and len(last) > 3:
            last = last[:-1]
        words[-1] = last
    return "".join(w[:1].upper() + w[1:] for w in words)


def prop_name_for(column: str) -> str:
    name = re.sub(r"[^0-9A-Za-z_一-鿿]+", "_", column.strip()).strip("_")
    if not name:
        name = "field"
    if name[0].isdigit():
        name = "f_" + name
    return name.lower() if name.isascii() else name


def infer_column_type(values: list[str], column: str | None = None) -> str:
    present = [v for v in values if v != ""]
    if not present:
        return "string"
    lowered = {v.lower() for v in present}
    if lowered <= (_BOOL_TRUE | _BOOL_FALSE):
        return "boolean"
    if all(re.fullmatch(r"[+-]?\d+", v) for v in present):
        # leading zeros mean an identifier, not a number
        if any(len(v) > 1 and v.lstrip("+-").startswith("0") for v in present):
            return "string"
        # whole numbers in a sample (amounts, prices) can still take decimals later
        return "integer" if column and _COUNT_HINT.search(column) else "number"
    if all(_is_number(v) for v in present):
        return "number"
    if all(_DATE.match(v) for v in present):
        return "date"
    return "string"


def _is_number(value: str) -> bool:
    try:
        float(value.replace(",", ""))
        return True
    except ValueError:
        return False


def _convert(value: str, type_name: str) -> Any:
    if value == "":
        return None
    if type_name == "boolean":
        return value.lower() in _BOOL_TRUE
    if type_name == "integer":
        return int(value)
    if type_name == "number":
        number = float(value.replace(",", ""))
        return int(number) if number.is_integer() else number
    if type_name == "date":
        return value[:10]
    return value


def _primary_key(table: Table) -> str | None:
    unique = [
        c for c in table.columns
        if table.rows and all(r.get(c) for r in table.rows)
        and len({r[c] for r in table.rows}) == len(table.rows)
    ]
    hinted = [c for c in unique if _ID_HINT.search(c)]
    if hinted:
        return hinted[0]
    return unique[0] if unique else None


def infer_tables(tables: list[Table]) -> TableSchema:
    out = TableSchema()
    pk_values: dict[str, tuple[str, set[str]]] = {}  # type -> (pk column, values)
    columns_of: dict[str, dict[str, str]] = {}  # type -> column -> prop name
    for table in tables:
        if not table.rows:
            out.notes.append(f"{table.table_id}: empty table skipped")
            continue
        type_name = type_name_for(table.name)
        if type_name in out.object_types:
            type_name = f"{type_name}_{len(out.object_types)}"
        pk = _primary_key(table)
        props: dict[str, dict[str, Any]] = {}
        colmap: dict[str, str] = {}
        for col in table.columns:
            pname = prop_name_for(col)
            colmap[col] = pname
            values = [r.get(col, "") for r in table.rows]
            ptype = infer_column_type(values, col)
            spec: dict[str, Any] = {"type": ptype}
            if pname != col:
                spec["label"] = col
            distinct = sorted({v for v in values if v})
            if ptype == "string" and col != pk and 1 < len(distinct) <= 4 and len(distinct) * 2 <= len(values):
                spec["enum"] = distinct
            if col == pk:
                spec["required"] = True
            props[pname] = spec
        if pk is None:
            pk_prop = "row_id"
            props = {"row_id": {"type": "string", "required": True}, **props}
            out.notes.append(f"{table.table_id}: no unique column found; added synthetic row_id")
        else:
            pk_prop = colmap[pk]
        ot: dict[str, Any] = {"primary_key": pk_prop, "properties": props}
        if pk is not None:
            prefixes = Counter(m.group(1) for v in (r[pk] for r in table.rows) if (m := _PREFIX.match(v)))
            if prefixes and prefixes.most_common(1)[0][1] == len(table.rows):
                ot["id_prefix"] = prefixes.most_common(1)[0][0]
        out.object_types[type_name] = ot
        out.provenance[type_name] = table.table_id
        columns_of[type_name] = colmap
        rows = []
        for i, r in enumerate(table.rows):
            obj = {colmap[c]: _convert(r.get(c, ""), props[colmap[c]]["type"]) for c in table.columns}
            if pk is None:
                obj["row_id"] = f"{type_name}-{i + 1}"
            rows.append(obj)
        out.objects[type_name] = rows
        if pk is not None:
            pk_values[type_name] = (pk_prop, {r[pk] for r in table.rows})

    # foreign keys -> link types
    for type_name, colmap in columns_of.items():
        ot = out.object_types[type_name]
        for col, pname in list(colmap.items()):
            if pname == ot["primary_key"]:
                continue
            values = {str(o[pname]) for o in out.objects[type_name] if o.get(pname) not in (None, "")}
            if not values:
                continue
            target = _match_foreign_key(pname, values, pk_values, exclude=type_name)
            if target is None:
                continue
            link_name = re.sub(r"(_id|id|_no|_code|编号)$", "", pname, flags=re.I).strip("_") or target.lower()
            if link_name in ot["properties"] and link_name != pname:
                link_name = f"{link_name}_ref"
            if link_name in out.link_types:
                link_name = f"{type_name.lower()}_{link_name}"
            out.link_types[link_name] = {"from": type_name, "to": target, "cardinality": "many_to_one"}
            pk_prop = ot["primary_key"]
            for obj in out.objects[type_name]:
                ref = obj.pop(pname, None)
                if ref not in (None, ""):
                    out.links.append({"type": link_name, "from": str(obj[pk_prop]), "to": str(ref)})
            del ot["properties"][pname]
            out.notes.append(f"{type_name}.{col} -> link '{link_name}' to {target}")
    return out


def _match_foreign_key(pname, values, pk_values, exclude) -> str | None:
    candidates = []
    for type_name, (pk_prop, pks) in pk_values.items():
        if type_name == exclude or not values <= pks:
            continue
        score = 2 if pname == pk_prop else (1 if type_name.lower() in pname.lower().replace("_", "") else 0)
        candidates.append((score, type_name))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    best_score = candidates[0][0]
    # a plain value column whose values happen to match is not a key unless the name says so
    if best_score == 0 and not _ID_HINT.search(pname):
        return None
    return candidates[0][1]
