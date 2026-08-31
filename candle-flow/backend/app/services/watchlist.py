import json
import re
import uuid
from dataclasses import dataclass, field

MAX_WATCHLIST = 50
DEFAULT_GROUP_ID = "default"
DEFAULT_GROUP_NAME = "默认"
MAX_GROUPS = 20


@dataclass
class WatchGroup:
    id: str
    name: str
    symbols: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "symbols": list(self.symbols)}


def _norm_symbol(raw: object) -> str | None:
    if not isinstance(raw, str):
        return None
    symbol = raw.strip().upper()
    return symbol or None


def _norm_group_name(raw: object) -> str | None:
    if not isinstance(raw, str):
        return None
    name = re.sub(r"\s+", " ", raw.strip())
    if not name or len(name) > 20:
        return None
    return name


def _new_group_id() -> str:
    return uuid.uuid4().hex[:10]


def _dedupe_symbols(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        sym = _norm_symbol(item)
        if not sym or sym in seen:
            continue
        seen.add(sym)
        out.append(sym)
    return out


def default_groups(symbols: list[str] | None = None) -> list[WatchGroup]:
    return [
        WatchGroup(
            id=DEFAULT_GROUP_ID,
            name=DEFAULT_GROUP_NAME,
            symbols=_dedupe_symbols(symbols or []),
        )
    ]


def flatten_groups(groups: list[WatchGroup]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for g in groups:
        for sym in g.symbols:
            if sym in seen:
                continue
            seen.add(sym)
            out.append(sym)
    return out


def parse_watchlist_groups(raw: str | None) -> list[WatchGroup]:
    """Parse stored JSON into groups. Legacy flat list → one default group."""
    if not raw:
        return default_groups()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return default_groups()

    if isinstance(data, list):
        return default_groups([s for s in data if isinstance(s, str)])

    if not isinstance(data, dict):
        return default_groups()

    groups_raw = data.get("groups")
    if not isinstance(groups_raw, list) or not groups_raw:
        # Tolerate {symbols: [...]} without groups
        symbols = data.get("symbols")
        if isinstance(symbols, list):
            return default_groups([s for s in symbols if isinstance(s, str)])
        return default_groups()

    used_ids: set[str] = set()
    used_syms: set[str] = set()
    groups: list[WatchGroup] = []
    for item in groups_raw:
        if not isinstance(item, dict):
            continue
        name = _norm_group_name(item.get("name")) or DEFAULT_GROUP_NAME
        gid = str(item.get("id") or "").strip() or _new_group_id()
        if gid in used_ids:
            gid = _new_group_id()
        used_ids.add(gid)
        syms: list[str] = []
        for s in item.get("symbols") or []:
            sym = _norm_symbol(s)
            if not sym or sym in used_syms:
                continue
            used_syms.add(sym)
            syms.append(sym)
        groups.append(WatchGroup(id=gid, name=name, symbols=syms))
        if len(groups) >= MAX_GROUPS:
            break

    if not groups:
        return default_groups()

    # Ensure a default bucket exists for UI consistency
    if not any(g.id == DEFAULT_GROUP_ID for g in groups):
        if groups[0].name == DEFAULT_GROUP_NAME:
            groups[0].id = DEFAULT_GROUP_ID
        else:
            groups.insert(0, WatchGroup(id=DEFAULT_GROUP_ID, name=DEFAULT_GROUP_NAME, symbols=[]))
    return groups


def parse_watchlist(raw: str | None) -> list[str]:
    return flatten_groups(parse_watchlist_groups(raw))


def dump_watchlist_groups(groups: list[WatchGroup]) -> str:
    cleaned = parse_watchlist_groups(json.dumps({"groups": [g.to_dict() for g in groups]}, ensure_ascii=False))
    return json.dumps({"groups": [g.to_dict() for g in cleaned]}, ensure_ascii=False)


def dump_watchlist(symbols: list[str]) -> str:
    """Persist flat symbols as a single default group (v2 shape)."""
    return dump_watchlist_groups(default_groups(symbols))


def find_group(groups: list[WatchGroup], group_id: str | None = None, group_name: str | None = None) -> WatchGroup:
    if group_id:
        for g in groups:
            if g.id == group_id:
                return g
    if group_name:
        name = _norm_group_name(group_name)
        if name:
            for g in groups:
                if g.name == name:
                    return g
    for g in groups:
        if g.id == DEFAULT_GROUP_ID:
            return g
    return groups[0]


def add_symbol(
    symbols: list[str],
    symbol: str,
    max_size: int = MAX_WATCHLIST,
) -> list[str]:
    item = _norm_symbol(symbol)
    if not item:
        return list(symbols)
    if item in {s.upper() for s in symbols}:
        return list(symbols)
    if len(symbols) >= max_size:
        raise ValueError(f"关注列表最多 {max_size} 只")
    return [*symbols, item]


def add_symbol_to_groups(
    groups: list[WatchGroup],
    symbol: str,
    *,
    group_id: str | None = None,
    group_name: str | None = None,
    max_size: int = MAX_WATCHLIST,
) -> list[WatchGroup]:
    item = _norm_symbol(symbol)
    if not item:
        return groups
    flat = flatten_groups(groups)
    if item in flat:
        return groups
    if len(flat) >= max_size:
        raise ValueError(f"关注列表最多 {max_size} 只")
    target = find_group(groups, group_id=group_id, group_name=group_name)
    target.symbols.append(item)
    return groups


def remove_symbol(symbols: list[str], symbol: str) -> list[str]:
    item = _norm_symbol(symbol)
    if not item:
        return list(symbols)
    return [s for s in symbols if s.upper() != item]


def remove_symbol_from_groups(groups: list[WatchGroup], symbol: str) -> list[WatchGroup]:
    item = _norm_symbol(symbol)
    if not item:
        return groups
    for g in groups:
        g.symbols = [s for s in g.symbols if s != item]
    return groups


def move_symbol(groups: list[WatchGroup], symbol: str, group_id: str) -> list[WatchGroup]:
    item = _norm_symbol(symbol)
    if not item:
        return groups
    target = find_group(groups, group_id=group_id)
    # Remove from all, then append to target (keeps uniqueness)
    for g in groups:
        g.symbols = [s for s in g.symbols if s != item]
    target.symbols.append(item)
    return groups


def create_group(groups: list[WatchGroup], name: str) -> list[WatchGroup]:
    cleaned = _norm_group_name(name)
    if not cleaned:
        raise ValueError("分组名称无效（1–20 字）")
    if any(g.name == cleaned for g in groups):
        raise ValueError("已有同名分组")
    if len(groups) >= MAX_GROUPS:
        raise ValueError(f"最多 {MAX_GROUPS} 个分组")
    groups.append(WatchGroup(id=_new_group_id(), name=cleaned, symbols=[]))
    return groups


def rename_group(groups: list[WatchGroup], group_id: str, name: str) -> list[WatchGroup]:
    cleaned = _norm_group_name(name)
    if not cleaned:
        raise ValueError("分组名称无效（1–20 字）")
    target = None
    for g in groups:
        if g.id == group_id:
            target = g
            break
    if target is None:
        raise ValueError("分组不存在")
    if any(g.name == cleaned and g.id != group_id for g in groups):
        raise ValueError("已有同名分组")
    target.name = cleaned
    return groups


def delete_group(groups: list[WatchGroup], group_id: str) -> list[WatchGroup]:
    if group_id == DEFAULT_GROUP_ID:
        raise ValueError("默认分组不能删除")
    target = next((g for g in groups if g.id == group_id), None)
    if target is None:
        raise ValueError("分组不存在")
    default = find_group(groups, group_id=DEFAULT_GROUP_ID)
    for sym in target.symbols:
        if sym not in default.symbols:
            default.symbols.append(sym)
    return [g for g in groups if g.id != group_id]


def replace_symbols(groups: list[WatchGroup], symbols: list[str], max_size: int = MAX_WATCHLIST) -> list[WatchGroup]:
    """Replace flat symbol set; keep existing group membership where possible."""
    cleaned = _dedupe_symbols(symbols)
    if len(cleaned) > max_size:
        raise ValueError(f"关注列表最多 {max_size} 只")
    keep = set(cleaned)
    for g in groups:
        g.symbols = [s for s in g.symbols if s in keep]
    present = set(flatten_groups(groups))
    default = find_group(groups, group_id=DEFAULT_GROUP_ID)
    for sym in cleaned:
        if sym not in present:
            default.symbols.append(sym)
            present.add(sym)
    return groups
