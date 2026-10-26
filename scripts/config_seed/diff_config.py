"""
Diff two configuration exports (export_config.py): what the source database
(dev) has beyond the reference (a fresh database: migrations + the existing
seeds), per table and natural key.

    python scripts/config_seed/diff_config.py <reference.json> <source.json> <diff.json>

The diff lists, per table: rows to add (only in the source), field changes
({field: {"from": reference value, "to": source value}}), and rows only in the
reference (reported for review, never deleted by the seed).
"""
import json
import sys


def diff(reference: dict, source: dict) -> dict:
    out = {}
    for table, spec in source.items():
        ref_rows = reference.get(table, {}).get("rows", {})
        src_rows = spec["rows"]
        added = {k: v for k, v in src_rows.items() if k not in ref_rows}
        changed = {}
        for key in src_rows.keys() & ref_rows.keys():
            fields = {f: {"from": ref_rows[key].get(f), "to": src_rows[key].get(f)}
                      for f in spec["fields"] if ref_rows[key].get(f) != src_rows[key].get(f)}
            if fields:
                changed[key] = fields
        only_in_reference = sorted(k for k in ref_rows if k not in src_rows)
        out[table] = {"keys": spec["keys"], "fields": spec["fields"], "add": dict(sorted(added.items())),
                      "change": dict(sorted(changed.items())), "only_in_reference": only_in_reference}
    return out


def summary(d: dict) -> dict:
    return {t: {"add": len(v["add"]), "change": len(v["change"]), "only_in_reference": len(v["only_in_reference"])}
            for t, v in d.items()}


if __name__ == "__main__":
    reference = json.load(open(sys.argv[1], encoding="utf-8"))
    source = json.load(open(sys.argv[2], encoding="utf-8"))
    result = diff(reference, source)
    json.dump(result, open(sys.argv[3], "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    for table, counts in summary(result).items():
        if any(counts.values()):
            print(table, counts)
