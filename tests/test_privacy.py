import sqlite3

from conftest import FIXTURES
from engine import service
from engine.privacy import luhn_ok, mask_account, mask_iban, sanitize, strip_card_numbers


def test_iban_keeps_last_four():
    assert mask_iban("VIR SEPA FR76 3000 6000 0112 3456 7890 189 LOYER") == "VIR SEPA IBAN ****0189 LOYER"
    assert mask_iban("DE89370400440532013000") == "IBAN ****3000"


def test_card_numbers_are_removed_only_when_luhn_valid():
    assert luhn_ok("4539148803436467")
    text, n = strip_card_numbers("PAIEMENT 4539 1488 0343 6467 AMAZON")
    assert n == 1 and "4539" not in text
    text, n = strip_card_numbers("REF 1234567890123 ORDER")       # 13 digits, not Luhn-valid
    assert n == 0 and "1234567890123" in text


def test_sanitize_combines_both():
    clean, removed = sanitize("FR7630006000011234567890189 4539-1488-0343-6467")
    assert removed == 1 and "****0189" in clean and "6467" not in clean


def test_account_mask():
    assert mask_account("40-11-22 87654321") == "****4321"
    assert mask_account("") == ""


def test_source_path_and_card_numbers_never_reach_the_database(tmp_path):
    secret_dir = tmp_path / "very-private-folder"
    secret_dir.mkdir()
    src = secret_dir / "my-bank-export.ofx"
    src.write_bytes((FIXTURES / "statement_v2.ofx").read_bytes())
    db = tmp_path / "data.db"
    conn = sqlite3.connect(db)
    out = service.import_statement(conn, src)
    assert out["status"] == "imported"
    conn.close()
    dump = "\n".join(sqlite3.connect(db).iterdump())
    assert "very-private-folder" not in dump and "my-bank-export" not in dump
    assert "4539" not in dump and "87654321" not in dump
