import pytest

from app.engines import fx

RATE_CARD_SPREAD_PCT = {"tier1": 1.5, "tier2": 0.9, "tier3": 0.5}
RATE_CARD_ALLOWANCE_EUR = {"tier1": 500, "tier2": 1000, "tier3": 5000}
EUR_PER_USD = 0.92


def expected_eur_to_usd(amount_eur, tier, spread_applies):
    gross = amount_eur / EUR_PER_USD
    spread_pct = RATE_CARD_SPREAD_PCT[tier] if spread_applies else 0.0
    spread = gross * spread_pct / 100
    return spread_pct, gross - spread


def test_tier1_pays_1_5_percent_once_allowance_is_used_up():
    q = fx.quote(1000, "EUR", "USD", "tier1", allowance_used_eur=500)
    spread_pct, final = expected_eur_to_usd(1000, "tier1", True)
    assert q.spread_pct == spread_pct
    assert q.final_amount == pytest.approx(final)


def test_tier2_pays_0_9_percent_above_allowance():
    q = fx.quote(2000, "EUR", "USD", "tier2", allowance_used_eur=0)
    spread_pct, final = expected_eur_to_usd(2000, "tier2", True)
    assert q.spread_pct == spread_pct
    assert q.final_amount == pytest.approx(final)


def test_tier3_pays_0_5_percent_above_allowance():
    q = fx.quote(10000, "EUR", "USD", "tier3", allowance_used_eur=0)
    spread_pct, final = expected_eur_to_usd(10000, "tier3", True)
    assert q.spread_pct == spread_pct
    assert q.final_amount == pytest.approx(final)


def test_conversion_inside_allowance_is_free():
    q = fx.quote(100, "EUR", "USD", "tier1", allowance_used_eur=0)
    assert q.spread_pct == 0.0
    assert q.final_amount == pytest.approx(100 / EUR_PER_USD)


def test_conversion_ending_exactly_on_tier2_allowance_is_free():
    q = fx.quote(600, "EUR", "USD", "tier2", allowance_used_eur=400)
    assert q.allowance_applied is True
    assert q.spread_pct == 0.0


def test_crossing_allowance_charges_spread_on_whole_amount():
    q = fx.quote(601, "EUR", "USD", "tier2", allowance_used_eur=400)
    spread_pct, final = expected_eur_to_usd(601, "tier2", True)
    assert q.allowance_applied is False
    assert q.final_amount == pytest.approx(final)


def test_unknown_tier_is_rejected():
    with pytest.raises(ValueError):
        fx.quote(100, "EUR", "USD", "tier9")
