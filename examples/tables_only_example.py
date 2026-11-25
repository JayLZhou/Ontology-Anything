"""Schema induction from tables alone -- deterministic, no LLM, no API key.

Shows what stage 1 infers from spreadsheets: object types, typed properties,
primary keys, ID prefixes, enums, and foreign keys turned into links.

    python examples/tables_only_example.py [folder or files ...]
"""

from __future__ import annotations

import sys
from pathlib import Path

from ontology_anything import infer_tables, load_corpus

DEFAULT = Path(__file__).resolve().parent / "procurement" / "data"


def main() -> None:
    corpus = load_corpus(sys.argv[1:] or [DEFAULT])
    schema = infer_tables(corpus.tables)

    for name, object_type in schema.object_types.items():
        prefix = f", ids like {object_type['id_prefix']}N" if object_type.get("id_prefix") else ""
        print(f"{name}  (from {schema.provenance[name]}, key {object_type['primary_key']}{prefix}, "
              f"{len(schema.objects[name])} records)")
        for prop, spec in object_type["properties"].items():
            enum = f"  one of {spec['enum']}" if spec.get("enum") else ""
            print(f"    {prop}: {spec['type']}{enum}")
    for name, link in schema.link_types.items():
        count = sum(1 for ln in schema.links if ln["type"] == name)
        print(f"link {name}: {link['from']} -> {link['to']}  ({count} instances)")
    for note in schema.notes:
        print(f"note: {note}")


if __name__ == "__main__":
    main()
