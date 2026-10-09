"""Probe local factor_snapshots for large-cap scores."""
from __future__ import annotations

import sqlite3
from pathlib import Path

roots = [Path("data"), Path("."), Path("..") / "data"]
dbs = []
for r in roots:
    if r.exists():
        dbs.extend(r.rglob("candle_flow.db"))
        dbs.extend(r.rglob("*.db"))

seen = set()
for p in dbs:
    p = p.resolve()
    if p in seen:
        continue
    seen.add(p)
    try:
        c = sqlite3.connect(str(p))
        tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    except Exception as e:
        print("skip", p, e)
        continue
    print("DB", p, "tables", tables)
    if "factor_snapshots" not in tables:
        continue
    n = c.execute("select count(*) from factor_snapshots").fetchone()[0]
    n2 = c.execute(
        "select count(*) from factor_snapshots where composite_score is not null"
    ).fetchone()[0]
    print("  snapshots", n, "scored", n2)
    try:
        rows = c.execute(
            """
            select symbol, round(composite_score,1),
                   round(json_extract(payload, '$.market.market_cap')/1e8, 0),
                   json_extract(payload, '$.name'),
                   json_extract(payload, '$.final_rating')
            from factor_snapshots
            where composite_score is not null
              and json_extract(payload, '$.market.market_cap') >= 200e8
            order by composite_score desc
            limit 20
            """
        ).fetchall()
        print("  top large-cap:")
        for r in rows:
            print("   ", r)
    except Exception as e:
        print("  query failed", e)
