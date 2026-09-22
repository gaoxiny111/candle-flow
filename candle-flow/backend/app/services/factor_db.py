"""盘后预构建因子库：将 run_full_analysis 结果持久化到 SQLite，盘中扫描零 API 调用。"""

from __future__ import annotations

import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text

from app.database import SessionLocal
from app.models.factor_snapshot import FactorSnapshot
from app.models.stock import StockInfo

logger = logging.getLogger(__name__)

# 非披露期 TTL：30 天内的快照视为有效
_DEFAULT_TTL_DAYS = 30
# 披露窗口期 TTL：3 天内视为有效（财报数据频繁更新）
_DISCLOSURE_TTL_DAYS = 3
# 批量构建参数
_BUILD_WORKERS = 8
_BUILD_PER_STOCK_TIMEOUT = 20.0
# 单次批跑时间预算：全市场 ≈5400 只、单只 ≈13s、8 并发 ≈2.4h，
# 默认 1800s 会在 ~1100 只处截断（非披露期只补缺失，故可多日渐进补齐）。
# 需一次性补齐时可用环境变量放宽，或调 /fundamentals/factors/rebuild?budget_sec=。
_BUILD_BATCH_DEADLINE = float(os.environ.get("FACTOR_BUILD_DEADLINE_SEC", "1800"))


def _is_in_disclosure_window(now: datetime | None = None) -> bool:
    """判断当前是否处于财报披露窗口期。

    年报/一季报: 1-4 月
    半年报: 7-8 月
    三季报: 10 月
    """
    today = now or datetime.now()
    month = today.month
    return month in (1, 2, 3, 4, 7, 8, 10)


def _ttl_days() -> int:
    """根据是否处于披露窗口返回 TTL。"""
    return _DISCLOSURE_TTL_DAYS if _is_in_disclosure_window() else _DEFAULT_TTL_DAYS


def is_stale(symbol: str, *, ttl_days: int | None = None) -> bool:
    """判断指定股票的因子快照是否过期或不存在。

    返回 True 表示需要重新构建（或数据不存在）。
    """
    ttl = ttl_days if ttl_days is not None else _ttl_days()
    db = SessionLocal()
    try:
        row = db.query(FactorSnapshot).filter(FactorSnapshot.symbol == symbol).first()
        if row is None:
            return True
        if row.built_at is None:
            return True
        # timezone-aware 比较
        built = row.built_at
        if built.tzinfo is None:
            built = built.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        return (now - built) > timedelta(days=ttl)
    except Exception:
        logger.debug("is_stale check failed for %s", symbol, exc_info=True)
        return True
    finally:
        db.close()


def get(symbol: str) -> dict[str, Any] | None:
    """从因子库读取单只股票的完整分析结果。缺失返回 None。"""
    db = SessionLocal()
    try:
        row = db.query(FactorSnapshot).filter(FactorSnapshot.symbol == symbol).first()
        if row is None:
            return None
        return json.loads(row.payload)
    except Exception:
        logger.debug("factor_db.get failed for %s", symbol, exc_info=True)
        return None
    finally:
        db.close()


def get_many(symbols: list[str]) -> dict[str, dict[str, Any]]:
    """批量读取多只股票的因子快照。返回 {symbol: report_dict}。"""
    if not symbols:
        return {}
    db = SessionLocal()
    try:
        rows = db.query(FactorSnapshot).filter(FactorSnapshot.symbol.in_(symbols)).all()
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            try:
                result[row.symbol] = json.loads(row.payload)
            except (json.JSONDecodeError, TypeError):
                continue
        return result
    except Exception:
        logger.debug("factor_db.get_many failed", exc_info=True)
        return {}
    finally:
        db.close()


def upsert(symbol: str, report: dict[str, Any]) -> None:
    """写入或更新单条因子快照。"""
    db = SessionLocal()
    try:
        # 打上口径版本戳：build_all(force=False) 与盘后任务据此识别"口径已变、
        # 分数需重算"的存量快照（见 SCORING_VERSION 注释）。
        report = {**report, "scoring_version": SCORING_VERSION}
        payload = json.dumps(report, ensure_ascii=False, default=str)
        composite = report.get("composite_score")
        market = report.get("market") or {}
        pe_ttm = market.get("pe_ttm")
        row = db.query(FactorSnapshot).filter(FactorSnapshot.symbol == symbol).first()
        if row is None:
            row = FactorSnapshot(symbol=symbol)
            db.add(row)
        row.payload = payload
        row.composite_score = float(composite) if composite is not None else None
        row.pe_ttm = float(pe_ttm) if pe_ttm is not None else None
        row.built_at = datetime.now(timezone.utc)
        db.commit()
    except Exception:
        logger.debug("factor_db.upsert failed for %s", symbol, exc_info=True)
        db.rollback()
    finally:
        db.close()


def _get_all_symbols() -> list[str]:
    """获取全部 SH/SZ 主板股票代码。"""
    db = SessionLocal()
    try:
        rows = db.query(StockInfo).filter(StockInfo.market.in_(("SH", "SZ"))).all()
        return [r.symbol for r in rows]
    finally:
        db.close()


def _get_stale_symbols(
    existing_symbols: set[str],
    all_symbols: list[str],
    outdated_symbols: set[str] | None = None,
) -> list[str]:
    """找出缺失、过期或 payload 缺必需字段的股票。

    非披露期只补缺失条目；``outdated_symbols`` 来自 schema 守卫，
    把「已有行但缺新字段」的存量快照也纳入重建，否则新增字段永不回填。
    """
    in_window = _is_in_disclosure_window()
    ordered = sorted(all_symbols)
    if in_window:
        # 披露窗口期：全部重建
        return ordered
    # 非披露期：补缺失的 + 缺必需字段的
    outdated = outdated_symbols or set()
    return [s for s in ordered if s not in existing_symbols or s in outdated]


# 快照 payload 必需字段（schema 守卫）。
#
# 增量构建若只看「主键是否已存在」，新增字段在存量行上就永不回填：
# 2026-09-20 的 profit_yoy 即如此丢失（5211 条存量快照缺该键 →
# market_scan.technical_overlay 的 PEG 恒 None → 「强买入」档不可达）。
# 以后给 run_full_analysis 返回值加字段时，把键名追加到这里即可自愈。
_REQUIRED_SNAPSHOT_KEYS: tuple[str, ...] = ("profit_yoy", "scoring_version")

# 打分数值口径版本。**任何会改变 composite_score / 模块分 / 风控判定的改动
# 都必须 bump 这个值**，否则盘后 build_all()（非 force）会把这些口径变更后
# 的分数当成"新鲜快照"跳过，要等到下一个披露窗口才生效 —— 期间榜单与详情
# 页会出现同一只票两个分数（详情页实时重算、榜单读快照）。
# 例：2026-09-21 修掉「ST 已摘帽仍强制 E」「衰退股按边际改善加分」两处口径，
# 若不 bump，摘帽股在榜单上会一直停在 25 分 E 级。
# .3：拆除 pe_distorted 自证循环 + turnaround 增加 PB 底部前提
#     + PEG 分母禁止退化为单期同比（三者都直接改估值分，必须全量重建）。
# .4：周期陷阱折减（低 PE 幻觉：PE低分位+PB高分位+高ROE → 估值合理性降 12 分）
#     —— 直接改估值分，必须全量重建。左侧抄底防守与买点信号阶段化是**读层**
#     改动（共振索引 / 榜单叠加），不进快照，但 .4 重建会顺带刷新它们的输入。
# .5：① 现金流「微利稀释」封顶（每股经营现金流<0.3 且 净利率<5% → 模块分≤50）
#        —— 直接改现金流模块分，必须全量重建；
#     ② 估值软化（PE>80 → signal"合理"）增加分位交叉校验（分位≥90 不再洗白）
#        —— 直接改估值分口径。
#     ③ 分类准入门槛（_admission_gate）是**读层**改动（榜单判档），不进快照。
# .6：① 分类准入闸门修两个洞（纯读层，但需 bump 以逼一次干净全量重建，
#        扫掉仍停留在 09-20 的 3640 份旧口径快照）：
#        - 周期型分支原本只判 cycle_trap 且该参数**恒为 False** → 894 只强周期股
#          无条件免检，混入 143 只「PE分位≥80 且 ROE<6」（泸天化99.3/0.5、
#          招商蛇口100.0/0.73、华菱钢铁99.2/4.77），与金牛化工同病。
#          现补「估值↔盈利矛盾」判据（高估值分位+弱 ROE），仍豁免真·谷底反转
#          （PE 低分位时 ROE 为负照常放行）。
#        - 周转率兜底（<0.15）对**金融业结构性豁免**：银行/券商周转天然
#          0.02~0.08，实测误杀 42 家银行 + 49 家券商 + 20 家多元金融
#          （宁波银行 0.02、华泰证券 0.033）。豁免后仍过矛盾判据，非免检。
#        实测全库 5254 只：新增拦截 160 只、纠正误杀 152 只（净 PASS 3893→3885）。
# .7：① 估值软化阶梯收紧（engine.py）—— 原 `PE>80 → signal="合理"` 只看绝对 PE，
#        只用了单边分位上限 90；现改为「绝对估值档位决定分位门槛」的双条件交叉：
#        PE 80~150 要求分位 <80、PE >150 要求分位 <70、PB >8 要求分位 <80，
#        否则改判「偏高」不再软化。直接改估值分，必须全量重建。
#        （靶点：603099 长白山 PE 80.6/分位 86.1 曾被洗成「合理」；
#          600722 金牛化工 PE 193.5/分位 89.8 同病。真成长 PE 95/分位 60 不受影响。）
#     ② 风险事件「减持」漏判修复（major_risk_events.py）—— 中文标题语序多变，
#        连续子串匹配实测 15 种真实写法只命中 5 种（漏 67%）：
#        「关于控股股东一致行动人减持股份计划公告」（立霸股份 2026-08-25）
#        含「减持股份计划」，却既不含连续的「减持计划」也不含顺序相反的
#        「股份减持」→ 零命中，公告已扫到（notice_scanned=150）但 events 为空。
#        现加 `all_of` 语义共现（("减持",) + ("计划"/"预披露"/"进展"/"减持股份")），
#        召回 5/15 → 13/15（余 2 例是「计划已结束」应走释放通道）；
#        并把「届满/到期/实施完毕」交给释放通道，释放只抵消更早的事件。
# .8（2026-09-22）：① 现金流模块新增「分红含金量」指标（cashflow.py）——
#        拆解分红来源，识别「靠一次性收益/存量现金支撑的伪红利」：
#        判据 = 近3年累计非经常性损益占比 ≥30% 或 近3年累计FCF/当年分红 <1。
#        口径用 **3 年累计**而非单年（单年是盲区：立霸 603519 一次性收益发生在
#        2023 年，2024/2025 单年非经常占比仅 4%/3%，而 3 年累计达 57.5%）。
#        直接改现金流模块分，必须全量重建。
#     ② 营运效率模块新增「应收周转天数变化率」+「存货/营收比值」（efficiency.py）
#        → 存货增速持续高于营收增速时输出利润侵蚀预警。同期对同期口径由
#        financials._ops_efficiency_from_sina 统一产出。模块分改变（效率不参与
#        综合加权，但榜单展示分改变），需重建。
#     ③ 风险模块新增「减持窗口期倒计时」+「大宗交易折价率」扣分
#        （major_risk_events.fetch_reduce_window / fetch_recent_block_trades）
#        → 风险分改变，经风险乘数传导至 composite，必须全量重建。
#     ④ 买点信号新增「减持窗口期闸门」：窗口内 strong_buy 封顶为 watch
#        （market_confluence_service._detect_buy_signal，只改档位不改分数）。
SCORING_VERSION = "2026.09.22.8"


def _outdated_snapshot_symbols() -> set[str]:
    """返回 payload 缺必需字段或口径版本落后的 symbol 集合。

    判定依据是**键是否存在**（``json_type`` 为 NULL = 路径不存在），
    不是值是否为 null —— 合法的空值（如无同比数据）不应触发反复重建。
    SQLite 无 JSON1 时回退 Python 解析 payload。
    """
    if not _REQUIRED_SNAPSHOT_KEYS:
        return set()
    db = SessionLocal()
    try:
        try:
            conds = " OR ".join(
                f"json_type(payload, '$.{k}') IS NULL" for k in _REQUIRED_SNAPSHOT_KEYS
            )
            conds += f" OR json_extract(payload, '$.scoring_version') IS NOT {json.dumps(SCORING_VERSION)}"
            sql = text(f"SELECT symbol FROM factor_snapshots WHERE {conds}")
            return {row[0] for row in db.execute(sql)}
        except Exception:
            logger.warning(
                "factor_db schema guard SQL failed; falling back to python parse",
                exc_info=True,
            )
        outdated: set[str] = set()
        for sym, payload in db.query(
            FactorSnapshot.symbol, FactorSnapshot.payload
        ).all():
            try:
                data = json.loads(payload)
            except (json.JSONDecodeError, TypeError):
                outdated.add(sym)
                continue
            if (
                not isinstance(data, dict)
                or any(k not in data for k in _REQUIRED_SNAPSHOT_KEYS)
                or data.get("scoring_version") != SCORING_VERSION
            ):
                outdated.add(sym)
        return outdated
    except Exception:
        logger.warning("factor_db schema guard failed", exc_info=True)
        return set()
    finally:
        db.close()


def build_all(
    *,
    force: bool = False,
    budget_sec: float | None = None,
    max_symbols: int | None = None,
) -> dict[str, Any]:
    """批量构建全市场因子库。

    Args:
        force: True 时无视披露窗口判断，全量重建。
        budget_sec: 本次批处理时间预算（秒）。缺省用 _BUILD_BATCH_DEADLINE。
            全市场约 5400 只、单只 ≈13s、8 并发 → 跑满约需 2.5h，
            默认 1800s 会在 ~1100 只处截断（非披露期只补缺失，故可多日渐进补齐）。
        max_symbols: 本次最多构建多少只（配合 budget_sec 做可控增量补齐）。

    Returns:
        {"built","failed","skipped","duration_sec","budget_sec",
         "truncated","coverage":{covered,universe,remaining,coverage_pct}}
    """
    from app.analysis.engine import analyze_symbol_full

    t0 = time.time()
    all_symbols = _get_all_symbols()
    if not all_symbols:
        logger.warning("factor_db.build_all: no symbols found in StockInfo")
        return {
            "built": 0,
            "failed": 0,
            "skipped": 0,
            "duration_sec": 0.0,
            "budget_sec": budget_sec or _BUILD_BATCH_DEADLINE,
            "truncated": False,
            "coverage": _coverage(len(all_symbols)),
        }

    # 确定需要构建的股票列表
    if force:
        to_build = all_symbols
    else:
        # 查已有快照的 symbol 集合
        db = SessionLocal()
        try:
            existing = {r.symbol for r in db.query(FactorSnapshot.symbol).all()}
        finally:
            db.close()
        # schema 守卫：已存在但 payload 缺必需字段的存量快照也要重建
        outdated = _outdated_snapshot_symbols()
        if outdated:
            logger.info(
                "factor_db.build_all: %d snapshots outdated by schema guard %s",
                len(outdated),
                _REQUIRED_SNAPSHOT_KEYS,
            )
        to_build = _get_stale_symbols(existing, all_symbols, outdated)

    if max_symbols is not None and max_symbols > 0:
        to_build = to_build[: int(max_symbols)]

    deadline = float(budget_sec) if budget_sec is not None else _BUILD_BATCH_DEADLINE
    total = len(to_build)
    skipped = len(all_symbols) - total
    logger.info(
        "factor_db.build_all: %d to build, %d skipped (force=%s, budget=%.0fs)",
        total,
        skipped,
        force,
        deadline,
    )

    if not to_build:
        coverage = _coverage(len(all_symbols))
        return {
            "built": 0,
            "failed": 0,
            "skipped": skipped,
            "duration_sec": time.time() - t0,
            "budget_sec": deadline,
            "truncated": False,
            "coverage": coverage,
        }

    built = 0
    failed = 0
    truncated = False
    batch_start = time.time()

    def _process_one(sym: str) -> bool:
        try:
            result = analyze_symbol_full(db=None, symbol=sym, use_cache=True)
            if result.get("composite_score") is not None:
                upsert(sym, result)
                return True
            return False
        except Exception:
            logger.debug("factor build failed for %s", sym, exc_info=True)
            return False

    workers = min(_BUILD_WORKERS, max(1, len(to_build)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_process_one, sym): sym for sym in to_build}
        for fut in as_completed(futures):
            # 批级 deadline
            if time.time() - batch_start > deadline:
                remaining = sum(1 for f in futures if not f.done())
                if remaining:
                    logger.warning(
                        "factor build BATCH DEADLINE %.0fs reached, %d remaining",
                        deadline,
                        remaining,
                    )
                    truncated = True
                    for f in futures:
                        if not f.done():
                            f.cancel()
                break
            sym = futures[fut]
            try:
                if fut.result(timeout=_BUILD_PER_STOCK_TIMEOUT):
                    built += 1
                else:
                    failed += 1
            except Exception:
                failed += 1
                logger.debug("factor build timeout/error for %s", sym)

    duration = time.time() - t0
    coverage = _coverage(len(all_symbols))
    stats = {
        "built": built,
        "failed": failed,
        "skipped": skipped,
        "duration_sec": round(duration, 1),
        "budget_sec": deadline,
        "truncated": truncated,
        "coverage": coverage,
    }
    logger.info(
        "factor_db.build_all done: %s | coverage %s/%s (%.1f%%)",
        stats,
        coverage["covered"],
        coverage["universe"],
        coverage["coverage_pct"],
    )
    return stats


def _coverage(universe: int) -> dict[str, Any]:
    """覆盖率自证：已构建快照数 / SH·SZ 股票总数。"""
    db = SessionLocal()
    try:
        covered = int(
            db.query(FactorSnapshot)
            .filter(FactorSnapshot.composite_score.isnot(None))
            .count()
        )
    except Exception:
        logger.debug("coverage count failed", exc_info=True)
        covered = 0
    finally:
        db.close()
    return {
        "covered": covered,
        "universe": universe,
        "remaining": max(0, universe - covered),
        "coverage_pct": round(covered / universe * 100.0, 1) if universe else 0.0,
    }
