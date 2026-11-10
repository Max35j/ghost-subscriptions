import json

import pytest

from conftest import ROOT
from engine.normalize import (fold, load_aliases, normalize_label, resolve_alias, slug, strip_noise,
                              strip_prefixes, strip_punctuation)


# Step 1
def test_fold_removes_accents_and_upper_cases():
    assert fold("Prélèvement Société Générale") == "PRELEVEMENT SOCIETE GENERALE"
    assert fold("café crème") == "CAFE CREME"


# Step 2
@pytest.mark.parametrize("raw, rest, kind", [
    ("CB NETFLIX.COM", "NETFLIX.COM", "card"),
    ("PRLV SEPA SPOTIFY AB", "SPOTIFY AB", "direct_debit"),
    ("PRLV EDF", "EDF", "direct_debit"),
    ("PAIEMENT CB APPLE.COM/BILL", "APPLE.COM/BILL", "card"),
    ("FACTURE CARTE DU 11/11 AMAZON", "DU 11/11 AMAZON", "card"),
    ("ECH PRET 1234", "1234", "loan"),
    ("VIR SEPA LOYER", "LOYER", "transfer"),
    ("VIR LIVRET A", "LIVRET A", "transfer"),
    ("RETRAIT DAB 11/11", "11/11", "withdrawal"),
    ("DAB PARIS", "PARIS", "withdrawal"),
    ("TIP ORANGE", "ORANGE", "direct_debit"),
    ("CHQ 1234567", "1234567", "check"),
    ("CARD PAYMENT TO NETFLIX", "NETFLIX", "card"),
    ("DIRECT DEBIT VODAFONE", "VODAFONE", "direct_debit"),
])
def test_strip_prefixes(raw, rest, kind):
    assert strip_prefixes(raw) == (rest, kind)


def test_strip_prefixes_does_not_eat_words_starting_with_a_prefix():
    assert strip_prefixes("TIPTOP SHOP")[0] == "TIPTOP SHOP"
    assert strip_prefixes("CBD STORE")[0] == "CBD STORE"


# Step 3
@pytest.mark.parametrize("raw, expected", [
    ("NETFLIX.COM 4412 11/11", "NETFLIX.COM"),
    ("APPLE.COM/BILL 12:45 IE", "APPLE.COM/BILL"),
    ("CARREFOUR N°1234 2026-11-03 FR", "CARREFOUR"),
    ("AMAZON X1234 05.11.26", "AMAZON"),
    ("SPOTIFY AB ECH/111126 ID EMETTEUR/FR12ZZZ123456 MDT/XYZ", "SPOTIFY AB"),
    ("DEEZER 12NOV26 FRANCE", "DEEZER"),
])
def test_strip_noise(raw, expected):
    assert strip_noise(raw) == expected


# Step 4
def test_strip_punctuation():
    assert strip_punctuation("NETFLIX.COM/BILL  -  *") == "NETFLIX COM BILL"


# Step 5
def test_alias_table_has_at_least_120_entries():
    raw = json.loads((ROOT / "data" / "aliases.json").read_text(encoding="utf-8"))
    assert len(raw["aliases"]) >= 120
    assert len(load_aliases()) >= 120


def test_every_alias_points_to_a_known_merchant():
    merchants = json.loads((ROOT / "data" / "merchants.json").read_text(encoding="utf-8"))["merchants"]
    assert set(load_aliases().values()) <= set(merchants)


@pytest.mark.parametrize("spellings, key", [
    (["CB NETFLIX.COM 4412 11/11", "PRLV SEPA NETFLIX INTERNATIONAL", "Card payment to Netflix.com"], "netflix"),
    (["PRLV SEPA SPOTIFY AB", "CB SPOTIFY P1234ABCD 03/10", "Spotify France"], "spotify"),
    (["PAIEMENT CB APPLE.COM/BILL ICLOUD", "CB ICLOUD STORAGE 4412", "Apple iCloud"], "icloud"),
    (["PRLV SEPA BASIC FIT FRANCE", "CB BASICFIT 1234", "Basic-Fit"], "basic-fit"),
    (["PRLV SEPA RED BY SFR", "RED BY SFR MOBILE", "redbysfr"], "red-by-sfr"),
])
def test_three_spellings_resolve_to_same_key(spellings, key):
    assert {normalize_label(s).key for s in spellings} == {key}


def test_specific_service_beats_generic_billing_name():
    assert resolve_alias("APPLE COM BILL ICLOUD") == "icloud"
    assert resolve_alias("APPLE COM BILL") == "apple"
    assert resolve_alias("SPOTIFY FAMILY") == "spotify-family"
    assert resolve_alias("AMAZON PRIME") == "prime"


# Step 6
def test_unresolved_label_keeps_normalised_key_and_is_flagged():
    n = normalize_label("CB BOULANGERIE DUPONT 12/10")
    assert n.key == "boulangerie-dupont"
    assert n.resolved is False
    assert slug("") == "unknown"


def test_installment_markers_are_detected_and_removed_from_key():
    n = normalize_label("CB FNAC 3X SANS FRAIS")
    assert n.installment_hint and n.key == "fnac"
    assert normalize_label("AMAZON 2/4").installment_hint
