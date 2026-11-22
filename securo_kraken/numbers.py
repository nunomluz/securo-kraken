"""Decimal parsing and formatting helpers."""

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation


def parse_decimal(value: object) -> Decimal | None:
    """Return value as a finite Decimal, or None if it is not a usable number."""
    if isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, str):
        try:
            result = Decimal(value)
        except InvalidOperation:
            return None
    else:
        return None
    if not result.is_finite():
        return None
    return result


def number(value: Decimal) -> str:
    """Format a Decimal as a fixed-point string with up to 8 decimal places."""
    quantized = value.quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)
    if quantized == 0:
        return "0"
    text = format(quantized, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def q6(value: Decimal) -> Decimal:
    """Round a Decimal half-up to 6 decimal places."""
    return value.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
