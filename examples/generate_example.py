"""Generate the synthetic 12-month demo statement (fictional person, fictional numbers).

    python examples/generate_example.py

Writes examples/revolut-12-months.csv (Revolut CSV layout) and
examples/statement-12-months.ofx (OFX 2.x) from the same transactions.
Deterministic: the same file comes out every time.
"""
from __future__ import annotations

import csv
import random
from datetime import date, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
START, END = date(2025, 11, 1), date(2026, 10, 31)
rng = random.Random(20261111)
rows: list[tuple[date, str, float, str, str]] = []   # date, description, amount, type, state


def add(d: date, desc: str, amount: float, typ: str = "CARD_PAYMENT", state: str = "COMPLETED") -> None:
    if START <= d <= END:
        rows.append((d, desc, round(amount, 2), typ, state))


def monthly(day: int, desc: str, amount, typ: str = "CARD_PAYMENT", until: date | None = None, jitter: int = 1) -> None:
    y, m = START.year, START.month
    while True:
        d = date(y, m, min(day, 28)) + timedelta(days=rng.randint(0, jitter))
        if d > END or (until and d > until):
            break
        add(d, desc, -(amount(d) if callable(amount) else amount), typ)
        m += 1
        if m == 13:
            y, m = y + 1, 1


# Fourteen subscriptions; the person would name about eight of them.
monthly(5, "Netflix.com", 13.49)
monthly(12, "Spotify Family", 17.99)
monthly(3, "CANAL+ abonnement", lambda d: 22.99 if d < date(2026, 5, 1) else 27.99, typ="TRANSFER")  # price step
monthly(17, "Apple.com/bill iCloud", 2.99)
monthly(21, "Google One storage", 1.99)                       # second cloud: overlap with iCloud
monthly(9, "Amazon Prime FR", 6.99)
monthly(14, "Audible FR", 9.95)                               # the forgotten one
monthly(25, "OpenAI *ChatGPT Subscr", 23.00)
monthly(8, "Xbox Game Pass", 12.99)
monthly(19, "Notion Labs", 9.50)
monthly(2, "Free Mobile", 19.99, typ="TRANSFER")
monthly(6, "EDF clients particuliers", lambda d: round(rng.uniform(58, 118) if d.month in (11, 12, 1, 2, 3) else rng.uniform(38, 62), 2), typ="TRANSFER")
monthly(16, "Disney Plus", 9.99, until=date(2026, 6, 30))     # stopped after June
d = date(2025, 11, 4)
while d <= END:                                               # Basic-Fit bills every four weeks
    add(d, "Basic-Fit France", -29.99)
    d += timedelta(days=28)
for d in (date(2025, 12, 10), date(2026, 3, 11), date(2026, 6, 10), date(2026, 9, 9)):
    add(d, "Veolia Eau", -round(rng.uniform(41, 58), 2), typ="TRANSFER")   # quarterly water bill

# Anomalies worth showing
add(date(2026, 1, 29), "Basic-Fit France", -29.99)           # charged twice (2 days after the 27 Jan charge)
add(date(2026, 1, 20), "Amazon.fr order", -54.90)
add(date(2026, 2, 4), "Refund from Amazon.fr", 54.90, typ="REFUND")
for d in (date(2026, 3, 15), date(2026, 4, 2), date(2026, 5, 9)):
    add(d, "FNAC 3X sans frais", -133.30)                    # payment plan, not a subscription

# Everyday life (noise)
shops = ["Carrefour City", "Monoprix", "Lidl", "Boulangerie Paul", "Uber", "SNCF Connect", "Pharmacie du Port",
         "Decathlon", "Leroy Merlin", "Picard", "Franprix", "Le Comptoir", "Station Total"]
d = START
while d <= END:
    for _ in range(rng.randint(0, 3)):
        add(d, rng.choice(shops), -round(rng.uniform(3.5, 85), 2))
    d += timedelta(days=1)
monthly(28, "Salary ACME SAS", -2450.00, typ="TOPUP")       # a credit: monthly() negates the amount
monthly(1, "Transfer to Alex Martin savings", 300.00, typ="TRANSFER")   # own savings account
for m in range(11, 23):
    y, mm = (2025, m) if m <= 12 else (2026, m - 12)
    add(date(y, mm, 15), "ATM withdrawal", -60.00, typ="ATM")
add(date(2026, 7, 7), "Le Comptoir", -42.00, state="REVERTED")

rows.sort(key=lambda r: (r[0], r[1]))


def write_csv(path: Path) -> None:
    balance = 1800.0
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Type", "Product", "Started Date", "Completed Date", "Description", "Amount", "Fee", "Currency", "State", "Balance"])
        for d, desc, amount, typ, state in rows:
            if state == "COMPLETED":
                balance += amount
            ts = datetime(d.year, d.month, d.day, rng.randint(7, 21), rng.randint(0, 59), rng.randint(0, 59))
            w.writerow([typ, "Current", ts.strftime("%Y-%m-%d %H:%M:%S"), ts.strftime("%Y-%m-%d %H:%M:%S") if state == "COMPLETED" else "",
                        desc, f"{amount:.2f}", "0.00", "EUR", state, f"{balance:.2f}" if state == "COMPLETED" else ""])


def write_ofx(path: Path) -> None:
    trn = []
    for i, (d, desc, amount, typ, state) in enumerate(rows):
        if state != "COMPLETED":
            continue
        name = desc.replace("&", "&amp;")
        trn.append(f"<STMTTRN><TRNTYPE>{'CREDIT' if amount > 0 else 'DEBIT'}</TRNTYPE><DTPOSTED>{d.strftime('%Y%m%d')}120000</DTPOSTED>"
                   f"<TRNAMT>{amount:.2f}</TRNAMT><FITID>SYN{i:05d}</FITID><NAME>{name}</NAME></STMTTRN>")
    body = (
        '<?xml version="1.0" encoding="UTF-8" standalone="no"?>\n<?OFX OFXHEADER="200" VERSION="220" SECURITY="NONE" OLDFILEUID="NONE" NEWFILEUID="NONE"?>\n'
        "<OFX><BANKMSGSRSV1><STMTTRNRS><TRNUID>1</TRNUID><STATUS><CODE>0</CODE><SEVERITY>INFO</SEVERITY></STATUS><STMTRS>"
        "<CURDEF>EUR</CURDEF><BANKACCTFROM><BANKID>99999</BANKID><ACCTID>00000000000012345678</ACCTID><ACCTTYPE>CHECKING</ACCTTYPE></BANKACCTFROM>"
        f"<BANKTRANLIST><DTSTART>{START.strftime('%Y%m%d')}</DTSTART><DTEND>{END.strftime('%Y%m%d')}</DTEND>\n"
        + "\n".join(trn) +
        "\n</BANKTRANLIST></STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>\n"
    )
    path.write_text(body, encoding="utf-8")


if __name__ == "__main__":
    write_csv(HERE / "revolut-12-months.csv")
    write_ofx(HERE / "statement-12-months.ofx")
    print(f"{len(rows)} transactions written")
