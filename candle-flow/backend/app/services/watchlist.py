import json

MAX_WATCHLIST = 50


def parse_watchlist(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in data:
        if not isinstance(item, str):
            continue
        symbol = item.strip().upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        out.append(symbol)
    return out


def dump_watchlist(symbols: list[str]) -> str:
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in symbols:
        if not isinstance(item, str):
            continue
        symbol = item.strip().upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        cleaned.append(symbol)
    return json.dumps(cleaned, ensure_ascii=False)


def add_symbol(symbols: list[str], symbol: str, max_size: int = MAX_WATCHLIST) -> list[str]:
    item = symbol.strip().upper()
    if not item:
        return symbols
    if item in {s.upper() for s in symbols}:
        return list(symbols)
    if len(symbols) >= max_size:
        raise ValueError(f"关注列表最多 {max_size} 只")
    return [*symbols, item]


def remove_symbol(symbols: list[str], symbol: str) -> list[str]:
    item = symbol.strip().upper()
    return [s for s in symbols if s.upper() != item]
