import json
from datetime import date

import pytest

from conftest import BINARY_SAMPLES, FIXTURES, ROOT
from engine.parsing import (choose_date_formats, load_profiles, parse_amount, parse_date, parse_statement,
                            read_file, StatementError)

REQUIRED_PROFILES = {"bnp", "societe_generale", "credit_agricole", "boursobank", "banque_postale", "lcl",
                     "caisse_epargne", "revolut", "n26", "qonto", "wise"}


def parse(name, **kw):
    data = BINARY_SAMPLES[name] if name in BINARY_SAMPLES else (FIXTURES / name).read_bytes()
    return parse_statement(data, **kw)


def test_all_required_bank_profiles_ship_as_json():
    ids = {p["id"] for p in load_profiles()}
    assert REQUIRED_PROFILES <= ids
    for p in (ROOT / "profiles").glob("*.json"):
        prof = json.loads(p.read_text(encoding="utf-8"))
        for field in ("id", "name", "delimiter", "encoding", "skip_rows", "date_formats", "decimal", "sign", "columns"):
            assert field in prof, (p.name, field)


@pytest.mark.parametrize("text, decimal, value", [
    ("-13,49", ",", -13.49), ("1 234,56", ",", 1234.56), ("1.234,56", ",", 1234.56), ("1,234.56", ".", 1234.56),
    ("-12.99", ",", -12.99), ("12,99-", ",", -12.99), ("(4.50)", ".", -4.5), ("+2 450,00 €", ",", 2450.0),
    ("1.234", ",", 1234.0), ("EUR -9.99", ".", -9.99), ("abc", ",", None), ("", ",", None),
])
def test_parse_amount(text, decimal, value):
    assert parse_amount(text, decimal) == value


def test_month_first_or_day_first_is_settled_for_the_whole_file():
    us = ["10/13/2026", "10/02/2026", "10/01/2026"]
    assert choose_date_formats(us, ["%d/%m/%Y", "%m/%d/%Y"])[0] == "%m/%d/%Y"
    eu = ["13/10/2026", "02/10/2026"]
    assert choose_date_formats(eu, ["%m/%d/%Y", "%d/%m/%Y"])[0] == "%d/%m/%Y"
    assert parse_date("2026-10-05 09:12:00", ["%Y-%m-%d %H:%M:%S"]) == date(2026, 10, 5)


def test_shifted_header_comma_decimal_latin1():
    r = parse("sg_latin1_offset.csv")
    assert not r.error
    assert r.profile_used == "societe_generale"
    assert [t.amount for t in r.transactions] == [-13.49, -4.2, 2450.0]
    assert "CAFÉ DE LA GARE" in r.transactions[1].label          # latin-1 decoded correctly
    s = r.summary()
    assert s["rows"] == {"read": 6, "skipped": 1, "rejected": 2}
    assert s["skipped_reasons"] == {"balance or total row": 1}
    assert set(s["rejected_reasons"]) == {"unreadable date '31/13/2026'", "unreadable amount"}
    assert {e["line"] for e in s["rejected_examples"]} == {7, 8}
    assert any("cp1252" in w or "latin-1" in w for w in r.warnings)


def test_detail_column_that_repeats_the_label_is_not_doubled():
    head = "Date de l'opération;Libellé;Détail de l'écriture;Montant de l'opération;Devise\r\n"
    body = ("05/10/2026;PRLV SEPA MAIF;PRLV SEPA MAIF ASSURANCES CONTRAT 1234567;-87,40;EUR\r\n"
            "10/04/2026;CARTE X1234 DECATHLON;CARTE X1234 10/04 DECATHLON LYON;-79,90;EUR\r\n"
            "22/04/2026;AVOIR DECATHLON;AVOIR CARTE X1234 DECATHLON LYON;79,90;EUR\r\n"
            "12/10/2026;RETRAIT DAB;PARIS 15 GAB 00123;-60,00;EUR\r\n")
    r = parse_statement((head + body).encode("cp1252"))
    assert not r.error and r.profile_used == "societe_generale"
    maif, shop, refund, cash = r.transactions
    assert maif.label == "PRLV SEPA MAIF ASSURANCES CONTRAT 1234567"
    from engine.normalize import normalize_label                 # same key, so the refund matches the charge
    assert normalize_label(shop.label).key == normalize_label(refund.label).key == "decathlon-lyon"
    assert cash.label == "RETRAIT DAB PARIS 15 GAB 00123"        # unrelated detail is still appended


def test_debit_and_credit_columns():
    r = parse("ca_debit_credit.csv")
    assert r.profile_used == "credit_agricole"
    assert [t.amount for t in r.transactions] == [-19.99, 100.0, -13.49]


def test_headerless_positional_profile():
    r = parse("lcl_noheader.csv")
    assert r.profile_used == "lcl"
    assert [(t.date, t.amount) for t in r.transactions] == [
        (date(2026, 10, 2), -19.99), (date(2026, 10, 5), -13.49), (date(2026, 10, 28), 2450.0)]


def test_revolut_keeps_completed_rows_and_subtracts_fees():
    r = parse("revolut.csv")
    assert r.profile_used == "revolut"
    assert [(t.amount, t.currency) for t in r.transactions] == [(-13.49, "EUR"), (-20.4, "USD")]
    assert r.summary()["rows"] == {"read": 4, "skipped": 2, "rejected": 0}


def test_us_bank_month_first_dates():
    r = parse("chase.csv")
    assert r.profile_used == "chase"
    assert r.transactions[0].date == date(2026, 10, 13)
    assert r.transactions[0].currency == "USD"


def test_ofx_1_sgml():
    r = parse("statement_v1.ofx")
    assert r.profile_used == "ofx-1"
    assert [t.amount for t in r.transactions] == [-13.49, -12.99, 2450.0]
    assert r.transactions[2].label == "VIR ACME & CO"
    assert r.account_masked == "****5678"
    assert r.rejected == 1


def test_ofx_2_xml_removes_card_numbers_and_masks_account():
    r = parse("statement_v2.ofx")
    assert r.profile_used == "ofx-2"
    assert {t.currency for t in r.transactions} == {"GBP"}
    assert r.cards_removed == 1
    assert "4539" not in r.transactions[1].label
    assert r.account_masked == "****4321"
    assert any("card number" in w for w in r.warnings)


@pytest.mark.parametrize("name, words", [
    ("empty.csv", "empty"), ("blank.csv", "empty"), ("garbage.bin", "binary"), ("fake.pdf", "PDF"),
])
def test_unreadable_and_empty_files_give_a_message_not_an_exception(name, words):
    r = parse(name)
    assert r.error and words in r.error
    assert r.transactions == []


def test_read_file_errors_are_actionable(tmp_path):
    with pytest.raises(StatementError, match="not found"):
        read_file(tmp_path / "missing.csv")
    with pytest.raises(StatementError, match="folder"):
        read_file(tmp_path)
    (tmp_path / "zero.csv").write_bytes(b"")
    with pytest.raises(StatementError, match="empty"):
        read_file(tmp_path / "zero.csv")


def test_unknown_layout_lists_columns_and_asks_for_mapping():
    r = parse("unknown.csv")
    assert "not recognised" in r.error
    assert r.columns_found == ["name", "email"] or "name" in r.error
    assert "column_mapping" in r.error


def test_fallback_mapping_from_settings():
    mapping = {"date": "Quand", "label": "Quoi", "amount": "Combien", "delimiter": "|"}
    r = parse("custom.csv", mapping=mapping)
    assert r.profile_used == "custom"
    assert [t.amount for t in r.transactions] == [-13.49, -10.99]
    assert parse("custom.csv", profile_id="custom").error            # custom without mapping


def test_explicit_unknown_profile_is_an_error_not_a_crash():
    r = parse("revolut.csv", profile_id="nope")
    assert "Unknown profile" in r.error


def test_plain_columns_are_read_as_generic_not_as_a_bank():
    """'Date, Description, Amount' alone must not be reported as a named bank's export."""
    data = b"Date,Description,Amount,Type\n2026-01-05,NETFLIX.COM,-13.49,CARD\n2026-02-05,NETFLIX.COM,-13.49,CARD\n"
    r = parse_statement(data)
    assert r.profile_used == "generic" and len(r.transactions) == 2
    # Asking for a bank by name still works: the user knows better than the header.
    assert parse_statement(data, profile_id="wise").profile_used == "wise"


def test_truncated_utf16_export_does_not_raise():
    data = "Date,Description,Amount\n2026-01-05,NETFLIX.COM,-13.49\n".encode("utf-16")[:-1]
    r = parse_statement(data)
    assert r.error or r.transactions


AMEX = ("Date,Description,Card Member,Account #,Amount\n"
        + "".join(f"{m:02d}/05/2026,NETFLIX.COM,ALEX MARTIN,-51006,15.49\n" for m in range(1, 11))
        + "10/20/2026,ONLINE PAYMENT - THANK YOU,ALEX MARTIN,-51006,-154.90\n")


def test_amex_purchases_are_positive_in_the_export_and_read_as_debits():
    r = parse_statement(AMEX.encode())
    assert r.profile_used == "amex"
    assert [t.amount for t in r.transactions[:2]] == [-15.49, -15.49] and r.transactions[-1].amount == 154.90
    assert not any("positive" in w for w in r.warnings)


def test_positive_only_file_read_as_generic_warns_about_the_sign():
    plain = AMEX.replace("Card Member,Account #,", "").replace("ALEX MARTIN,-51006,", "")
    r = parse_statement(plain.encode())
    assert r.profile_used == "generic" and any('profile="amex"' in w for w in r.warnings)
