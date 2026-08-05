from decimal import ROUND_HALF_UP, Decimal


def round_composite_score(value: Decimal) -> float:
    """
    M10-E01: the ONLY place a composite score is ever rounded. Every
    intermediate value feeding into it (redistributed weights, per-layer
    contributions) must stay full-precision Decimal until this final step -
    callers must never round anything before calling this.
    """
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
