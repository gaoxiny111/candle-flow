import re

SYMBOL_WITH_MARKET = re.compile(r"^(\d{6})\.(SH|SZ)$", re.I)
CODE_ONLY = re.compile(r"^\d{6}$")
FUTURE_WITH_MARKET = re.compile(r"^([A-Z]{1,2}(?:0|\d{3,4}))\.FUT$", re.I)
FUTURE_CODE = re.compile(r"^([A-Za-z]{1,2})(0|\d{3,4})$")

# 期货主力连续（新浪代码 +0）-> 中文名
FUTURE_NAMES: dict[str, str] = {
    "RB0.FUT": "螺纹钢连续",
    "HC0.FUT": "热卷连续",
    "I0.FUT": "铁矿石连续",
    "J0.FUT": "焦炭连续",
    "JM0.FUT": "焦煤连续",
    "CU0.FUT": "沪铜连续",
    "AL0.FUT": "沪铝连续",
    "ZN0.FUT": "沪锌连续",
    "NI0.FUT": "沪镍连续",
    "SN0.FUT": "沪锡连续",
    "PB0.FUT": "沪铅连续",
    "AU0.FUT": "沪金连续",
    "AG0.FUT": "沪银连续",
    "RU0.FUT": "橡胶连续",
    "BU0.FUT": "沥青连续",
    "FU0.FUT": "燃油连续",
    "SP0.FUT": "纸浆连续",
    "SS0.FUT": "不锈钢连续",
    "AO0.FUT": "氧化铝连续",
    "SC0.FUT": "原油连续",
    "IF0.FUT": "沪深300股指连续",
    "IH0.FUT": "上证50股指连续",
    "IC0.FUT": "中证500股指连续",
    "IM0.FUT": "中证1000股指连续",
    "T0.FUT": "十年国债连续",
    "TF0.FUT": "五年国债连续",
    "TS0.FUT": "二年国债连续",
    "M0.FUT": "豆粕连续",
    "Y0.FUT": "豆油连续",
    "P0.FUT": "棕榈油连续",
    "C0.FUT": "玉米连续",
    "CS0.FUT": "淀粉连续",
    "A0.FUT": "豆一连续",
    "L0.FUT": "塑料连续",
    "V0.FUT": "PVC连续",
    "PP0.FUT": "聚丙烯连续",
    "EG0.FUT": "乙二醇连续",
    "EB0.FUT": "苯乙烯连续",
    "PG0.FUT": "LPG连续",
    "LH0.FUT": "生猪连续",
    "TA0.FUT": "PTA连续",
    "MA0.FUT": "甲醇连续",
    "CF0.FUT": "郑棉连续",
    "SR0.FUT": "白糖连续",
    "RM0.FUT": "菜粕连续",
    "OI0.FUT": "菜油连续",
    "FG0.FUT": "玻璃连续",
    "SA0.FUT": "纯碱连续",
    "UR0.FUT": "尿素连续",
    "ZC0.FUT": "动力煤连续",
    "AP0.FUT": "苹果连续",
    "CJ0.FUT": "红枣连续",
    "SF0.FUT": "硅铁连续",
    "SM0.FUT": "锰硅连续",
    "SI0.FUT": "工业硅连续",
    "LC0.FUT": "碳酸锂连续",
}

FUTURE_ALIASES: dict[str, str] = {
    "螺纹": "RB0.FUT",
    "螺纹钢": "RB0.FUT",
    "热卷": "HC0.FUT",
    "热轧卷板": "HC0.FUT",
    "铁矿": "I0.FUT",
    "铁矿石": "I0.FUT",
    "焦炭": "J0.FUT",
    "焦煤": "JM0.FUT",
    "沪铜": "CU0.FUT",
    "沪铝": "AL0.FUT",
    "沪锌": "ZN0.FUT",
    "沪镍": "NI0.FUT",
    "沪金": "AU0.FUT",
    "黄金": "AU0.FUT",
    "沪银": "AG0.FUT",
    "白银": "AG0.FUT",
    "橡胶": "RU0.FUT",
    "沥青": "BU0.FUT",
    "原油": "SC0.FUT",
    "股指": "IF0.FUT",
    "期指": "IF0.FUT",
    "沪深300股指": "IF0.FUT",
    "豆粕": "M0.FUT",
    "豆油": "Y0.FUT",
    "棕榈油": "P0.FUT",
    "玉米": "C0.FUT",
    "PTA": "TA0.FUT",
    "甲醇": "MA0.FUT",
    "郑棉": "CF0.FUT",
    "棉花": "CF0.FUT",
    "白糖": "SR0.FUT",
    "玻璃": "FG0.FUT",
    "纯碱": "SA0.FUT",
    "工业硅": "SI0.FUT",
    "碳酸锂": "LC0.FUT",
    "生猪": "LH0.FUT",
}

# 常用简称 -> 标准代码（便于搜索）
NAME_ALIASES: dict[str, str] = {
    "神华": "601088.SH",
    "中国神华": "601088.SH",
    "伊泰B股": "900948.SH",
    "伊泰b股": "900948.SH",
    "茅台": "600519.SH",
    "平安": "601318.SH",
    "五粮液": "000858.SZ",
    "平安银行": "000001.SZ",
    **FUTURE_ALIASES,
}

# 股票 / 期货代码 -> 中文名称
SYMBOL_NAMES: dict[str, str] = {
    "000001.SZ": "平安银行",
    "600519.SH": "贵州茅台",
    "000858.SZ": "五粮液",
    "601318.SH": "中国平安",
    "601088.SH": "中国神华",
    "900948.SH": "伊泰B股",
    **FUTURE_NAMES,
}

_FUTURE_ALIAS_UPPER = {key.upper(): value for key, value in FUTURE_ALIASES.items()}


class SymbolError(ValueError):
    pass


def symbol_name(symbol: str) -> str:
    upper = symbol.strip().upper()
    return SYMBOL_NAMES.get(upper, SYMBOL_NAMES.get(symbol, ""))


def futures_symbol(code: str) -> str | None:
    raw = (code or "").strip()
    if not raw:
        return None
    m = FUTURE_WITH_MARKET.match(raw.upper())
    if m:
        return f"{m.group(1).upper()}.FUT"
    if FUTURE_CODE.fullmatch(raw):
        return f"{raw.upper()}.FUT"
    return None


def normalize_symbol(symbol: str) -> str:
    """Normalize to 000001.SZ / 600519.SH / RB0.FUT"""
    raw = (symbol or "").strip()
    if not raw:
        raise SymbolError("代码不能为空")

    if raw in NAME_ALIASES:
        return NAME_ALIASES[raw]
    mapped = _FUTURE_ALIAS_UPPER.get(raw.upper())
    if mapped:
        return mapped

    upper = raw.upper()

    m = SYMBOL_WITH_MARKET.match(upper)
    if m:
        return f"{m.group(1)}.{m.group(2).upper()}"

    fut = futures_symbol(upper)
    if fut:
        return fut

    if CODE_ONLY.match(upper):
        code = upper
        if code.startswith(("6", "9")):
            return f"{code}.SH"
        if code.startswith(("0", "3")):
            return f"{code}.SZ"
        raise SymbolError(f"无法识别市场: {code}，请使用完整格式如 {code}.SH")

    raise SymbolError(
        f"无效代码: {raw}，请使用股票如 000001.SZ、600519，或期货如 螺纹钢、RB0、IF0"
    )


def parse_symbol(symbol: str) -> tuple[str, str]:
    """Return (code, market) e.g. ('000001', 'sz') or ('RB0', 'fut')"""
    normalized = normalize_symbol(symbol)
    code, market = normalized.rsplit(".", 1)
    return code, market.lower()


def is_future(symbol: str) -> bool:
    raw = (symbol or "").strip()
    if not raw:
        return False
    try:
        _, market = parse_symbol(raw)
        return market == "fut"
    except SymbolError:
        return futures_symbol(raw) is not None


def futures_sina_code(symbol: str) -> str:
    code, market = parse_symbol(symbol)
    if market != "fut":
        raise SymbolError(f"不是期货代码: {symbol}")
    return code


def is_b_share(symbol: str) -> bool:
    if is_future(symbol):
        return False
    code, _ = parse_symbol(symbol)
    return code.startswith("900") or code.startswith("200")
