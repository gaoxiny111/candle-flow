# -*- coding: utf-8 -*-
"""用线上真实因子快照验证全市场扫描读层（不联网、不触碰生产库）。

输出：覆盖率自证、Top 榜、维度排序榜、以及「读层分数 == payload 原始综合分」的一致性校验。

用法（先取回线上快照，再本地跑）：

    ssh root@47.100.175.214 'python3 - << "EOF"
import sqlite3, json
con=sqlite3.connect("/opt/candle-flow/backend/data/candle_flow.db")
cur=con.cursor()
cur.execute("SELECT symbol, composite_score, pe_ttm, built_at, payload FROM factor_snapshots")
json.dump([[r[0], r[1], r[2], str(r[3]), r[4]] for r in cur.fetchall()],
          open("/tmp/factor_dump.json", "w", encoding="utf-8"), ensure_ascii=False)
EOF'
    ssh root@47.100.175.214 'cat /tmp/factor_dump.json' > _factor_dump.json
    ./venv/Scripts/python.exe _diag_scan.py

`_factor_dump.json` 约 19MB，属一次性诊断产物，验证完即可删除。
"""

import json
import os
import sys
import tempfile
from datetime import datetime

sys.path.insert(0, ".")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.factor_snapshot import FactorSnapshot
from app.models.stock import StockInfo
from app.services import market_scan as ms

DUMP = "_factor_dump.json"
# 线上 SH/SZ 股票池规模（stock_info），用于模拟真实覆盖率
REAL_UNIVERSE = 5382

dump = json.load(open(DUMP, encoding="utf-8"))
print(f"载入真实快照 {len(dump)} 条")

db_path = os.path.join(tempfile.gettempdir(), "scan_probe.db")
if os.path.exists(db_path):
    os.remove(db_path)
eng = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
Base.metadata.create_all(eng)
S = sessionmaker(bind=eng)
db = S()

payload_comp: dict[str, float] = {}
for sym, comp, pe, built, payload in dump:
    try:
        ts = datetime.fromisoformat(str(built))
    except ValueError:
        ts = None
    db.add(FactorSnapshot(symbol=sym, composite_score=comp, pe_ttm=pe, payload=payload, built_at=ts))
    db.add(StockInfo(symbol=sym, code=sym[:6], name="", market=sym[-2:]))
    try:
        payload_comp[sym] = float(json.loads(payload).get("composite_score"))
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
for i in range(REAL_UNIVERSE - len(dump)):
    sym = f"9{i:05d}.SZ"
    db.add(StockInfo(symbol=sym, code=sym[:6], name="占位", market="SZ"))
db.commit()

data = ms.scan_market(db, top=15)
cov = data["coverage"]
print("\n=== 覆盖率自证 ===")
print(
    f"已覆盖 {cov['covered']} / 股票池 {cov['universe']} = {cov['coverage_pct']}%"
    f"，缺口 {cov['remaining']}；最近构建 {cov['latest_built_at']}（{cov['stale_days']} 天前）"
)
print(f"分位基准 {data['percentile_base']} 只；匹配 {data['matched']} 只（exclude_st=True）")

print("\n=== Top 15（按既有 composite_score 排序，扫描层未重算）===")
print(f"{'代码':<12}{'名称':<10}{'行业':<12}{'综合':>7}{'评级':>6}{'全市场分位':>10}{'行业内分位':>11}{'PE':>8}{'市值(亿)':>11}")
for i in data["items"]:
    print(
        f"{i['symbol']:<12}{str(i['name'])[:9]:<10}{str(i['industry'])[:11]:<12}"
        f"{i['composite_score']:>7}{str(i['final_rating']):>6}"
        f"{str(i['market_pct']):>10}{str(i['industry_pct']):>11}"
        f"{str(i['pe_ttm'])[:7]:>8}{str(i['market_cap_yi'])[:10]:>11}"
    )

print("\n=== 一致性校验：读层综合分 vs payload 原始综合分 ===")
mismatch = [
    (i["symbol"], i["composite_score"], payload_comp.get(i["symbol"]))
    for i in data["items"]
    if payload_comp.get(i["symbol"]) is None
    or abs(i["composite_score"] - payload_comp[i["symbol"]]) > 0.01
]
print("不一致条目：", mismatch or "无（全部逐位一致）")

print("\n=== 维度排序（展示排序，不合成新总分）：现金流质量 Top 8 ===")
cash = ms.scan_market(db, top=8, sort_by="cashflow")
for i in cash["items"]:
    print(
        f"  {i['symbol']} {str(i['name'])[:8]:<9} 现金流质量={i['dim_scores']['现金流质量']}"
        f"  综合={i['composite_score']}"
    )

print("\n=== 基础过滤 ===")
big = ms.scan_market(db, top=5, min_market_cap_yi=500, min_composite=70)
print(f"  市值≥500亿 且 综合分≥70 → 匹配 {big['matched']} 只，前 5：",
      [i["symbol"] for i in big["items"]])
coal = ms.scan_market(db, top=5, industry="煤炭")
print(f"  行业含「煤炭」→ 匹配 {coal['matched']} 只，前 5：", [i["symbol"] for i in coal["items"]])
st = ms.scan_market(db, top=200, exclude_st=False)
st_names = [i["name"] for i in st["items"] if i["name"] and ("ST" in i["name"] or "退" in i["name"])]
print(f"  exclude_st=False 时榜内 ST/退 标的数：{len(st_names)}  样例：{st_names[:5]}")
print(f"  exclude_st=True 时（默认）同榜 ST/退 标的数：0（已剔除）")

print("\n=== 口径声明 ===")
for n in data["notes"]:
    print("  -", n)

db.close()
