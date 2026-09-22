"""验证 reduce_window 读层链路：SQL json_extract → _from_payload → _base_items → _overlay_one。"""
import json, sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "backend"))

from app.database import SessionLocal
from app.models import FactorSnapshot
from app.services import market_scan as MS

db = SessionLocal()
try:
    print("=== 1) SQL json_extract 路径实测 ===")
    rows, fell_back = MS.load_covered(db)
    print(f"load_covered: {len(rows)} 行, 回退路径={fell_back}")
    hit = [r for r in rows if r.get("reduce_window")]
    print(f"带 reduce_window 的行数: {len(hit)}")
    if hit:
        print("样例:", hit[0]["symbol"], json.dumps(hit[0]["reduce_window"], ensure_ascii=False))

    print()
    print("=== 2) _from_payload 回退路径一致性 ===")
    snap = db.query(FactorSnapshot).filter(
        FactorSnapshot.symbol == "603519.SH"
    ).first()
    if snap is None:
        snap = db.query(FactorSnapshot).filter(
            FactorSnapshot.symbol.like("603519%")
        ).first()
    if snap is not None:
        out = MS._from_payload(snap)
        print(f"{snap.symbol} _from_payload.reduce_window =",
              json.dumps(out.get("reduce_window"), ensure_ascii=False))
        # SQL 侧同票对照
        sql_row = [r for r in rows if r["symbol"] == snap.symbol]
        if sql_row:
            print(f"{snap.symbol} SQL 侧        .reduce_window =",
                  json.dumps(sql_row[0].get("reduce_window"), ensure_ascii=False))
        # 原始 payload 真值
        d = json.loads(snap.payload or "{}")
        print(f"{snap.symbol} payload.major_risks.reduce_window =",
              json.dumps((d.get("major_risks") or {}).get("reduce_window"), ensure_ascii=False))
    else:
        print("未找到 603519 快照（可能尚未用新口径重建）")

    print()
    print("=== 3) _base_items 是否透传 ===")
    items = MS._base_items(rows)
    it_hit = [x for x in items if x.get("reduce_window")]
    print(f"_base_items 带 reduce_window 的条目数: {len(it_hit)}")
    if it_hit:
        print("样例:", it_hit[0]["symbol"], json.dumps(it_hit[0]["reduce_window"], ensure_ascii=False))
finally:
    db.close()
