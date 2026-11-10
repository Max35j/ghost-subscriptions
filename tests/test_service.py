import csv
import re
import sqlite3
from datetime import date

import pytest

from conftest import BINARY_SAMPLES, FIXTURES, ROOT
from engine import service

DEMO = ROOT / "examples" / "revolut-12-months.csv"
DEMO_OFX = ROOT / "examples" / "statement-12-months.ofx"


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "data.db")
    yield c
    c.close()


def test_demo_statement_end_to_end(conn, tmp_path):
    imp = service.import_statement(conn, DEMO)
    assert imp["status"] == "imported" and imp["profile_used"] == "revolut"
    assert imp["rows"] == {"read": 744, "skipped": 1, "rejected": 0}
    a = service.run_audit(conn, as_of="2026-11-09", holder_aliases=["Alex Martin"])
    assert a["totals"]["count"] == 14
    ids = {s["id"] for s in a["subscriptions"]}
    assert {"netflix", "audible", "canal", "icloud", "google-one", "basic-fit", "edf", "veolia"} <= ids
    types = {x["type"] for x in a["anomalies"]}
    assert types == {"duplicate", "price_increase", "overlap", "disappeared", "refund", "installment"}
    lines = service.summary_lines(a).splitlines()
    assert len(lines) == 3 and lines[0].startswith("👻 14 active subscriptions")


def test_reimport_same_file_and_overlapping_ofx_do_not_duplicate(conn):
    first = service.import_statement(conn, DEMO)
    again = service.import_statement(conn, DEMO)
    assert again["status"] == "already_imported" and again["transactions_added"] == 0
    ofx = service.import_statement(conn, DEMO_OFX)                   # same transactions, other format
    assert ofx["status"] == "imported" and ofx["transactions_added"] == 0
    assert ofx["already_known"] == first["transactions_added"]


def test_import_errors_are_json_not_exceptions(conn, tmp_path):
    assert "not found" in service.import_statement(conn, tmp_path / "nope.csv")["error"]
    assert "empty" in service.import_statement(conn, FIXTURES / "empty.csv")["error"]
    (tmp_path / "garbage.bin").write_bytes(BINARY_SAMPLES["garbage.bin"])
    assert "binary" in service.import_statement(conn, tmp_path / "garbage.bin")["error"]
    (tmp_path / "statement.pdf").write_bytes(BINARY_SAMPLES["fake.pdf"])
    assert "PDF" in service.import_statement(conn, tmp_path / "statement.pdf")["error"]
    out = service.import_statement(conn, FIXTURES / "unknown.csv")
    assert "column_mapping" in out["error"] and out["columns_found"] == ["name", "email"]
    assert "No statement imported" in service.run_audit(conn)["error"]
    assert "No statement" in service.summary_lines(service.run_audit(conn))


def test_audit_argument_validation(conn):
    service.import_statement(conn, DEMO)
    assert "between 0 and 1" in service.run_audit(conn, threshold=3)["error"]
    assert "date like" in service.run_audit(conn, period_from="31/12/2026")["error"]


def test_cancel_pack_files(conn, tmp_path):
    service.import_statement(conn, DEMO)
    out = service.cancel_pack(conn, tmp_path, ["netflix", "canal", "disney", "nope"], language="fr",
                              holder_name="Alex Martin", contract_refs={"canal": "CNL-0042"},
                              as_of="2026-11-09", holder_aliases=["Alex Martin"])
    assert out["status"] == "written"
    assert [s["id"] for s in out["subscriptions"]] == ["netflix", "canal"]
    assert any("Disney" in w for w in out["warnings"]) and any("nope" in w for w in out["warnings"])
    assert out["annual_saving_if_cancelled"] == {"EUR": round(161.88 + 335.88, 2)}

    rows = list(csv.reader(open(out["files"]["csv"], encoding="utf-8")))
    assert rows[0][0] == "id" and len(rows) == 3
    md = open(out["files"]["md"], encoding="utf-8").read()
    assert len(re.findall(r"^## ", md, re.M)) == 2 and "Résiliation de mon abonnement Canal+" in md and "CNL-0042" in md
    assert "Alex Martin" in md and "Prochaine échéance" in md
    ics = open(out["files"]["ics"], "rb").read().decode("utf-8")
    assert ics.count("BEGIN:VEVENT") == 2 and "\r\n" in ics
    netflix = next(s for s in out["subscriptions"] if s["id"] == "netflix")
    expected = date.fromisoformat(netflix["next_expected"]).toordinal() - 7
    assert netflix["reminder_date"] == max(date(2026, 11, 9), date.fromordinal(expected)).isoformat()
    assert all(len(line.encode()) <= 75 for line in ics.split("\r\n"))


def test_cancel_pack_never_targets_a_past_charge(conn, tmp_path):
    """Statement ends 2026-10-31, today is 2026-11-09: Canal+ 'next_expected' (2026-11-02) is behind us."""
    service.import_statement(conn, DEMO)
    out = service.cancel_pack(conn, tmp_path, ["canal", "google-one"], as_of="2026-11-09")
    canal, google = out["subscriptions"]
    assert canal["next_expected"] == "2026-12-02" and canal["reminder_date"] == "2026-11-25"
    assert google["next_expected"] == "2026-11-21" and google["reminder_date"] == "2026-11-14"
    assert len(out["warnings"]) == 1 and "Canal+" in out["warnings"][0] and "2026-12-02" in out["warnings"][0]
    ics = open(out["files"]["ics"], encoding="utf-8").read()
    assert "Cancel Canal+ before 2026-12-02" in ics and "2026-11-02" not in ics
    assert "2026-11-02" not in open(out["files"]["md"], encoding="utf-8").read()


def test_next_charge_rolls_by_calendar_period():
    from engine import pack
    sub = {"next_expected": "2026-01-31", "periodicity": "monthly"}
    assert pack.next_charge(sub, date(2026, 1, 15)) == date(2026, 1, 31)
    assert pack.next_charge(sub, date(2026, 2, 1)) == date(2026, 2, 28)
    assert pack.next_charge(sub, date(2026, 3, 1)) == date(2026, 3, 31)
    assert pack.next_charge({"next_expected": "2026-01-05", "periodicity": "weekly"}, date(2026, 1, 20)) == date(2026, 1, 26)
    assert pack.next_charge({"next_expected": "2025-06-10", "periodicity": "yearly"}, date(2026, 11, 9)) == date(2027, 6, 10)
    assert pack.next_charge({"next_expected": "2026-08-10", "periodicity": "quarterly"}, date(2026, 11, 9)) == date(2026, 11, 10)


def test_cancel_pack_english_default_and_validation(conn, tmp_path):
    service.import_statement(conn, DEMO)
    out = service.cancel_pack(conn, tmp_path, ["audible"], as_of="2026-11-09", formats=["md"])
    assert set(out["files"]) == {"md"}
    assert "Cancellation of my Audible subscription" in open(out["files"]["md"], encoding="utf-8").read()
    assert "available_ids" in service.cancel_pack(conn, tmp_path, [], as_of="2026-11-09")
    assert "language" in service.cancel_pack(conn, tmp_path, ["audible"], language="de")["error"]
    assert "format" in service.cancel_pack(conn, tmp_path, ["audible"], formats=["pdf"])["error"]


def test_purge_deletes_everything(conn, tmp_path):
    service.import_statement(conn, DEMO)
    service.cancel_pack(conn, tmp_path, ["netflix"], as_of="2026-11-09")
    out = service.purge(conn, tmp_path)
    assert out["status"] == "purged" and out["transactions_deleted"] == 743 and out["files_deleted"] == 3
    assert not (tmp_path / "cancel-packs").exists()
    assert "No statement imported" in service.run_audit(conn)["error"]


def test_fifty_thousand_rows_import_and_audit_in_seconds(conn, tmp_path):
    """Ten years of a busy account: refunds and transfers carry the same labels as everything else,
    which is the worst case for the matching steps."""
    import random
    import time
    from datetime import timedelta

    rnd = random.Random(7)
    start, days = date(2016, 11, 1), 3650
    rows = []
    for name, amount, step in (("NETFLIX.COM", 13.49, 30), ("SPOTIFY P1234", 10.99, 30), ("BASIC FIT", 29.99, 28)):
        d = start
        while (d - start).days < days:
            rows.append((d, name, -amount))
            d += timedelta(days=step)
    while len(rows) < 50_000:
        d = start + timedelta(days=rnd.randrange(days))
        r = rnd.random()
        if r < 0.08:
            a = rnd.choice([50, 100, 200, 500])
            rows.append((d, "TRANSFER SAVINGS", -a))
            rows.append((d + timedelta(days=rnd.randrange(3)), "TRANSFER SAVINGS", a))
        elif r < 0.14:
            rows.append((d, f"CARD SHOP {rnd.randrange(400):03d} PARIS", round(rnd.uniform(5, 120), 2)))
        else:
            rows.append((d, f"CARD SHOP {rnd.randrange(400):03d} PARIS", -round(rnd.uniform(2, 150), 2)))
    rows = sorted(rows[:50_000])
    path = tmp_path / "ten-years.csv"
    path.write_text("Date,Description,Amount\n" + "".join(f"{d.isoformat()},{l},{a:.2f}\n" for d, l, a in rows))

    t0 = time.perf_counter()
    imp = service.import_statement(conn, path)
    a = service.run_audit(conn, as_of="2026-11-01")
    elapsed = time.perf_counter() - t0
    assert imp["rows"]["read"] == 50_000 and imp["profile_used"] == "generic"
    assert {"netflix", "spotify", "basic-fit"} <= {s["id"] for s in a["subscriptions"]}
    assert elapsed < 20, f"import + audit took {elapsed:.1f}s"
