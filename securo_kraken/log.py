"""Deduplicated stderr logging for a single sync run.

Callers must pass fixed text or identifiers only. Never put credentials,
amounts, ids or tickers into a message.
"""

import re
import sys

_MAX_NAME = 40
_seen: set[str] = set()


def reset() -> None:
    """Forget what has been logged. Call at the start of each sync."""
    _seen.clear()


def unknown(kind: str, name: object) -> None:
    """Report an unrecognised value once per sync, with the name sanitised."""
    safe = re.sub(r"[^A-Z0-9_]", "", str(name).upper())[:_MAX_NAME] or "?"
    key = f"unknown:{kind}:{safe}"
    if key in _seen:
        return
    _seen.add(key)
    print(f"kraken: unknown {kind}: {safe}", file=sys.stderr)


def warning(message: str) -> None:
    """Print a fixed warning once per sync."""
    key = f"warning:{message}"
    if key in _seen:
        return
    _seen.add(key)
    print(f"kraken: {message}", file=sys.stderr)
