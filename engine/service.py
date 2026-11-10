"""Use cases behind the four tools and the /subs command.

Every function takes its dependencies (SQLite connection, data directory, settings) as
arguments, so the whole flow is testable without Hermes. Each returns a plain dict; the
Hermes layer serialises it to JSON."""
from __future__ import annotations

import sqlite3
from datetime import date, datetime
from pathlib import Path

from . import pack, store
from .detect import as_tx, audit
from .normalize import normalize_label
from .parsing import StatementError, parse_statement, profile_ids, read_file
from .privacy import fingerprint


def _parse_day(value, name: str) -> date | None:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        raise ValueError(f"{name} must be a date like 2026-10-31, got {value!r}") from None


def import_statement(conn: sqlite3.Connection, path: Path, profile: str | None = None,
                     mapping: dict | None = None) -> dict:
    store.init(conn)
    try:
        data = read_file(path)
    except StatementError as exc:
        return {"error": str(exc)}
    fp = fingerprint(data)
    previous = store.find_import(conn, fp)
    if previous:
        return {"status": "already_imported", "imported_at": previous["imported_at"],
                "profile_used": previous["profile"], **{k: previous["summary"].get(k) for k in ("rows", "period")},
                "transactions_added": 0, "next_step": "Run statement_audit."}
    result = parse_statement(data, profile or None, mapping or None)
    if result.error:
        out = {"error": result.error, **result.summary()}
        if result.columns_found:
            out["columns_found"] = result.columns_found
            out["available_profiles"] = profile_ids() + ["ofx", "custom"]
        return out
    rows = []
    for t in result.transactions:
        n = normalize_label(t.label)
        rows.append({"date": t.date.isoformat(), "amount": t.amount, "currency": t.currency, "raw_label": t.label,
                     "key": n.key, "label": n.label, "kind": n.kind, "resolved": n.resolved,
                     "installment_hint": n.installment_hint, "category": t.category})
    dates = [t.date for t in result.transactions]
    summary = {**result.summary(), "period": {"from": min(dates).isoformat(), "to": max(dates).isoformat()}}
    added, known = store.add_import(conn, fp, result.profile_used, result.account_masked, summary, rows)
    currencies = sorted({r["currency"] for r in rows})
    recognised = sum(1 for r in rows if r["resolved"])
    return {
        "status": "imported", **summary, "account": result.account_masked or None,
        "transactions_added": added, "already_known": known, "currencies": currencies,
        "merchants_recognised_share": round(recognised / len(rows), 2) if rows else 0.0,
        "warnings": result.warnings,
        "privacy": "Stored locally in the plugin data folder only: dates, amounts, currencies and sanitised "
                   "descriptions (IBANs masked, card numbers removed). The file path is not stored.",
        "next_step": "Run statement_audit to detect subscriptions.",
    }


def run_audit(conn: sqlite3.Connection, *, threshold: float = 0.6, period_from=None, period_to=None,
              as_of=None, holder_aliases: list[str] | None = None, fx_rates: dict | None = None) -> dict:
    store.init(conn)
    rows = store.transactions(conn)
    if not rows:
        return {"error": "No statement imported yet. Import a CSV or OFX bank export with statement_import first."}
    try:
        threshold = float(threshold)
        if not 0 <= threshold <= 1:
            raise ValueError
    except (TypeError, ValueError):
        return {"error": f"confidence threshold must be a number between 0 and 1, got {threshold!r}"}
    try:
        pf, pt, ao = _parse_day(period_from, "from"), _parse_day(period_to, "to"), _parse_day(as_of, "as_of")
    except ValueError as exc:
        return {"error": str(exc)}
    return audit([as_tx(r) for r in rows], as_of=ao, threshold=threshold, holder_aliases=holder_aliases,
                 fx_rates=fx_rates, period_from=pf, period_to=pt, import_summary=store.last_import_summary(conn))


def cancel_pack(conn: sqlite3.Connection, out_root: Path, ids: list[str], *, language: str = "en",
                formats: list[str] | None = None, holder_name: str = "", contract_refs: dict | None = None,
                **audit_kwargs) -> dict:
    language = (language or "en").lower()
    if language not in pack.T:
        return {"error": f"language must be 'en' or 'fr', got {language!r}"}
    bad = [f for f in (formats or []) if f not in pack.FORMATS]
    if bad:
        return {"error": f"Unknown format(s) {', '.join(bad)}; use any of csv, md, ics."}
    result = run_audit(conn, **audit_kwargs)
    if "error" in result:
        return result
    known = {s["id"]: s for s in result["subscriptions"] + result["low_confidence"]}
    by_key = {}
    for s in known.values():
        by_key.setdefault(s["key"], s)
    if not ids:
        return {"error": "Give the subscription ids to cancel (the 'id' field of statement_audit).",
                "available_ids": sorted(i for i, s in known.items() if s["status"] == "active")}
    chosen, warnings, missing = [], [], []
    for i in ids:
        s = known.get(i) or by_key.get(i)
        if not s:
            missing.append(i)
        elif s["status"] != "active":
            warnings.append(f"{s['label']} looks already cancelled (no charge since {s['last_seen']}); skipped.")
        elif s not in chosen:
            chosen.append(s)
    if missing:
        warnings.append(f"Unknown id(s): {', '.join(missing)}.")
    if not chosen:
        return {"error": "None of the requested subscriptions is active.", "warnings": warnings,
                "available_ids": sorted(i for i, s in known.items() if s["status"] == "active")}
    as_of = date.fromisoformat(result["as_of"])
    rolled = []
    for s in chosen:
        nxt = pack.next_charge(s, as_of).isoformat()
        if nxt != s["next_expected"]:
            warnings.append(f"{s['label']}: the charge expected around {s['next_expected']} is not in the statement "
                            f"yet (it ends {result['period']['to']}); the letter and reminder target the following "
                            f"one, around {nxt}.")
            s = {**s, "next_expected": nxt}
        rolled.append(s)
    chosen = rolled
    out_dir = out_root / "cancel-packs" / datetime.now().strftime("%Y%m%d-%H%M%S")
    files = pack.write_pack(out_dir, chosen, lang=language, formats=formats, refs=contract_refs or {},
                            holder_name=holder_name, as_of=as_of)
    saving = {}
    for s in chosen:
        saving[s["currency"]] = round(saving.get(s["currency"], 0.0) + s["annual_cost"], 2)
    return {
        "status": "written", "folder": str(out_dir), "files": files, "language": language,
        "subscriptions": [{"id": s["id"], "label": s["label"], "next_expected": s["next_expected"],
                           "reminder_date": pack.reminder_date(s, as_of).isoformat(),
                           "annual_cost": s["annual_cost"], "currency": s["currency"]} for s in chosen],
        "annual_saving_if_cancelled": saving,
        "warnings": warnings,
        "note": "Nothing was sent. Review the letters, fill in the [bracketed] fields and send them yourself; "
                "import the .ics file into your calendar for the reminders.",
    }


def purge(conn: sqlite3.Connection, data_dir: Path) -> dict:
    store.init(conn)
    before = store.purge(conn)
    removed = 0
    keep = {"data.db", "data.db-wal", "data.db-shm"}
    for p in sorted(data_dir.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if p.is_file() and p.name not in keep:
            p.unlink()
            removed += 1
        elif p.is_dir():
            try:
                p.rmdir()
            except OSError:
                pass
    return {"status": "purged", "transactions_deleted": before["transactions"], "imports_deleted": before["imports"],
            "files_deleted": removed,
            "note": "All ghost-subscriptions data is gone: transactions, import history and generated cancellation packs."}


def summary_lines(result: dict, language: str = "en") -> str:
    """Three-line digest for the /subs command."""
    fr = (language or "en").lower() == "fr"
    if "error" in result:
        return ("Aucun relevé importé. Demandez-moi d'importer un export CSV ou OFX de votre banque."
                if fr else "No statement imported yet. Ask me to import a CSV or OFX export from your bank.")
    t, cur = result["totals"], result["currency"]
    l1 = "👻 " + (f"{t['count']} abonnements actifs : {t['annual']:.2f} {cur}/an ({t['monthly_average']:.2f}/mois)"
          if fr else f"{t['count']} active subscriptions: {t['annual']:.2f} {cur}/yr ({t['monthly_average']:.2f}/month)")
    if len(result["totals_by_currency"]) > 1:
        others = [f"{v['annual']:.2f} {c}" for c, v in result["totals_by_currency"].items() if c != cur]
        l1 += " + " + " + ".join(others)
    top = " · ".join(f"{s['label']} {s['annual_cost']:.0f}" for s in result["top10"][:3])
    l2 = ("Top : " if fr else "Top: ") + (top or "-")
    kinds = {}
    for a in result["anomalies"]:
        kinds.setdefault(a["type"], []).append(a)
    names = {"price_increase": ("price increase", "hausse de prix"), "duplicate": ("duplicate charge", "doublon"),
             "overlap": ("overlap", "chevauchement"), "disappeared": ("stopped subscription", "abonnement arrêté"),
             "installment": ("payment plan", "paiement échelonné")}
    parts = []
    for k, (en, fr_name) in names.items():
        if k in kinds:
            n = len(kinds[k])
            label = fr_name if fr else en
            extra = ""
            if k == "price_increase":
                extra = f" (+{sum(a['annual_impact'] for a in kinds[k]):.2f}/{'an' if fr else 'yr'})"
            if n > 1:
                label = " ".join(w + ("s" if i == 0 or not fr else "") for i, w in enumerate(label.split(" "))) if fr \
                    else label + "s"
            parts.append(f"{n} {label}{extra}")
    if result["totals"]["low_confidence_count"]:
        n = result["totals"]["low_confidence_count"]
        parts.append(f"{n} {'à confiance faible' if fr else 'low-confidence'}")
    l3 = ("Alertes : " if fr else "Alerts: ") + (", ".join(parts) if parts else ("aucune" if fr else "none"))
    return "\n".join([l1, l2, l3])
