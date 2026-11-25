"""Unit-тести для app/engines/fx.py."""

import math

import pytest

from app.engines import fx, policy


# ---------------------------------------------------------------------------
# mid_rate
# ---------------------------------------------------------------------------

class TestMidRate:
    def test_same_currency_is_one(self):
        for code in policy.RATES_TO_EUR:
            assert fx.mid_rate(code, code) == pytest.approx(1.0)

    def test_eur_to_usd(self):
        assert fx.mid_rate("EUR", "USD") == pytest.approx(1.0 / 0.92)

    def test_usd_to_eur(self):
        assert fx.mid_rate("USD", "EUR") == pytest.approx(0.92)

    def test_cross_rate_gbp_to_pln(self):
        assert fx.mid_rate("GBP", "PLN") == pytest.approx(1.17 / 0.23)

    @pytest.mark.parametrize("a,b", [("USD", "GBP"), ("PLN", "CHF"), ("UAH", "EUR")])
    def test_rates_are_reciprocal(self, a, b):
        assert fx.mid_rate(a, b) * fx.mid_rate(b, a) == pytest.approx(1.0)

    def test_unknown_from_currency_raises(self):
        with pytest.raises(ValueError, match="Unknown currency: XXX"):
            fx.mid_rate("XXX", "EUR")

    def test_unknown_to_currency_raises(self):
        with pytest.raises(ValueError, match="Unknown currency: YYY"):
            fx.mid_rate("EUR", "YYY")


    def test_currency_code_is_case_sensitive(self):
        with pytest.raises(ValueError):
            fx.mid_rate("eur", "USD")



# ---------------------------------------------------------------------------
# quote — валідація
# ---------------------------------------------------------------------------

class TestQuoteValidation:
    def test_unknown_tier_raises(self):
        with pytest.raises(ValueError, match="Unknown tier: gold"):
            fx.quote(100, "EUR", "USD", "gold")





# ---------------------------------------------------------------------------
# quote — у межах безкоштовного ліміту
# ---------------------------------------------------------------------------

class TestQuoteWithinAllowance:

    def test_basic_fields(self):
        q = fx.quote(100, "EUR", "USD", "tier1", allowance_used_eur=50.0)
        assert q.from_currency == "EUR"
        assert q.to_currency == "USD"
        assert q.amount == 100
        assert q.tier == "tier1"
        assert q.mid_rate == pytest.approx(1.0 / 0.92)
        assert q.allowance_total_eur == 500
        assert q.allowance_used_before_eur == 50.0

    def test_no_spread_within_allowance(self):
        q = fx.quote(100, "EUR", "USD", "tier1")
        assert q.allowance_applied is True
        assert q.spread_pct == 0.0
        assert q.spread_amount == 0.0
        assert q.gross_amount == pytest.approx(100 / 0.92)
        assert q.final_amount == pytest.approx(q.gross_amount)

    def test_exact_boundary_is_within(self):
        # 0 + 500 <= 500
        q = fx.quote(500, "EUR", "USD", "tier1")
        assert q.allowance_applied is True
        assert q.spread_pct == 0.0

    def test_exact_boundary_with_prior_usage(self):
        q = fx.quote(200, "EUR", "GBP", "tier1", allowance_used_eur=300.0)
        assert q.allowance_applied is True
        assert q.spread_amount == 0.0


    def test_allowance_measured_in_eur_not_source_currency(self):
        # 10000 UAH = 210 EUR -> у межах ліміту tier1 (500)
        q = fx.quote(10000, "UAH", "EUR", "tier1")
        assert q.allowance_applied is True
        assert q.gross_amount == pytest.approx(210.0)
        assert q.final_amount == pytest.approx(210.0)

    def test_source_amount_above_allowance_but_eur_below(self):
        # 540 USD = 496.8 EUR <= 500
        q = fx.quote(540, "USD", "EUR", "tier1")
        assert q.allowance_applied is True

    @pytest.mark.parametrize("tier,amount", [("tier1", 500), ("tier2", 1000), ("tier3", 5000)])
    def test_each_tier_allowance_boundary(self, tier, amount):
        q = fx.quote(amount, "EUR", "USD", tier)
        assert q.allowance_applied is True
        assert q.allowance_total_eur == amount

    def test_zero_amount_within(self):
        q = fx.quote(0, "EUR", "USD", "tier1")
        assert q.allowance_applied is True
        assert q.gross_amount == 0
        assert q.final_amount == 0


# ---------------------------------------------------------------------------
# quote — понад ліміт
# ---------------------------------------------------------------------------

class TestQuoteOverAllowance:
    def test_just_over_boundary(self):
        q = fx.quote(500, "EUR", "USD", "tier1", allowance_used_eur=0.01)
        assert q.allowance_applied is False
        assert q.spread_pct == 1.5

    @pytest.mark.parametrize("tier", policy.TIERS)
    def test_default_spread_per_tier(self, tier):
        amount = policy.FX_FREE_MONTHLY_ALLOWANCE_EUR[tier] + 1000
        q = fx.quote(amount, "EUR", "USD", tier)
        assert q.allowance_applied is False
        assert q.spread_pct == {"tier1": 1.5, "tier2": 0.9, "tier3": 0.5}[tier]

    def test_full_spread_amounts(self):
        q = fx.quote(1000, "EUR", "USD", "tier1")
        gross = 1000 / 0.92
        assert q.gross_amount == pytest.approx(gross)
        assert q.spread_amount == pytest.approx(gross * 0.015)
        assert q.final_amount == pytest.approx(gross * 0.985)

    def test_whole_amount_charged_without_partial(self):
        # використано 300, переказ 400 -> весь переказ зі спредом
        q = fx.quote(400, "EUR", "EUR", "tier1", allowance_used_eur=300.0)
        assert q.allowance_applied is False
        assert q.spread_amount == pytest.approx(6.0)
        assert q.final_amount == pytest.approx(394.0)



    def test_final_equals_gross_minus_spread(self):
        q = fx.quote(12345.67, "GBP", "PLN", "tier2", allowance_used_eur=900)
        assert q.final_amount == pytest.approx(q.gross_amount - q.spread_amount)

    def test_used_already_exceeds_allowance(self):
        q = fx.quote(10, "EUR", "EUR", "tier1", allowance_used_eur=600)
        assert q.allowance_applied is False
        assert q.spread_amount == pytest.approx(0.15)






# ---------------------------------------------------------------------------
# FxQuote.as_dict
# ---------------------------------------------------------------------------

class TestAsDict:
    def test_contains_all_fields(self):
        d = fx.quote(100, "EUR", "USD", "tier1").as_dict()
        assert set(d) == {
            "from_currency", "to_currency", "amount", "mid_rate", "tier",
            "spread_pct", "allowance_total_eur", "allowance_used_before_eur",
            "allowance_applied", "gross_amount", "spread_amount", "final_amount",
        }

    def test_floats_rounded_to_6_places(self):
        d = fx.quote(100.0, "EUR", "USD", "tier1").as_dict()
        assert d["mid_rate"] == round(1.0 / 0.92, 6)
        assert d["gross_amount"] == round(100.0 / 0.92, 6)



    def test_does_not_mutate_instance(self):
        q = fx.quote(100.0, "EUR", "USD", "tier1")
        before = q.mid_rate
        q.as_dict()
        assert q.mid_rate == before


# ---------------------------------------------------------------------------
# transfer_fee
# ---------------------------------------------------------------------------

class TestTransferFee:
    def test_internal_is_free(self):
        r = fx.transfer_fee(1000, "internal")
        assert r["total_fee_eur"] == 0.0

    def test_sepa_flat(self):
        r = fx.transfer_fee(1000, "sepa")
        assert r["total_fee_eur"] == 0.35
        assert r["flat_fee_eur"] == 0.35
        assert r["percent_fee"] == 0.0

    def test_swift_flat_plus_percent(self):
        r = fx.transfer_fee(1000, "swift")
        assert r["total_fee_eur"] == 18.0

    def test_card_percent(self):
        assert fx.transfer_fee(100, "card")["total_fee_eur"] == 1.2

    def test_rounding_to_cents(self):
        # 10.04 * 1.2% = 0.12048 -> 0.12
        assert fx.transfer_fee(10.04, "card")["total_fee_eur"] == 0.12

    def test_zero_amount(self):
        assert fx.transfer_fee(0, "swift")["total_fee_eur"] == 15.0

    def test_result_structure(self):
        r = fx.transfer_fee(250.5, "swift")
        assert r == {
            "transfer_type": "swift",
            "amount_eur": 250.5,
            "flat_fee_eur": 15.0,
            "percent_fee": 0.3,
            "total_fee_eur": round(15.0 + 250.5 * 0.003, 2),
        }


    def test_unknown_type_raises(self):
        with pytest.raises(ValueError, match="Unknown transfer type: wire"):
            fx.transfer_fee(100, "wire")

