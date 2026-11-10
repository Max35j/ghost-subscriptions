"""One hand-built synthetic ledger per case. Dates are fixed; ``AS_OF`` is 2026-11-09."""
import random
import statistics
from datetime import date, timedelta

import pytest

from conftest import AS_OF, Book, anomalies, sub
from engine.detect import audit, classify_period


def run(book, **kw):
    kw.setdefault("as_of", AS_OF)
    return audit(book.txs, **kw)


def test_periodicity_bands():
    assert classify_period(7) == "weekly" and classify_period(30.5) == "monthly"
    assert classify_period(91) == "quarterly" and classify_period(365) == "yearly"
    assert classify_period(15) is None and classify_period(200) is None


def test_monthly_stable_fixed_amount_and_confidence_formula():
    b = Book().monthly(date(2025, 11, 5), 12, "CB NETFLIX.COM 4412", 13.49)
    r = run(b)
    s = sub(r, "netflix")
    assert (s["periodicity"], s["amount_kind"], s["status"], s["occurrences"]) == ("monthly", "fixed", "active", 12)
    assert s["annual_cost"] == 161.88 and s["amount_last"] == 13.49
    gaps = [(b2.date - b1.date).days for b1, b2 in zip(b.txs, b.txs[1:])]
    m = statistics.median(gaps)
    mad = statistics.median([abs(g - m) for g in gaps])
    expected = round(0.4 * 1 + 0.4 * max(0, 1 - mad / m) + 0.2 * 1, 2)
    assert s["confidence"] == expected
    assert r["totals"] == {"annual": 161.88, "monthly_average": 13.49, "count": 1, "low_confidence_count": 0}
    assert r["currency"] == "EUR" and r["period"] == {"from": "2025-11-05", "to": "2026-10-05"}
    assert not anomalies(r, "price_increase")


def test_price_increase_at_month_8():
    b = Book().monthly(date(2025, 11, 3), 12, "PRLV SEPA CANAL+", [22.99] * 7 + [25.99] * 5)
    r = run(b)
    s = sub(r, "canal")
    assert s["annual_cost"] == round(25.99 * 12, 2) and s["amount_kind"] == "fixed"
    (a,) = anomalies(r, "price_increase", "canal")
    assert a["pattern"] == "step" and a["since"] == "2026-06-03"
    assert (a["from"], a["to"], a["annual_impact"]) == (22.99, 25.99, 36.0)


def test_price_step_six_and_six():
    b = Book().monthly(date(2025, 11, 10), 12, "SPOTIFY AB", [9.99] * 6 + [12.99] * 6)
    r = run(b)
    (a,) = anomalies(r, "price_increase", "spotify")
    assert a["pattern"] == "step" and a["since"] == "2026-05-10" and a["annual_impact"] == 36.0
    assert sub(r, "spotify")["annual_cost"] == 155.88


def test_price_increase_on_latest_charge_only():
    b = Book().monthly(date(2025, 11, 10), 12, "DEEZER", [10.99] * 11 + [12.99])
    r = run(b)
    (a,) = anomalies(r, "price_increase", "deezer")
    assert a["pattern"] == "latest_charge" and a["annual_impact"] == 24.0
    assert sub(r, "deezer")["annual_cost"] == round(12.99 * 12, 2)


def test_small_or_tiny_changes_are_not_price_increases():
    b = Book().monthly(date(2025, 11, 10), 12, "ICLOUD", [0.99] * 6 + [1.29] * 6)   # +30 % but +0.30
    r = run(b)
    assert not anomalies(r, "price_increase")


def test_quarterly():
    b = Book()
    for d in (date(2025, 11, 2), date(2026, 2, 1), date(2026, 5, 3), date(2026, 8, 2), date(2026, 11, 1)):
        b.add(d, "PRLV SEPA LE MONDE", -59.0)
    s = sub(run(b), "lemonde")
    assert s["periodicity"] == "quarterly" and s["annual_cost"] == 236.0


def test_yearly():
    b = Book().every(date(2024, 3, 1), 365, 3, "AMAZON PRIME FR", 69.90)
    r = run(b)
    s = sub(r, "prime")
    assert s["periodicity"] == "yearly" and s["annual_cost"] == 69.9 and s["next_expected"] == "2027-03-01"


def test_weekly_gym():
    b = Book().every(date(2026, 4, 6), 7, 30, "CB SALLE DE SPORT 1234", 12.0)
    s = sub(run(b), "salledesport")
    assert s["periodicity"] == "weekly" and s["annual_cost"] == 624.0


def test_variable_energy_bill_is_not_a_fixed_subscription():
    amounts = [80.12, 95.40, 110.03, 121.55, 90.10, 60.20, 45.80, 40.15, 42.60, 55.33, 70.71, 88.90]
    b = Book().monthly(date(2025, 11, 6), 12, "PRLV SEPA EDF CLIENTS PARTICULIERS", amounts)
    r = run(b)
    s = sub(r, "edf")
    assert s["amount_kind"] == "variable"
    assert s["annual_cost"] == round(statistics.fmean(amounts) * 12, 2)
    assert not anomalies(r, "price_increase")
    assert any("Variable-amount" in w and "EDF" in w for w in r["warnings"])


def test_duplicate_two_days_apart():
    b = Book().monthly(date(2025, 11, 5), 12, "NETFLIX.COM", 13.49)
    b.add(date(2026, 3, 7), "CB NETFLIX.COM", -13.49)
    r = run(b)
    (a,) = anomalies(r, "duplicate", "netflix")
    assert a["date"] == "2026-03-07" and a["annual_impact"] == 13.49
    s = sub(r, "netflix")
    assert s["occurrences"] == 12 and s["annual_cost"] == 161.88


def test_same_shop_twice_is_not_a_duplicate_charge():
    b = Book().add(date(2026, 3, 5), "CB MONOPRIX", -30.80).add(date(2026, 3, 6), "CB MONOPRIX", -30.69)
    assert not anomalies(run(b), "duplicate")


def test_refund_is_neutralised():
    b = Book().monthly(date(2025, 11, 12), 12, "PRLV SEPA SPOTIFY AB", 10.99)
    b.add(date(2026, 4, 20), "CB SPOTIFY", -10.99)
    b.add(date(2026, 5, 2), "REMBOURSEMENT SPOTIFY", 10.99)
    r = run(b)
    (a,) = anomalies(r, "refund", "spotify")
    assert "refunded on 2026-05-02" in a["detail"]
    assert r["excluded"]["refunds_neutralised"] == 1
    assert sub(r, "spotify")["occurrences"] == 12


def test_internal_transfers_are_excluded():
    b = Book().monthly(date(2025, 11, 1), 12, "VIR SEPA M ALEX MARTIN EPARGNE", 300.0)
    b.add(date(2026, 6, 10), "VIR SEPA VERS LIVRET A", -500.0).add(date(2026, 6, 11), "VIR SEPA RECU COMPTE JOINT", 500.0)
    b.monthly(date(2025, 11, 5), 12, "NETFLIX", 13.49)
    r = run(b, holder_aliases=["Alex Martin"])
    assert r["excluded"]["internal_transfers"] == 14
    assert [s["key"] for s in r["subscriptions"]] == ["netflix"]
    assert r["recurring_transfers"] == []
    # Without the alias the savings transfer is shown apart, never counted as a subscription.
    r2 = run(b)
    assert r2["excluded"]["internal_transfers"] == 2
    assert [t["kind"] for t in r2["recurring_transfers"]] == ["transfer"]
    assert r2["totals"]["count"] == 1


def test_three_payments_without_periodicity_are_a_payment_plan():
    b = Book()
    for d in (date(2026, 3, 1), date(2026, 3, 16), date(2026, 4, 20)):
        b.add(d, "CB DARTY", -149.97)
    r = run(b)
    (a,) = anomalies(r, "installment", "darty")
    assert a["occurrences"] == 3 and r["subscriptions"] == []


def test_payment_plan_marker_wins_even_when_monthly():
    b = Book()
    for i, d in enumerate((date(2026, 3, 1), date(2026, 4, 1), date(2026, 5, 1))):
        b.add(d, f"CB AMAZON {i + 1}/3", -66.66)
    r = run(b)
    assert anomalies(r, "installment", "amazon") and r["subscriptions"] == []


def test_subscription_gone_for_60_days_is_probably_cancelled():
    b = Book().monthly(date(2026, 2, 10), 8, "DISNEY PLUS", 9.99)       # last charge 2026-09-10
    b.monthly(date(2025, 11, 5), 12, "NETFLIX", 13.49)
    r = run(b)
    s = sub(r, "disney")
    assert s["status"] == "probably_cancelled" and s["annual_cost"] == 0.0
    assert s["annual_cost_before_stop"] == 119.88
    (a,) = anomalies(r, "disappeared", "disney")
    assert a["annual_impact"] == -119.88
    assert r["totals"]["annual"] == 161.88 and r["totals"]["count"] == 1
    assert not anomalies(r, "overlap")                                 # cancelled services do not overlap


def test_overlap_of_two_services_of_the_same_family():
    b = Book().monthly(date(2025, 11, 17), 12, "APPLE.COM/BILL ICLOUD", 2.99)
    b.monthly(date(2025, 11, 21), 12, "DROPBOX", 11.99)
    (a,) = anomalies(run(b), "overlap")
    assert a["family"] == "cloud" and a["key"] == "dropbox,icloud" and a["annual_impact"] == 35.88
    assert "Not an error" in a["detail"]


def test_two_plans_billed_under_one_merchant_are_split():
    b = Book().monthly(date(2025, 11, 3), 12, "APPLE.COM/BILL", 0.99)
    b.monthly(date(2025, 11, 20), 12, "APPLE.COM/BILL", 4.99)
    r = run(b)
    assert sorted(s["id"] for s in r["subscriptions"]) == ["apple", "apple#2"]
    assert r["totals"]["annual"] == round((0.99 + 4.99) * 12, 2)


def test_variable_bill_is_not_mistaken_for_several_plans():
    """A cable bill between 72 and 96 falls into three 10 %-wide amount clusters; it is still one bill."""
    amounts = [91.01, 78.34, 90.44, 73.17, 84.6, 95.2, 72.9, 88.53, 93.51, 78.05, 81.3, 86.75]
    r = run(Book().monthly(date(2025, 11, 9), 12, "COMCAST CABLE COMM 800-COMCAST", amounts, currency="USD"))
    s = sub(r, "xfinity")
    assert s["id"] == "xfinity" and s["occurrences"] == 12 and s["amount_kind"] == "variable"
    assert [x["id"] for x in r["subscriptions"]] == ["xfinity"]


def test_threshold_and_low_confidence_bucket():
    b = Book().monthly(date(2025, 11, 5), 12, "CB CLUB ECHECS DU PORT", 15.0, day_jitter=(0, 4, -3, 2, 5, -4))
    r = run(b)
    c = sub(r, "club-echecs-du")["confidence"]
    assert c < 0.9                                                    # unrecognised + irregular days
    r2 = run(b, threshold=0.95)
    assert r2["subscriptions"] == [] and r2["totals"]["low_confidence_count"] == 1
    assert r2["low_confidence"][0]["confidence"] == c and r2["low_confidence_share"] == 1.0


def test_two_charges_are_only_a_lead():
    b = Book().monthly(date(2026, 8, 14), 2, "AUDIBLE FR", 9.95)
    r = run(b)
    assert r["subscriptions"] == [] and [s["key"] for s in r["leads"]] == ["audible"]


def test_everyday_shopping_is_not_a_subscription():
    rng = random.Random(7)
    b = Book()
    d = date(2025, 11, 1)
    while d < date(2026, 11, 1):
        for _ in range(rng.randint(0, 2)):
            b.add(d, rng.choice(["CB CARREFOUR", "CB BOULANGERIE PAUL", "CB PHARMACIE"]), -round(rng.uniform(3, 80), 2))
        d += timedelta(days=1)
    r = run(b)
    assert r["subscriptions"] == [] and not anomalies(r, "duplicate")


def test_currencies_are_never_mixed_without_rates():
    b = Book().monthly(date(2025, 11, 5), 12, "NETFLIX", 13.49)
    b.monthly(date(2025, 11, 7), 12, "OPENAI CHATGPT", 20.0, currency="USD")
    r = run(b)
    assert set(r["totals_by_currency"]) == {"EUR", "USD"}
    assert r["totals_by_currency"]["USD"]["annual"] == 240.0
    assert r["totals_converted"] is None and any("never converted" in w for w in r["warnings"])
    r2 = run(b, fx_rates={"base": "EUR", "USD": 0.9})                 # 1 USD = 0.90 EUR, given by the user
    assert r2["currency"] == "EUR"
    assert r2["totals_converted"] == {"currency": "EUR", "annual": round(161.88 + 240 * 0.9, 2),
                                      "monthly_average": round((161.88 + 216) / 12, 2), "rates_used": {"USD": 0.9}}


def test_period_filter_and_stale_statement_warning():
    b = Book().monthly(date(2025, 1, 5), 12, "NETFLIX", 13.49)
    r = run(b)
    assert any("ends" in w for w in r["warnings"]) and sub(r, "netflix")["status"] == "probably_cancelled"
    r2 = run(b, as_of=date(2025, 12, 10))
    assert sub(r2, "netflix")["status"] == "active"
    assert run(b, period_from=date(2030, 1, 1))["error"]


@pytest.mark.parametrize("new_charges", [2, 3, 4])
def test_recent_price_step_is_reported_once(new_charges):
    """A rise seen on the last 2-4 charges is one alert, not a 'step' plus a 'latest charge'."""
    b = Book().monthly(date(2025, 11, 10), 12, "SPOTIFY AB", [9.99] * (12 - new_charges) + [12.99] * new_charges)
    r = run(b)
    (a,) = anomalies(r, "price_increase", "spotify")
    assert a["pattern"] == "step" and a["annual_impact"] == 36.0
    assert sub(r, "spotify")["annual_cost"] == 155.88
