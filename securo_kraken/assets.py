"""Kraken asset families and price routes. Pure: no HTTP."""

from . import log

FIAT = frozenset({"USD", "EUR", "GBP", "CAD", "CHF", "AUD", "JPY", "AED", "KRW"})
VALUATIONS = ("EUR", "USD", "GBP", "CAD", "CHF", "AUD", "JPY")
ALIASES = {"XBT": "BTC", "XDG": "DOGE", "SETH": "ETH", "ETH2": "ETH", "FLOWH": "FLOW"}

_STAKING_SUFFIXES = frozenset({"S", "M", "F", "B", "P"})
_HOLD = "HOLD"
_FEE_CREDIT = "KFEE"


def family(code: str, altnames: dict[str, str]) -> str | None:
    """Map a Kraken asset code to its family, or None for the fee credit."""
    if code == _FEE_CREDIT:
        return None
    stem, dot, suffix = code.partition(".")
    staking = False
    if dot:
        if suffix == _HOLD:
            pass
        elif suffix in _STAKING_SUFFIXES:
            staking = True
        else:
            log.unknown("asset_suffix", suffix)
            return code
    if staking and stem != "ETH2":
        stem = stem.rstrip("0123456789") or stem
    name = altnames.get(stem, stem)
    return ALIASES.get(name, name)


def is_fiat(family_code: str) -> bool:
    return family_code in FIAT


def altnames_from(assets_result: dict) -> dict[str, str]:
    """Kraken asset code -> altname, from the public Assets result."""
    return {
        code: info["altname"]
        for code, info in assets_result.items()
        if isinstance(info, dict) and info.get("altname")
    }


def index_pairs(asset_pairs_result: dict, altnames: dict[str, str]) -> dict[str, str]:
    """Map "BASE/QUOTE" (families) to the Kraken pair key, whatever the status."""
    pairs: dict[str, str] = {}
    for key, info in asset_pairs_result.items():
        if not isinstance(info, dict):
            continue
        base, quote = info.get("base"), info.get("quote")
        if not base or not quote:
            continue
        base_family = family(base, altnames)
        quote_family = family(quote, altnames)
        if base_family and quote_family:
            pairs[f"{base_family}/{quote_family}"] = key
    return pairs


def _leg(a: str, b: str, pairs: dict[str, str]) -> tuple[str, bool] | None:
    if f"{a}/{b}" in pairs:
        return pairs[f"{a}/{b}"], False
    if f"{b}/{a}" in pairs:
        return pairs[f"{b}/{a}"], True
    return None


def route(coin: str, valuation: str, pairs: dict[str, str]) -> list[tuple[str, bool]] | None:
    """Legs (pair_key, invert) converting one coin to the valuation currency."""
    if coin == valuation:
        return []
    direct = _leg(coin, valuation, pairs)
    if direct:
        return [direct]
    for mid in ("USD", "BTC"):
        if mid in (coin, valuation):
            continue
        first = _leg(coin, mid, pairs)
        second = _leg(mid, valuation, pairs)
        if first and second:
            return [first, second]
    return None
