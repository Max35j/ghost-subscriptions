"""Recurring-payment detection, anomalies and annualised cost.

Pure functions over a list of ``Tx``. No I/O, no Hermes. ``audit()`` returns the
stable dict that the ``statement_audit`` tool serialises.
"""
from __future__ import annotations

import statistics
from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import date, timedelta

from .normalize import fold, load_merchants

PERIODS = {                     # name: (min median days, max median days, charges per year)
    "weekly": (5, 9, 52),
    "monthly": (26, 35, 12),
    "quarterly": (80, 100, 4),
    "yearly": (350, 380, 1),
}
MIN_INTERVAL_REGULARITY = {"monthly": 0.6, "weekly": 0.5, "quarterly": 0.5, "yearly": 0.5}
FIXED_CV = 0.05
UNRESOLVED_FACTOR = 0.9         # alias table did not recognise the merchant
UNRESOLVED_VARIABLE_FACTOR = 0.8  # ...and the amount changes every time
MAX_VARIABLE_CV = 0.45          # energy bills stay well below; random shopping does not
CANCELLED_GRACE_DAYS = 10
DUPLICATE_DAYS = 4              # strictly fewer than 4 days apart
DUPLICATE_TOLERANCE = 0.01
REFUND_DAYS = 45
REFUND_TOLERANCE = 0.05
TRANSFER_DAYS = 3
INSTALLMENT_SPAN_DAYS = 90
INSTALLMENT_MIN_AMOUNT = 20.0   # three identical small card payments are usually just habits
PRICE_MIN_PCT = 0.03
PRICE_MIN_ABS = 0.50


@dataclass
class Tx:
    id: int
    date: date
    amount: float               # negative = debit
    currency: str
    key: str
    label: str
    kind: str = "other"
    resolved: bool = True
    installment_hint: bool = False
    raw_label: str = ""


@dataclass
class Series:
    id: str
    key: str
    label: str
    currency: str
    resolved: bool
    txs: list[Tx] = field(default_factory=list)


# --------------------------------------------------------------------------- helpers

def _median(values: list[float]) -> float:
    return float(statistics.median(values)) if values else 0.0


def _mad(values: list[float]) -> float:
    m = _median(values)
    return _median([abs(v - m) for v in values])


def _cv(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = statistics.fmean(values)
    return statistics.pstdev(values) / mean if mean else 0.0


def _close(a: float, b: float, tol: float) -> bool:
    return abs(a - b) <= tol * max(abs(a), abs(b))


def _r2(x: float) -> float:
    return round(x + 0.0, 2)


def classify_period(median_days: float) -> str | None:
    for name, (lo, hi, _) in PERIODS.items():
        if lo <= median_days <= hi:
            return name
    return None


def _is_increase(old: float, new: float) -> bool:
    return new - old >= PRICE_MIN_ABS and new - old >= PRICE_MIN_PCT * old


# --------------------------------------------------------------------------- exclusions

def find_internal_transfers(txs: list[Tx], holder_aliases: list[str]) -> set[int]:
    """Own-account moves: holder-name matches, or transfer debit/credit pairs of equal amount
    within 3 days."""
    excluded: set[int] = set()
    aliases = [fold(a) for a in holder_aliases or [] if a and a.strip()]
    for t in txs:
        if aliases and any(a in fold(t.raw_label or t.label) for a in aliases):
            excluded.add(t.id)
    debits = sorted((t for t in txs if t.amount < 0 and t.kind == "transfer" and t.id not in excluded), key=lambda t: t.date)
    credits = sorted((t for t in txs if t.amount > 0 and t.kind == "transfer" and t.id not in excluded), key=lambda t: t.date)
    # Credits are bucketed by (currency, amount in cents) and each bucket is read once, in date order,
    # so a long statement full of transfers is not compared pair by pair.
    buckets: dict[tuple[str, int], list[Tx]] = {}
    for c in credits:
        buckets.setdefault((c.currency, round(c.amount * 100)), []).append(c)
    start: dict[tuple[str, int], int] = {}
    used: set[int] = set()
    for d in debits:
        key = (d.currency, round(-d.amount * 100))
        bucket = buckets.get(key)
        if not bucket:
            continue
        i = start.get(key, 0)
        while i < len(bucket) and (d.date - bucket[i].date).days > TRANSFER_DAYS:
            i += 1                                      # too old for this debit and every later one
        start[key] = i
        while i < len(bucket) and (bucket[i].date - d.date).days <= TRANSFER_DAYS:
            c = bucket[i]
            if c.id not in used and abs(c.amount + d.amount) < 0.005:
                excluded.update((d.id, c.id))
                used.add(c.id)
                break
            i += 1
    return excluded


def match_refunds(debits: list[Tx], credits: list[Tx]) -> list[tuple[Tx, Tx]]:
    """Pair each credit with the latest earlier debit (<= 45 days) of a close amount."""
    pairs, used = [], set()
    order = sorted(range(len(debits)), key=lambda i: debits[i].date)        # stable: ties keep list order
    dates = [debits[i].date for i in order]
    for c in sorted(credits, key=lambda t: t.date):
        best = None
        k = bisect_right(dates, c.date) - 1                                   # latest debit not after the credit
        while k >= 0 and (c.date - dates[k]).days <= REFUND_DAYS:
            d = debits[order[k]]
            if best is not None and d.date < best.date:
                break
            if d.id not in used and _close(-d.amount, c.amount, REFUND_TOLERANCE):
                best = d                                                      # same day: the earliest listed wins
            k -= 1
        if best:
            used.add(best.id)
            pairs.append((best, c))
    return pairs


def split_duplicates(debits: list[Tx]) -> tuple[list[Tx], list[tuple[Tx, Tx]]]:
    kept, dups = [], []
    for t in sorted(debits, key=lambda t: (t.date, t.id)):
        prev = kept[-1] if kept and (t.date - kept[-1].date).days < DUPLICATE_DAYS else None
        if prev is not None and _close(prev.amount, t.amount, DUPLICATE_TOLERANCE):
            dups.append((prev, t))
        else:
            kept.append(t)
    return kept, dups


def _periodic(txs: list[Tx]) -> bool:
    """True when the charges, in date order, already form one regular weekly/monthly/... series."""
    if len(txs) < 3:
        return False
    gaps = [(b.date - a.date).days for a, b in zip(txs, txs[1:])]
    m = _median(gaps)
    period = classify_period(m) if m > 0 else None
    return period is not None and max(0.0, 1 - _mad(gaps) / m) >= MIN_INTERVAL_REGULARITY[period]


def split_concurrent(debits: list[Tx]) -> list[list[Tx]]:
    """Two plans billed under one merchant at the same time (two Apple subscriptions):
    amount clusters of 3+ charges whose date ranges overlap by more than 45 days.

    A single bill whose amount varies (energy, cable) also falls into several amount clusters, so the
    split only happens when the charges are NOT one regular series as a whole and every cluster IS one.
    """
    whole = sorted(debits, key=lambda t: t.date)
    if _periodic(whole):
        return [whole]
    clusters: list[list[Tx]] = []
    for t in sorted(debits, key=lambda t: -t.amount):           # by absolute amount, ascending
        for c in clusters:
            if _close(c[0].amount, t.amount, 0.10):
                c.append(t)
                break
        else:
            clusters.append([t])
    big = [sorted(c, key=lambda t: t.date) for c in clusters if len(c) >= 3]
    # Only split when the clusters ARE the merchant's charges (two plans), not when a few
    # coincidences stand out of everyday shopping at the same shop.
    if len(big) < 2 or len(big) > 4 or sum(len(c) for c in big) < 0.9 * len(debits):
        return [sorted(debits, key=lambda t: t.date)]
    for i in range(len(big)):
        for j in range(i + 1, len(big)):
            a, b = big[i], big[j]
            overlap = (min(a[-1].date, b[-1].date) - max(a[0].date, b[0].date)).days
            if overlap <= 45:
                return [whole]
    if not all(_periodic(c) for c in big):
        return [whole]
    return big


# --------------------------------------------------------------------------- amounts

def regimes(amounts: list[float]) -> list[list[float]]:
    """Consecutive runs of near-equal amounts (within 3 % of the run's median)."""
    runs: list[list[float]] = []
    for a in amounts:
        if runs:
            ref = _median(runs[-1])
            if abs(a - ref) <= PRICE_MIN_PCT * ref + 1e-9:
                runs[-1].append(a)
                continue
        runs.append([a])
    return runs


@dataclass
class AmountProfile:
    kind: str                   # fixed | variable
    reference: float            # amount used for the annual cost
    regularity: float           # 1 - CV of the current regime (or whole series if variable)
    steps: list[dict]           # stable-regime price increases
    latest_jump: dict | None    # last charge vs median of the six before


def analyse_amounts(txs: list[Tx]) -> AmountProfile:
    amounts = [-t.amount for t in txs]
    runs = regimes(amounts)
    stable = [r for r in runs if len(r) >= 2]
    in_stable = sum(len(r) for r in stable)
    fixed = _cv(amounts) < FIXED_CV or (stable and in_stable >= 0.7 * len(amounts) and len(stable) <= 3
                                        and all(_cv(r) < FIXED_CV for r in stable))
    steps, latest = [], None
    if fixed:
        idx = 0
        bounds = []
        for r in runs:
            bounds.append((idx, r))
            idx += len(r)
        stable_b = [(i, r) for i, r in bounds if len(r) >= 2]
        for (i0, r0), (i1, r1) in zip(stable_b, stable_b[1:]):
            old, new = _median(r0), _median(r1)
            if _is_increase(old, new):
                steps.append({"from": _r2(old), "to": _r2(new), "since": txs[i1].date.isoformat()})
        if len(amounts) >= 3:
            prev = amounts[-7:-1]
            med = _median(prev)
            if _is_increase(med, amounts[-1]):
                latest = {"from": _r2(med), "to": _r2(amounts[-1]), "since": txs[-1].date.isoformat()}
        if latest and len(runs[-1]) >= 2 and steps and abs(steps[-1]["to"] - _r2(_median(runs[-1]))) < 0.005:
            latest = None                               # the current price level is already reported as a step
    if fixed:
        current = stable[-1] if stable else amounts
        reference = amounts[-1] if latest else _median(current)
        regularity = max(0.0, 1 - _cv(current))
        return AmountProfile("fixed", reference, regularity, steps, latest)
    window = amounts[-12:]
    return AmountProfile("variable", statistics.fmean(window), max(0.0, 1 - _cv(amounts)), [], None)


# --------------------------------------------------------------------------- core

def _confidence(n: int, interval_reg: float, amount_reg: float, resolved: bool) -> float:
    c = 0.4 * min(n, 6) / 6 + 0.4 * interval_reg + 0.2 * amount_reg
    return round(c * (1 if resolved else UNRESOLVED_FACTOR), 2)


def _installment(s: Series, period: str | None) -> bool:
    n = len(s.txs)
    if n > 3 or n < 2:
        return False
    span = (s.txs[-1].date - s.txs[0].date).days
    amounts = [-t.amount for t in s.txs]
    same = all(_close(amounts[0], a, DUPLICATE_TOLERANCE) for a in amounts)
    hinted = any(t.installment_hint for t in s.txs)
    if not same or span > INSTALLMENT_SPAN_DAYS:
        return False
    if hinted:
        return True
    return n == 3 and period is None and amounts[0] >= INSTALLMENT_MIN_AMOUNT


def analyse_series(s: Series, as_of: date) -> dict | None:
    txs = s.txs
    n = len(txs)
    if n < 2:
        return None
    gaps = [(b.date - a.date).days for a, b in zip(txs, txs[1:])]
    m = _median(gaps)
    period = classify_period(m) if m > 0 else None
    if _installment(s, period):
        return {"kind": "installment", "series": s}
    if period is None:
        return None
    interval_reg = max(0.0, 1 - _mad(gaps) / m)
    if interval_reg < MIN_INTERVAL_REGULARITY[period]:
        return None
    amt = analyse_amounts(txs)
    if amt.kind == "variable" and _cv([-t.amount for t in txs]) > MAX_VARIABLE_CV:
        return None                         # amounts all over the place: shopping, not a bill
    per_year = PERIODS[period][2]
    confidence = _confidence(n, interval_reg, amt.regularity, s.resolved)
    if amt.kind == "variable" and not s.resolved:
        confidence = round(confidence * UNRESOLVED_VARIABLE_FACTOR, 2)
    next_expected = txs[-1].date + timedelta(days=round(m))
    cancelled = as_of > next_expected + timedelta(days=CANCELLED_GRACE_DAYS)
    annual = amt.reference * per_year
    merchant = load_merchants().get(s.key, {})
    sub = {
        "id": s.id, "key": s.key, "label": s.label, "periodicity": period,
        "amount_last": _r2(-txs[-1].amount), "amount_median": _r2(_median([-t.amount for t in txs])),
        "amount_kind": amt.kind, "currency": s.currency, "confidence": confidence, "occurrences": n,
        "first_seen": txs[0].date.isoformat(), "last_seen": txs[-1].date.isoformat(),
        "next_expected": next_expected.isoformat(),
        "annual_cost": 0.0 if cancelled else _r2(annual),
        "status": "probably_cancelled" if cancelled else "active",
        "family": merchant.get("family"), "merchant_recognised": s.resolved,
        "interval_regularity": round(interval_reg, 2), "amount_regularity": round(amt.regularity, 2),
    }
    if cancelled:
        sub["annual_cost_before_stop"] = _r2(annual)
    return {"kind": "recurring", "sub": sub, "amounts": amt, "per_year": per_year, "lead": n == 2}


def build_series(txs: list[Tx]) -> tuple[list[Series], list[dict], int, dict[str, list[dict]]]:
    """Group debits by (key, currency); neutralise refunds; set duplicates aside."""
    anomalies: list[dict] = []
    pending_dups: dict[str, list[dict]] = {}
    refunds_neutralised = 0
    groups: dict[tuple[str, str], list[Tx]] = defaultdict(list)
    for t in txs:
        groups[(t.key, t.currency)].append(t)
    out: list[Series] = []
    for (key, cur), items in sorted(groups.items()):
        debits = [t for t in items if t.amount < 0]
        credits = [t for t in items if t.amount > 0]
        refunded = set()
        for d, c in match_refunds(debits, credits):
            refunded.add(d.id)
            refunds_neutralised += 1
            anomalies.append({
                "type": "refund", "key": key, "currency": cur, "annual_impact": 0.0, "amount": _r2(c.amount),
                "date": c.date.isoformat(),
                "detail": f"Charge of {_r2(-d.amount):.2f} {cur} on {d.date.isoformat()} refunded on {c.date.isoformat()}; "
                          "both left out of the analysis.",
            })
        debits = [d for d in debits if d.id not in refunded]
        debits, dups = split_duplicates(debits)
        for a, b in dups:
            pending_dups.setdefault(key, []).append({
                "type": "duplicate", "key": key, "currency": cur, "amount": _r2(-b.amount),
                "annual_impact": _r2(-b.amount), "date": b.date.isoformat(),
                "detail": f"Charged twice: {_r2(-a.amount):.2f} {cur} on {a.date.isoformat()} and {_r2(-b.amount):.2f} "
                          f"{cur} on {b.date.isoformat()}. Ask the merchant to refund the second charge.",
            })
        parts = split_concurrent(debits) if debits else []
        label = items[-1].label
        for i, part in enumerate(parts):
            sid = key if i == 0 else f"{key}#{i + 1}"
            out.append(Series(sid, key, label, cur, all(t.resolved for t in part) if part else True, part))
    return out, anomalies, refunds_neutralised, pending_dups


def _price_anomalies(sub: dict, amt: AmountProfile, per_year: int) -> list[dict]:
    out = []
    cur = sub["currency"]
    for st in amt.steps:
        out.append({
            "type": "price_increase", "key": sub["key"], "id": sub["id"], "currency": cur, "pattern": "step",
            "from": st["from"], "to": st["to"], "since": st["since"],
            "annual_impact": _r2((st["to"] - st["from"]) * per_year),
            "detail": f"{sub['label']}: {st['from']:.2f} -> {st['to']:.2f} {cur} per charge since {st['since']} "
                      f"(+{(st['to'] / st['from'] - 1) * 100:.0f} %).",
        })
    lj = amt.latest_jump
    if lj and not any(abs(st["to"] - lj["to"]) < 0.005 and st["since"] == lj["since"] for st in amt.steps):
        out.append({
            "type": "price_increase", "key": sub["key"], "id": sub["id"], "currency": cur, "pattern": "latest_charge",
            "from": lj["from"], "to": lj["to"], "since": lj["since"],
            "annual_impact": _r2((lj["to"] - lj["from"]) * per_year),
            "detail": f"{sub['label']}: last charge {lj['to']:.2f} {cur} vs {lj['from']:.2f} {cur} median of the "
                      f"six before (+{(lj['to'] / lj['from'] - 1) * 100:.0f} %).",
        })
    return out


def _overlaps(subs: list[dict]) -> list[dict]:
    merchants = load_merchants()
    families = set(_merchant_overlap_families())
    by_fam: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for s in subs:
        fam = merchants.get(s["key"], {}).get("family")
        if s["status"] == "active" and fam in families:
            by_fam[(fam, s["currency"])].append(s)
    out = []
    for (fam, cur), items in sorted(by_fam.items()):
        keys = sorted({s["key"] for s in items})
        if len(keys) < 2:
            continue
        cheapest = min(items, key=lambda s: s["annual_cost"])
        out.append({
            "type": "overlap", "key": ",".join(keys), "family": fam, "currency": cur,
            "annual_impact": cheapest["annual_cost"],
            "detail": f"{len(keys)} services of the same kind ({fam.replace('_', ' ')}) are active: "
                      + ", ".join(f"{s['label']} {s['annual_cost']:.2f} {cur}/yr" for s in items)
                      + f". Not an error; keeping one would save at least {cheapest['annual_cost']:.2f} {cur} a year.",
        })
    return out


def _merchant_overlap_families() -> list[str]:
    from .normalize import DATA_DIR
    import json
    return json.loads((DATA_DIR / "merchants.json").read_text(encoding="utf-8")).get("overlap_families", [])


def _totals(subs: list[dict], low_count: int) -> dict:
    annual = sum(s["annual_cost"] for s in subs if s["status"] == "active")
    count = sum(1 for s in subs if s["status"] == "active")
    return {"annual": _r2(annual), "monthly_average": _r2(annual / 12), "count": count,
            "low_confidence_count": low_count}


def audit(txs: list[Tx], *, as_of: date | None = None, threshold: float = 0.6,
          holder_aliases: list[str] | None = None, fx_rates: dict | None = None,
          period_from: date | None = None, period_to: date | None = None,
          import_summary: dict | None = None) -> dict:
    as_of = as_of or date.today()
    warnings: list[str] = []
    txs = [t for t in txs if (not period_from or t.date >= period_from) and (not period_to or t.date <= period_to)]
    if not txs:
        return {"error": "No transactions in the store for this period. Import a statement first with statement_import."}
    internal = find_internal_transfers(txs, holder_aliases or [])
    other_excluded = [t for t in txs if t.kind in ("withdrawal", "check") and t.id not in internal]
    pool = [t for t in txs if t.id not in internal and t.kind not in ("withdrawal", "check")]
    series, anomalies, refunds, pending_dups = build_series(pool)

    subs, low, leads, transfers = [], [], [], []
    recurring_keys: set[str] = set()
    for s in series:
        res = analyse_series(s, as_of)
        if not res:
            continue
        if res["kind"] == "recurring":
            recurring_keys.add(s.key)
        if res["kind"] == "installment":
            t0 = s.txs[0]
            anomalies.append({
                "type": "installment", "key": s.key, "currency": s.currency, "annual_impact": 0.0,
                "amount": _r2(-t0.amount), "occurrences": len(s.txs),
                "detail": f"{s.label}: {len(s.txs)} identical payments of {_r2(-t0.amount):.2f} {s.currency} between "
                          f"{s.txs[0].date.isoformat()} and {s.txs[-1].date.isoformat()} - a payment plan, not a subscription.",
            })
            continue
        sub = res["sub"]
        if not s.resolved and all(t.kind == "transfer" for t in s.txs):
            sub["kind"] = "transfer"
            transfers.append(sub)       # rent, savings, pocket money: recurring, but not a subscription
            continue
        if res["lead"]:
            leads.append(sub)
            continue
        if sub["confidence"] < threshold:
            low.append(sub)
            continue
        subs.append(sub)
        if sub["status"] == "active":
            anomalies.extend(_price_anomalies(sub, res["amounts"], res["per_year"]))
        else:
            anomalies.append({
                "type": "disappeared", "key": sub["key"], "id": sub["id"], "currency": sub["currency"],
                "annual_impact": -sub["annual_cost_before_stop"],
                "detail": f"{sub['label']}: expected around {sub['next_expected']}, no charge since {sub['last_seen']}. "
                          "Probably cancelled; left out of the current annual cost.",
            })
    anomalies.extend(_overlaps(subs))
    # A near-equal pair 1-3 days apart is a "duplicate charge" only for a recurring payment;
    # at a supermarket it is just two visits.
    for key in sorted(recurring_keys):
        anomalies.extend(pending_dups.get(key, []))

    subs.sort(key=lambda s: (s["status"] != "active", -s["annual_cost"], s["key"]))
    currencies = sorted({s["currency"] for s in subs}) or sorted({t.currency for t in txs})
    by_cur = {c: _totals([s for s in subs if s["currency"] == c], sum(1 for s in low if s["currency"] == c)) for c in currencies}
    base = str((fx_rates or {}).get("base") or "").upper()
    primary = base if base in currencies else max(currencies, key=lambda c: (by_cur[c]["annual"], -currencies.index(c)))
    if len(currencies) > 1:
        warnings.append(f"Several currencies ({', '.join(currencies)}): totals are given per currency and never "
                        "converted unless you set fx_rates.")
    converted = None
    if fx_rates and len(currencies) > 1:
        rates = {k.upper(): float(v) for k, v in fx_rates.items() if k.lower() != "base"}
        missing = [c for c in currencies if c != primary and c not in rates]
        if missing:
            warnings.append(f"fx_rates has no rate for {', '.join(missing)}; no converted total.")
        else:
            total = sum(by_cur[c]["annual"] * (1.0 if c == primary else rates[c]) for c in currencies)
            converted = {"currency": primary, "annual": _r2(total), "monthly_average": _r2(total / 12),
                         "rates_used": {c: rates[c] for c in currencies if c != primary}}

    period = {"from": min(t.date for t in txs).isoformat(), "to": max(t.date for t in txs).isoformat()}
    span = (max(t.date for t in txs) - min(t.date for t in txs)).days
    if span < 90:
        warnings.append(f"Only {span} days of history: monthly subscriptions need about 3 months to be confirmed.")
    if span < 730:
        warnings.append("Yearly subscriptions need 3 charges (over 2 years of history) to be confirmed; with less, "
                        "a yearly charge seen twice is only listed as a lead.")
    stale = (as_of - max(t.date for t in txs)).days
    if stale > 35:
        warnings.append(f"The statement ends {stale} days before {as_of.isoformat()}: subscriptions without a recent "
                        "charge are marked probably_cancelled. Pass as_of to audit as of the statement's last day.")
    variable = [s["label"] for s in subs if s["amount_kind"] == "variable" and s["status"] == "active"]
    if variable:
        warnings.append("Variable-amount recurring bills (energy, telecom usage...): " + ", ".join(variable)
                        + ". Their annual cost is an average, and no price-increase check is run on them.")
    if transfers:
        warnings.append(f"{len(transfers)} recurring outgoing transfer(s) (rent, savings...) listed apart in "
                        "recurring_transfers and not counted as subscriptions. Add your own name to holder_aliases "
                        "to hide transfers between your accounts.")
    if internal:
        warnings.append(f"{len(internal)} internal transfer(s) between your own accounts left out.")
    if other_excluded:
        warnings.append(f"{len(other_excluded)} cash withdrawal(s) and cheque(s) left out of subscription detection.")
    if refunds:
        warnings.append(f"{refunds} refunded charge(s) neutralised.")

    pool_subs = len(subs) + len(low)
    anomalies.sort(key=lambda a: (["duplicate", "price_increase", "overlap", "disappeared", "refund", "installment"].index(a["type"]),
                                  -abs(a.get("annual_impact", 0.0))))
    result = {
        "profile_used": (import_summary or {}).get("profile_used", ""),
        "rows": (import_summary or {}).get("rows", {"read": 0, "skipped": 0, "rejected": 0}),
        "period": period,
        "as_of": as_of.isoformat(),
        "currency": primary,
        "totals": by_cur[primary],
        "totals_by_currency": by_cur,
        "totals_converted": converted,
        "subscriptions": subs,
        "top10": [{"id": s["id"], "label": s["label"], "annual_cost": s["annual_cost"], "currency": s["currency"]}
                  for s in subs if s["status"] == "active"][:10],
        "low_confidence": sorted(low, key=lambda s: -s["confidence"]),
        "low_confidence_share": round(len(low) / pool_subs, 2) if pool_subs else 0.0,
        "leads": leads,
        "recurring_transfers": transfers,
        "anomalies": anomalies,
        "excluded": {"internal_transfers": len(internal), "withdrawals_and_cheques": len(other_excluded),
                     "refunds_neutralised": refunds},
        "confidence_threshold": threshold,
        "warnings": warnings,
    }
    return result


def as_tx(row: dict) -> Tx:
    """Build a Tx from a store row (dict with date as ISO string)."""
    d = row["date"]
    return Tx(id=row["id"], date=date.fromisoformat(d) if isinstance(d, str) else d, amount=row["amount"],
              currency=row["currency"], key=row["key"], label=row["label"], kind=row.get("kind", "other"),
              resolved=bool(row.get("resolved", 1)), installment_hint=bool(row.get("installment_hint", 0)),
              raw_label=row.get("raw_label", ""))


__all__ = ["Tx", "audit", "as_tx", "classify_period", "regimes", "analyse_amounts", "replace"]
