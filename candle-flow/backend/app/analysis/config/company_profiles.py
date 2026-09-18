"""公司画像配置 — 将个股特殊因子从模块代码中抽离，集中管理。

新增公司只需在 _PROFILES 中添加一条记录，无需修改分析逻辑代码。
每个 profile 的 key 为公司名称或代码，value 为 dict，包含以下可选字段：

    match_names    : list[str]  — 名称匹配关键词
    match_symbols  : list[str]  — 代码匹配
    match_industry : list[str]  — 行业关键词（辅助条件，部分场景需要）

    # ── 成长性模块 (growth) ──
    asset_injection    : dict  — 资产注入/并表事件
    second_curve       : dict  — 第二增长曲线
    resource_injection : dict  — 资源注入预期

    # ── 风险模块 (risk) ──
    credit_quality_note : str          — 应收客户信用质量说明
    risk_warnings       : list[dict]   — 公司特有额外风险警告
    cyclical            : bool         — 强制标记为周期/资源股
    industry_tags       : list[str]    — 行业标签（触发行业风险块）
        可选值: "optical"(光模块) | "baijiu"(白酒)

    # ── 盈利模块 (profitability) ──
    cyclical            : bool         — 盈利能力评分按周期股权重

    # ── 商业模式 (跨模块) ──
    business_model      : str          — "distribution"(分销/贸易) 等，
                                         影响 WACC 口径、ROIC 归一化与可比公司
    cashflow_profile    : str          — 现金流评判档位，如 "distribution"
                                         （低净利率高周转、扩张期经营现金流为负是常态）
    merger_consolidation: dict         — 并购并表事件（增速含并表贡献，非纯有机增长）
    comps_peers         : list[str]    — 显式可比公司清单（覆盖按行业字符串的自动筛选）
"""

from __future__ import annotations
from typing import Any


_PROFILES: dict[str, dict[str, Any]] = {
    # ═══════════════════════════════════════════════════════════════
    # 中国神华 (601088) — 煤炭龙头，2026年重大资产并表
    # ═══════════════════════════════════════════════════════════════
    "中国神华": {
        "match_names": ["神华"],
        "match_symbols": ["601088"],

        # 成长性：资产注入并表
        "asset_injection": {
            "indicators": [
                {
                    "name": "外延式增长(资产注入)",
                    "value": 5.2,
                    "score": 82.0,
                    "level": "good",
                    "trend": "up",
                    "weight": 1.5,
                    "period": "2026-2028业绩承诺",
                    "comment": (
                        "2026年3月完成收购12家核心资产（交易对价约1336亿，30%股份+70%现金）："
                        "煤炭产量+56.6%、可采储量+97.7%的规模跃升；"
                        "业绩承诺2026-2028归母净利29.6/45.5/66.4亿，"
                        "对应2026约+5.2%外延增量；需关注商誉减值与整合协同"
                    ),
                },
                {
                    "name": "并表后规模增速",
                    "value": 28.6,
                    "score": 84.0,
                    "level": "good",
                    "trend": "up",
                    "weight": 1.8,
                    "period": "2026经营目标",
                    "comment": (
                        "资产注入后2026年经营目标全面上调：商品煤产量5.134亿吨(+55.5%)、"
                        "发电量2881亿千瓦时(+28.8%)、营收目标3600亿(+28.6%)；"
                        "历史3年CAGR为负但并表后规模跃升，纯CAGR口径会系统性低估"
                    ),
                },
                {
                    "name": "并表跃升调整",
                    "value": 28.6,
                    "score": 86.0,
                    "level": "excellent",
                    "trend": "up",
                    "weight": 3.0,
                    "period": "2026并表元年",
                    "comment": (
                        "2026年3月完成12家核心资产并表（交易对价~1336亿），"
                        "商品煤+55.5%/发电量+28.8%/营收+28.6%规模跃升；"
                        "业绩承诺2026-2028净利29.6/45.5/66.4亿，"
                        "纯CAGR口径无法捕捉并表级规模跃升，额外给予并表溢价+8分"
                    ),
                },
            ],
            "warnings": [
                (
                    "并表跃升：2026年3月完成12家资产并表，经营目标全面上调"
                    "（煤+55.5%/电+28.8%/营收+28.6%），历史CAGR口径系统性低估，"
                    "需跟踪业绩承诺兑现及整合协同"
                ),
            ],
        },

        # 风险：应收客户信用质量
        "credit_quality_note": "客户信用质量高（五大发电集团央企为主），坏账风险可控",

        # 风险：资产注入并表特有警告
        "risk_warnings": [
            {
                "text": (
                    "资产注入并表（观察）：交易对价约1336亿（30%股份+70%现金），"
                    "注入资产2026-2028业绩承诺净利29.6/45.5/66.4亿；"
                    "关注商誉减值风险及整合协同效应"
                ),
            },
        ],

        # 盈利：强制周期股
        "cyclical": True,
    },

    # ═══════════════════════════════════════════════════════════════
    # 云天化 (600096) — 磷化工龙头，新能源材料第二曲线 + 磷矿注入
    # ═══════════════════════════════════════════════════════════════
    "云天化": {
        "match_names": ["云天化"],
        "match_symbols": ["600096"],

        # 成长性：第二曲线（磷酸铁/磷酸铁锂）
        "second_curve": {
            "name": "磷酸铁/磷酸铁锂",
            "status": "capacity_ramp",
            "detail": (
                "10万吨磷酸铁已投产满产满销，2026H1销量5.04万吨超去年全年七成；"
                "20万吨磷酸铁+15万吨磷酸铁锂2026Q4-2027年集中释放，"
                "规划总产能50万吨；与当升科技合资（云天化控股51%），"
                "绑定宁德时代等头部客户"
            ),
            "score": 88.0,
            "bonus": 12,
        },

        # 成长性：资源注入预期（镇雄磷矿）
        "resource_injection": {
            "name": "镇雄磷矿",
            "reserve_billion_tons": 24.38,
            "existing_reserve": "近8亿吨",
            "total_after_injection": "超32亿吨",
            "domestic_share": "近90%",
            "cost_self": "200-300元/吨",
            "cost_market": "700-1200元/吨",
            "cost_advantage": "50%+",
            "commitment": "集团承诺取得采矿证后3年内优先注入上市公司",
            "score": 82.0,
            "bonus": 5,
            "industry_required": ("磷", "矿", "化工"),  # 需行业匹配才生效
        },

        # 风险：公司特有额外风险
        "risk_warnings": [
            {
                "text": (
                    "镇雄磷矿注入进度风险：碗厂磷矿(24.38亿吨)2026年12月开工，"
                    "建设期约5年，取得采矿证后3年内注入，时点存在不确定性"
                ),
                "score_deduct": 2,
            },
        ],

        # 盈利：强制周期股
        "cyclical": True,
    },

    # ═══════════════════════════════════════════════════════════════
    # 光模块/光通信公司 — 行业标签触发光模块风险块
    # ═══════════════════════════════════════════════════════════════
    "中际旭创": {
        "match_names": ["中际旭创"],
        "match_symbols": ["300308"],
        "industry_tags": ["optical"],
    },
    "新易盛": {
        "match_names": ["新易盛"],
        "industry_tags": ["optical"],
    },
    "天孚通信": {
        "match_names": ["天孚通信"],
        "industry_tags": ["optical"],
    },
    "光迅科技": {
        "match_names": ["光迅科技"],
        "industry_tags": ["optical"],
    },

    # ═══════════════════════════════════════════════════════════════
    # 贵州茅台 — 行业标签触发白酒风险块
    # ═══════════════════════════════════════════════════════════════
    "贵州茅台": {
        "match_names": ["茅台"],
        "match_symbols": ["600519"],
        "industry_tags": ["baijiu"],
    },

    # ═══════════════════════════════════════════════════════════════
    # 商络电子 (300975) — 被动元件分销，2026 起并表广州立功电子
    # ═══════════════════════════════════════════════════════════════
    "商络电子": {
        "match_names": ["商络电子"],
        "match_symbols": ["300975"],

        # 商业模式：电子元器件分销 —— 低净利率、高周转、营运资本占用大
        "business_model": "distribution",
        "cashflow_profile": "distribution",

        # 并购并表：折减因子只做「定性标注 + 风险提示」。
        # 若掌握并表贡献占比，填 consolidation_contribution_pct（0~100）即可自动折减增速；
        # 未填时不臆造数字，仅提示「增速含并表贡献，非纯有机增长」。
        "merger_consolidation": {
            "target": "广州立功电子科技有限公司",
            "since": "2026",
            "note": (
                "自 2026 年起纳入合并报表。2026 中报归母净利同比大幅增长中，"
                "并表贡献与有机增长未拆分，直接按披露同比外推会高估可持续增速"
            ),
            "keyword": "并表",
        },

        # 可比公司：分销同行（与自身商业模式一致），
        # 避免拿法拉电子/顺络电子等被动元件制造商来锚定分销商估值
        "comps_peers": [
            "300184",  # 力源信息
            "001298",  # 好上好
            "301099",  # 雅创电子
            "300131",  # 英唐智控
            "300493",  # 润欣科技
            "000062",  # 深圳华强
            "001287",  # 中电港
            "300475",  # 香农芯创
        ],
    },
}


# ── 商业模式识别（跨模块复用） ────────────────────────────────────

# 分销/贸易/经销：低净利率、高周转，营运资本（应收+存货）随规模同比例扩张
DISTRIBUTION_INDUSTRY_KEYS = ("分销", "贸易", "经销", "商贸", "供应链", "批发")

# A 股电子元器件分销同业（供 comps 在无显式清单时使用）
ELECTRONIC_DISTRIBUTION_PEERS = [
    "000062",  # 深圳华强
    "001287",  # 中电港
    "001298",  # 好上好
    "300131",  # 英唐智控
    "300184",  # 力源信息
    "300475",  # 香农芯创
    "300493",  # 润欣科技
    "301099",  # 雅创电子
]
ELECTRONIC_DISTRIBUTION_PEERS_NORM = frozenset(ELECTRONIC_DISTRIBUTION_PEERS)

# ═══════════════════════════════════════════════════════════════════════
# 分销/贸易同业基准（相对评分锚点）
#
# 为什么需要：分销是「低净利率、高周转」模式，用制造业绝对阈值衡量会
# 把整个行业判成不合格 —— 实测 7 家 A 股电子元器件分销商 2026 中报，
# ROIC 三年均值全部落在 1.5%~3.8%，无一达到 WACC≈10%；经营现金流/净利
# 7 家中 6 家为负。这是商业模式使然，不是个体经营失败。
#
# 因此分销模式改用「同业中位数」为锚点：达到行业中位 ≈ 中性（55~60 分），
# 领先加分、落后扣分。这样既尊重行业特性，又保留区分度
# （行业最差的中电港 ROIC 1.89% 仍会被判低分，不会被一并豁免）。
#
# 样本（2026 中报）：
#   深圳华强 000062 / 中电港 001287 / 力源信息 300184 / 润欣科技 300493
#   好上好 001298 / 雅创电子 301099 / 商络电子 300975
#
#   指标        同业原始分布                                      中位数
#   ROIC 单期   1.89 2.92 4.23 4.33 4.92 5.08 6.36               4.33
#   ROIC 3年均  1.50 2.46 2.75 3.32 3.67 3.78 3.78               3.32
#   净利率      0.43 0.91 1.86 1.91 1.91 1.94 3.54               1.91
#   ROE         4.34 4.71 4.76 5.35 6.66 9.51 13.20              5.35
#   毛利率      2.85 5.28 7.71 9.58 10.35 12.30 15.21            9.58
#   OCF/净利    -5.20 -4.17 -3.66 -2.25 -2.14 -0.53 1.42         -2.25
#   资产负债率  39.69 44.43 61.42 70.29 72.55 75.76 88.65         61.42
# ═══════════════════════════════════════════════════════════════════════
DISTRIBUTION_BENCHMARKS: dict[str, float | str] = {
    "sampled_period": "2026H1",
    "sample_size": 7,
    "roic_ttm_pct": 4.33,
    "roic_3y_pct": 3.32,
    "net_margin_pct": 1.91,
    "roe_pct": 5.35,
    "gross_margin_pct": 9.58,
    "ocf_to_profit": -2.25,
    "debt_ratio_pct": 61.42,
}


def distribution_benchmarks() -> dict[str, float | str]:
    """分销/贸易同业基准（相对评分锚点），返回副本以免调用方误改。"""
    return dict(DISTRIBUTION_BENCHMARKS)


# ═══════════════════════════════════════════════════════════════════════
# 银行同业基准（偿债口径锚点）
#
# 为什么需要：银行的负债主要是客户存款，资产负债率天然落在 90%~94%，
# 而流动比率/速动比率对存款类机构也没有制造业含义。套用制造业/轻资产
# 阈值（>75% 即判危险）会把整个银行业判成 E 级——实测招商银行 90.18%
# 被打 25 分并提示「资产负债率偏高，偿债压力较大」，而它恰恰是样本里
# 杠杆最低的一家。
#
# 样本（2026 中报，20 家 A 股银行）：
#   招商 / 工商 / 建设 / 农业 / 中国 / 浦发 / 兴业 / 交通 / 宁波 / 光大 /
#   民生 / 中信 / 江苏 / 邮储 / 上海 / 南京 / 江阴 / 杭州 / 常熟 / 张家港
#
#   资产负债率%      90.18  91.62  91.94  92.96  93.95   (min/p25/中位/p75/max)
#   权益占比%(=100-)  9.82   8.38   8.06   7.04   6.05
#
# 银行偿债评分改用「权益/总资产（资本缓冲）」相对同业中位为锚：
# 达到中位 ≈ 中性偏保守，缓冲更厚的加分、更薄的扣分。保留区分度
# （样本内缓冲最薄的邮储银行仍会被判低分，不会被一并豁免）。
#
# 注意：本数据源不提供不良贷款率 / 拨备覆盖率 / 资本充足率，
# 因此银行偿债评分只反映杠杆水平，不等同于资产质量结论。
# ═══════════════════════════════════════════════════════════════════════
BANK_INDUSTRY_KEYS = ("银行",)

BANK_BENCHMARKS: dict[str, float | str] = {
    "sampled_period": "2026H1",
    "sample_size": 20,
    "debt_ratio_pct": 91.94,
    "equity_ratio_pct": 8.06,
}


def bank_benchmarks() -> dict[str, float | str]:
    """银行同业基准（杠杆锚点），返回副本以免调用方误改。"""
    return dict(BANK_BENCHMARKS)


def _norm_code(symbol: str) -> str:
    """归一化代码：'000062.SZ' / 'sz000062' / '000062' → '000062'。"""
    s = str(symbol or "").strip().upper()
    for suf in (".SH", ".SZ", ".BJ", ".SS"):
        if s.endswith(suf):
            s = s[: -len(suf)]
    for pre in ("SH", "SZ", "BJ", "SS"):
        if s.startswith(pre) and len(s) > len(pre):
            s = s[len(pre):]
    digits = "".join(ch for ch in s if ch.isdigit())
    return digits.zfill(6) if digits and len(digits) <= 6 else digits


def business_model_of(name: str = "", symbol: str = "", industry: str = "") -> str:
    """识别商业模式：profile 显式声明 > 显式同业名单 > 行业关键词推断。"""
    profile = get_company_profile(name, symbol)
    explicit = str(profile.get("business_model") or "")
    if explicit:
        return explicit
    # 显式列入电子元器件分销同业名单的公司，本身就是分销商。
    # 东财行业分类把它们归入「其他电子Ⅱ」「元件」等泛行业，
    # 靠行业关键词匹配不到，会出现「同是分销商却套用制造业阈值」的不一致
    # （例：商络电子 OCF/净利 -289.7% 得 52 分，深圳华强 -204.0% 却只有 34 分）。
    code = _norm_code(symbol)
    if code and code in ELECTRONIC_DISTRIBUTION_PEERS_NORM:
        return "distribution"
    ind = str(industry or "")
    if any(k in ind for k in DISTRIBUTION_INDUSTRY_KEYS):
        return "distribution"
    if any(k in ind for k in BANK_INDUSTRY_KEYS):
        return "bank"
    return ""


def is_bank(name: str = "", symbol: str = "", industry: str = "") -> bool:
    """是否银行（存款类金融机构）。

    只认「银行」，不含证券/保险/多元金融/非银金融：它们没有不良贷款率
    与拨备覆盖率，套用银行监管口径会把原本正常的偿债评分（实测中信证券
    63.1 C、中国平安 72.2 B）压成无信息量的常数（~52 分），是比现状更差
    的结果。券商的负债主要来自卖出回购与客户保证金，与存款类机构不同源。
    """
    return business_model_of(name, symbol, industry) == "bank"


def is_distribution(name: str = "", symbol: str = "", industry: str = "") -> bool:
    """是否分销/贸易类商业模式（用于现金流阈值、WACC、ROIC 归一化）。"""
    return business_model_of(name, symbol, industry) == "distribution"


def peers_for(name: str = "", symbol: str = "", industry: str = "") -> list[str]:
    """显式可比公司清单；无配置时，分销类公司回退到电子元器件分销同业。"""
    profile = get_company_profile(name, symbol)
    explicit = profile.get("comps_peers")
    if explicit:
        return [str(s) for s in explicit]
    if business_model_of(name, symbol, industry) == "distribution":
        code = _norm_code(symbol)
        return [s for s in ELECTRONIC_DISTRIBUTION_PEERS if s != code]
    return []


def get_merger_consolidation(name: str = "", symbol: str = "") -> dict[str, Any]:
    """并购并表事件配置（无则空 dict）。"""
    profile = get_company_profile(name, symbol)
    conf = profile.get("merger_consolidation")
    return conf if isinstance(conf, dict) else {}


# ── 公开接口 ──────────────────────────────────────────────────────


def get_company_profile(name: str, symbol: str = "") -> dict[str, Any]:
    """根据公司名/代码查找画像配置，返回匹配 profile 或空 dict。

    匹配优先级：精确名称 > 关键词包含 > 代码包含。
    """
    name_str = str(name or "")
    symbol_str = str(symbol or "")

    for _key, profile in _PROFILES.items():
        if any(n in name_str for n in profile.get("match_names", [])):
            return profile
        if any(s in symbol_str for s in profile.get("match_symbols", [])):
            return profile
    return {}
