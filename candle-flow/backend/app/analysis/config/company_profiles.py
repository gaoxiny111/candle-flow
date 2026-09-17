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
}


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
