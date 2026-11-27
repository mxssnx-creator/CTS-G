"""Stable symbol groups for venue and simulated exposure limits."""
from zlib import crc32


GROUPS = {
    "majors": {"SOL-USDT", "XRP-USDT", "ETC-USDT"},
    "meme": {"DOGE-USDT", "1000PEPE-USDT", "KAS-USDT", "JUP-USDT"},
    "l1": {"APT-USDT", "HYPE-USDT", "TRX-USDT"},
    "defi": {"ENA-USDT", "LDO-USDT", "JTO-USDT", "ZRO-USDT", "COMP-USDT", "ORDI-USDT"},
}
_BY_SYMBOL = {symbol: group for group, symbols in GROUPS.items() for symbol in symbols}


def symbol_group(symbol: str) -> str:
    group = _BY_SYMBOL.get(symbol)
    return group if group is not None else f"u{crc32(symbol.encode()) % 8}"
