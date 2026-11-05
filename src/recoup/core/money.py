"""Money helpers. All amounts are Decimal; floats never touch a dollar figure."""

from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")
ZERO = Decimal("0")


def to_money(value: Decimal | int | str) -> Decimal:
    """Round to cents, half up, the way payers print remits."""
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def parse_money(text: str) -> Decimal:
    """Parse '$1,234.50' or '1234.5' into a Decimal rounded to cents."""
    cleaned = text.strip().replace("$", "").replace(",", "")
    return to_money(Decimal(cleaned))
