from decimal import Decimal

from securo_kraken.numbers import number, parse_decimal, q6


def test_number_formats_without_exponent():
    assert number(Decimal("1E-8")) == "0.00000001"
    assert number(Decimal("12.3400")) == "12.34"
    assert number(Decimal("0.000000004")) == "0"


def test_q6_rounds_half_up():
    assert q6(Decimal("0.0000005")) == Decimal("0.000001")
    assert q6(Decimal("1.23456749")) == Decimal("1.234567")


def test_parse_decimal_rejects_floats_bools_and_nan():
    assert parse_decimal("1.5") == Decimal("1.5")
    assert parse_decimal(True) is None and parse_decimal("NaN") is None and parse_decimal(1.5) is None
