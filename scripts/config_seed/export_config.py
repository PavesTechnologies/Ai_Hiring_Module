"""
Export the product configuration of the database the app is pointed at, as
canonical JSON keyed by natural keys - never database ids, timestamps,
who-edited-it columns or embeddings - so two databases can be diffed and the
result written as seed data (see diff_config.py and app/seeds/seed_product_config.py).

    python scripts/config_seed/export_config.py <out.json>

Only configuration tables are read (the TABLES below). Candidate, campaign,
JD, audit, task and cache data, users, organisations, encryption keys and AI
provider credentials are never exported. Read-only: one READ ONLY transaction.
"""
import json
import sys

from sqlalchemy import text

from app.seeds.product_config_spec import TABLES, plain as _plain, row_key  # noqa: E402


def export(connection) -> dict:
    out = {}
    for table, (keys, fields, sql) in TABLES.items():
        rows = [dict(r._mapping) for r in connection.execute(text(sql))]
        records = {}
        for row in rows:
            key = row_key(row, keys)
            records[key] = {f: _plain(row[f]) for f in keys + fields}
        out[table] = {"keys": keys, "fields": fields, "rows": dict(sorted(records.items()))}
    return out


def main(path: str) -> None:
    from app.db.session import engine
    engine.echo = False
    with engine.connect() as connection:
        connection.execute(text("SET TRANSACTION READ ONLY"))
        data = export(connection)
        connection.rollback()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=False)
    print({table: len(t["rows"]) for table, t in data.items()})


if __name__ == "__main__":
    main(sys.argv[1])
