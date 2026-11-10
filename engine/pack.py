"""Cancellation pack: a CSV summary, one cancellation letter per subscription (Markdown)
and an ICS calendar with a reminder 7 days before each next charge.

The pack is written to disk and nothing else: no e-mail, no form submission, no network."""
from __future__ import annotations

import csv
import io
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

REMINDER_DAYS = 7

T = {
    "en": {
        "title": "Cancellation pack",
        "generated": "Generated on {d} by ghost-subscriptions. Nothing has been sent: review, complete the [bracketed] fields, then send each letter yourself.",
        "how": ("How to cancel: first look for a cancel button in the service's account settings or app; when there is "
                "none, send the letter below through the merchant's customer-service form, e-mail or by registered post, "
                "and keep the confirmation."),
        "subject": "Subject", "to": "To", "ref": "Contract / customer reference", "next": "Next expected charge",
        "notice": "Notice", "amount": "Current charge", "letter": "Letter",
        "to_value": "{label} - customer service, cancellations department [address or e-mail to complete]",
        "ref_unknown": "[your customer or contract number - see a past invoice or your account page]",
        "subject_value": "Cancellation of my {label} subscription",
        "body": (
            "[Your name]\n[Your address]\n[Your e-mail]\n\n{today}\n\nDear Sir or Madam,\n\n"
            "I am writing to cancel my {label} subscription (reference: {ref}), currently charged {amount} {cur} "
            "{period_word}. Please end it at the earliest date allowed by my contract, and no later than the end of "
            "the current billing period, and stop all further payments, including direct debits on my account.\n\n"
            "Please confirm the cancellation and its effective date in writing.\n\nYours faithfully,\n\n{signature}"
        ),
        "periods": {"weekly": "every week", "monthly": "every month", "quarterly": "every quarter", "yearly": "every year"},
        "notice_default": "Check the terms: most subscriptions stop at the end of the current billing period.",
        "notice_family": {
            "telecom": "Plans without a minimum term usually end within days of the request; a plan still under a minimum term may charge early-termination fees.",
            "energy": "Energy contracts can usually be ended at any time; you will receive a final bill for consumption up to the end date.",
            "gym": "Gyms often require written notice of one billing period or more: check your membership terms.",
        },
        "ics_summary": "Cancel {label} before {next}",
        "ics_desc": "Next expected charge: {amount} {cur} on {next}. Letter ready in cancel-letters.md.",
        "csv_header": ["id", "service", "periodicity", "last_amount", "currency", "annual_cost", "next_expected",
                       "reminder_date", "notice", "contract_ref", "confidence"],
    },
    "fr": {
        "title": "Pack de résiliation",
        "generated": "Généré le {d} par ghost-subscriptions. Rien n'a été envoyé : relisez, complétez les champs [entre crochets], puis envoyez vous-même chaque lettre.",
        "how": ("Pour résilier : cherchez d'abord la fonction de résiliation dans votre espace client ou l'application. "
                "Pour un contrat souscrit en ligne, la loi française impose depuis le 1er juin 2023 une résiliation "
                "possible en ligne « en trois clics ». À défaut, envoyez la lettre ci-dessous par le formulaire du service "
                "client, par e-mail ou en recommandé avec accusé de réception, et conservez la confirmation."),
        "subject": "Objet", "to": "Destinataire", "ref": "Référence de contrat / client", "next": "Prochaine échéance",
        "notice": "Préavis", "amount": "Montant actuel", "letter": "Lettre",
        "to_value": "{label} - service clients, service résiliation [adresse ou e-mail à compléter]",
        "ref_unknown": "[votre numéro de client ou de contrat - voir une ancienne facture ou votre espace client]",
        "subject_value": "Résiliation de mon abonnement {label}",
        "body": (
            "[Prénom Nom]\n[Adresse]\n[E-mail]\n\nLe {today}\n\nMadame, Monsieur,\n\n"
            "Par la présente, je vous informe de ma décision de résilier mon abonnement {label} (référence : {ref}), "
            "actuellement facturé {amount} {cur} {period_word}. Je vous demande d'y mettre fin à la première date "
            "permise par mon contrat, et au plus tard à la fin de la période en cours, et de cesser tout prélèvement "
            "sur mon compte.\n\nJe vous remercie de me confirmer par écrit la prise en compte de cette résiliation et "
            "sa date d'effet.\n\nVeuillez agréer, Madame, Monsieur, mes salutations distinguées.\n\n{signature}"
        ),
        "periods": {"weekly": "chaque semaine", "monthly": "chaque mois", "quarterly": "chaque trimestre", "yearly": "chaque année"},
        "notice_default": "À vérifier dans vos conditions générales : la plupart des abonnements s'arrêtent à la fin de la période en cours.",
        "notice_family": {
            "telecom": "Offres sans engagement : la résiliation prend en général effet sous 10 jours. Sous engagement, des frais de résiliation anticipée peuvent s'appliquer.",
            "energy": "Un contrat d'énergie peut en général être résilié à tout moment ; une facture de clôture sera émise.",
            "gym": "Les salles de sport demandent souvent un préavis écrit d'au moins une période : vérifiez votre contrat.",
        },
        "ics_summary": "Résilier {label} avant le {next}",
        "ics_desc": "Prochain prélèvement prévu : {amount} {cur} le {next}. Lettre prête dans cancel-letters.md.",
        "csv_header": ["id", "service", "periodicite", "dernier_montant", "devise", "cout_annuel", "prochaine_echeance",
                       "date_rappel", "preavis", "reference_contrat", "confiance"],
    },
}
FORMATS = ("csv", "md", "ics")


def _notice(sub: dict, lang: str) -> str:
    return T[lang]["notice_family"].get(sub.get("family") or "", T[lang]["notice_default"])


_STEP_MONTHS = {"monthly": 1, "quarterly": 3, "yearly": 12}


def _add_months(d: date, months: int) -> date:
    y, m = divmod(d.year * 12 + d.month - 1 + months, 12)
    for day in (d.day, 30, 29, 28):
        try:
            return date(y, m + 1, day)
        except ValueError:
            continue
    return date(y, m + 1, 28)


def next_charge(sub: dict, as_of: date) -> date:
    """First expected charge on or after ``as_of``.

    The audit's ``next_expected`` is "last charge + one period"; when the statement ends a few days
    before ``as_of`` that date is already behind us, and a reminder for it would be useless.
    """
    start = date.fromisoformat(sub["next_expected"])
    months = _STEP_MONTHS.get(sub.get("periodicity") or "")
    d, n = start, 0
    while d < as_of and n < 1000:
        n += 1
        d = _add_months(start, months * n) if months else start + timedelta(days=7 * n)
    return d


def reminder_date(sub: dict, as_of: date) -> date:
    return max(as_of, date.fromisoformat(sub["next_expected"]) - timedelta(days=REMINDER_DAYS))


def build_csv(subs: list[dict], lang: str, refs: dict, as_of: date) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(T[lang]["csv_header"])
    for s in subs:
        w.writerow([s["id"], s["label"], s["periodicity"], f"{s['amount_last']:.2f}", s["currency"],
                    f"{s['annual_cost']:.2f}", s["next_expected"], reminder_date(s, as_of).isoformat(),
                    _notice(s, lang), refs.get(s["id"]) or refs.get(s["key"]) or "", f"{s['confidence']:.2f}"])
    return buf.getvalue()


def build_letters(subs: list[dict], lang: str, refs: dict, holder_name: str, as_of: date) -> str:
    t = T[lang]
    out = [f"# {t['title']}", "", t["generated"].format(d=as_of.isoformat()), "", t["how"], ""]
    for s in subs:
        ref = refs.get(s["id"]) or refs.get(s["key"]) or t["ref_unknown"]
        amount = f"{s['amount_last']:.2f}"
        out += [
            f"## {s['label']}", "",
            f"- **{t['subject']}:** {t['subject_value'].format(label=s['label'])}",
            f"- **{t['to']}:** {t['to_value'].format(label=s['label'])}",
            f"- **{t['ref']}:** {ref}",
            f"- **{t['amount']}:** {amount} {s['currency']} ({s['periodicity']})",
            f"- **{t['next']}:** {s['next_expected']}",
            f"- **{t['notice']}:** {_notice(s, lang)}",
            "", f"### {t['letter']}", "", "```text",
            t["body"].format(today=as_of.isoformat(), label=s["label"], ref=ref, amount=amount, cur=s["currency"],
                             period_word=t["periods"].get(s["periodicity"], ""),
                             signature=holder_name or "[signature]"),
            "```", "",
        ]
    return "\n".join(out)


def _ics_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _fold(line: str) -> str:
    """RFC 5545 line folding at 75 octets."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    parts, cur = [], b""
    for ch in line:
        b = ch.encode("utf-8")
        if len(cur) + len(b) > (75 if not parts else 74):
            parts.append(cur.decode("utf-8"))
            cur = b""
        cur += b
    parts.append(cur.decode("utf-8"))
    return "\r\n ".join(parts)


def build_ics(subs: list[dict], lang: str, as_of: date) -> str:
    t = T[lang]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//ghost-subscriptions//cancel-pack//EN", "CALSCALE:GREGORIAN"]
    for s in subs:
        d = reminder_date(s, as_of)
        amount = f"{s['amount_last']:.2f}"
        lines += [
            "BEGIN:VEVENT",
            f"UID:{s['id'].replace('#', '-')}-{s['next_expected']}@ghost-subscriptions.local",
            f"DTSTAMP:{stamp}",
            f"DTSTART;VALUE=DATE:{d.strftime('%Y%m%d')}",
            f"DTEND;VALUE=DATE:{(d + timedelta(days=1)).strftime('%Y%m%d')}",
            "SUMMARY:" + _ics_escape(t["ics_summary"].format(label=s["label"], next=s["next_expected"])),
            "DESCRIPTION:" + _ics_escape(t["ics_desc"].format(amount=amount, cur=s["currency"], next=s["next_expected"])),
            "TRANSP:TRANSPARENT",
            "BEGIN:VALARM", "ACTION:DISPLAY", "TRIGGER:PT9H",
            "DESCRIPTION:" + _ics_escape(t["ics_summary"].format(label=s["label"], next=s["next_expected"])),
            "END:VALARM", "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(ln) for ln in lines) + "\r\n"


def write_pack(out_dir: Path, subs: list[dict], *, lang: str = "en", formats: list[str] | None = None,
               refs: dict | None = None, holder_name: str = "", as_of: date | None = None) -> dict[str, str]:
    as_of = as_of or date.today()
    refs = refs or {}
    formats = [f for f in (formats or FORMATS) if f in FORMATS]
    out_dir.mkdir(parents=True, exist_ok=True)
    written = {}
    if "csv" in formats:
        p = out_dir / "cancel-summary.csv"
        p.write_text(build_csv(subs, lang, refs, as_of), encoding="utf-8")
        written["csv"] = str(p)
    if "md" in formats:
        p = out_dir / "cancel-letters.md"
        p.write_text(build_letters(subs, lang, refs, holder_name, as_of), encoding="utf-8")
        written["md"] = str(p)
    if "ics" in formats:
        p = out_dir / "cancel-reminders.ics"
        p.write_bytes(build_ics(subs, lang, as_of).encode("utf-8"))
        written["ics"] = str(p)
    return written
