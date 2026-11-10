"""Bank-label normalisation: raw statement text -> canonical merchant key.

The chain runs in a fixed order and every step is a separate, tested function:

1. ``fold``            Unicode decomposition, accents removed, upper case.
2. ``strip_prefixes``  transaction-type prefixes (CB, PRLV SEPA, VIR, ...).
3. ``strip_noise``     dates, times, masked cards, store numbers, 3+ digit codes,
                       SEPA reference tails, country suffixes.
4. ``strip_punctuation`` punctuation and repeated spaces.
5. ``resolve_alias``   alias table (data/aliases.json) -> canonical key.
6. unresolved labels keep their normalised slug, with ``resolved=False`` so the
   detector can lower their confidence.
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# Longest first: "PRLV SEPA" must win over "PRLV".  Each entry maps to a transaction kind.
_PREFIXES: list[tuple[str, str]] = sorted(
    [
        ("PRLV SEPA", "direct_debit"), ("PRLV", "direct_debit"), ("PRELEVEMENT SEPA", "direct_debit"),
        ("PRELEVEMENT", "direct_debit"), ("PRELEVMNT", "direct_debit"), ("ECH PRET", "loan"),
        ("ECH", "direct_debit"), ("TIP", "direct_debit"),
        ("PAIEMENT PAR CARTE", "card"), ("PAIEMENT CB", "card"), ("PAIEMENT CARTE", "card"),
        ("PAIEMENT", "card"), ("FACTURE CARTE", "card"), ("ACHAT CB", "card"), ("CARTE", "card"),
        ("CB", "card"), ("DEBIT CARTE", "card"),
        ("VIR SEPA RECU", "transfer"), ("VIR SEPA EMIS", "transfer"), ("VIR SEPA", "transfer"),
        ("VIR INST", "transfer"), ("VIREMENT SEPA", "transfer"), ("VIREMENT", "transfer"),
        ("VIR", "transfer"), ("AVOIR", "refund"), ("REMBOURSEMENT", "refund"),
        ("RETRAIT DAB", "withdrawal"), ("RETRAIT", "withdrawal"), ("DAB", "withdrawal"),
        ("CHQ", "check"), ("CHEQUE", "check"), ("REMISE CHEQUE", "check"),
        # English-language banks (UK, US, Revolut, N26, Wise...)
        ("DIRECT DEBIT", "direct_debit"), ("DIRECT DEBIT PAYMENT TO", "direct_debit"),
        ("RECURRING PAYMENT", "direct_debit"), ("ACH DEBIT", "direct_debit"), ("ACH", "direct_debit"),
        ("CARD PAYMENT TO", "card"), ("CARD PAYMENT", "card"), ("DEBIT CARD PURCHASE", "card"),
        ("POS PURCHASE", "card"), ("POS", "card"), ("PURCHASE AUTHORIZED ON", "card"), ("CONTACTLESS", "card"),
        ("TRANSFER TO", "transfer"), ("TRANSFER FROM", "transfer"), ("TRANSFER", "transfer"),
        ("STANDING ORDER", "transfer"), ("FASTER PAYMENT", "transfer"), ("FPS", "transfer"),
        ("ONLINE TRANSFER", "transfer"), ("ZELLE PAYMENT TO", "transfer"), ("ZELLE", "transfer"),
        ("ATM WITHDRAWAL", "withdrawal"), ("CASH WITHDRAWAL", "withdrawal"), ("ATM", "withdrawal"),
        ("REFUND FROM", "refund"), ("REFUND", "refund"), ("CHECK", "check"),
    ],
    key=lambda p: -len(p[0]),
)
_PREFIX_RES = [(re.compile(r"^" + re.escape(p) + r"(?=[\s/:.\-*]|$)[\s/:.\-*]*"), kind) for p, kind in _PREFIXES]

# Card token after a "CARTE"/"CB" prefix: "CARTE X1234", "CB*1234", "XXXX XXXX XXXX 1234".
_MASKED_CARD = re.compile(r"\b(?:X{2,}[\s-]?){0,3}X*\d{4}\b|\b[X*]+\d{2,4}\b|\*+\d{2,4}\b")
_DATE = re.compile(
    r"\b\d{1,2}[/.\-]\d{1,2}(?:[/.\-]\d{2,4})?\b"     # 11/11, 11.11.26, 11-11-2026
    r"|\b\d{4}-\d{2}-\d{2}\b"                         # 2026-11-11
    r"|\b\d{1,2}(?:JAN|FEV|FEB|MAR|AVR|APR|MAI|MAY|JUN|JUI|JUL|AOU|AUG|SEP|OCT|NOV|DEC)\d{0,4}\b"
)
_TIME = re.compile(r"\b\d{1,2}[:H]\d{2}(?::\d{2})?\b")
_STORE = re.compile(r"(?:\bN[O°]\s*|#)\s*\d+\b")
_LONG_CODE = re.compile(r"\b[A-Z]*\d{3,}[A-Z0-9]*\b")
# Everything after a SEPA reference marker is mandate / creditor noise.
_SEPA_TAIL = re.compile(r"\s(?:ECH/|ID EMETTEUR|ID CREANCIER|MDT/|REF/|RUM|ICS|LIB/|REF\.?\s*CLIENT|MANDAT)\b.*$")
_COUNTRIES = {
    "FR", "FRA", "FRANCE", "IE", "IRL", "IRELAND", "LU", "LUX", "LUXEMBOURG", "NL", "NLD", "GB",
    "GBR", "UK", "US", "USA", "DE", "DEU", "ES", "ESP", "IT", "ITA", "BE", "BEL", "SE", "SWE",
    "CH", "CHE", "PT", "PRT", "MT", "CY", "EE",
}
_GLUED_SUFFIXES = {"COM", "FR", "NET", "IO", "AI", "EU", "CO", "SO", "ME", "TV", "PLUS"}
_PUNCT = re.compile(r"[^A-Z0-9 ]+")
_SPACES = re.compile(r"\s+")
_NO_FEES = re.compile(r"\b(?:SANS FRAIS|INTEREST FREE|NO FEES)\b")
_INSTALLMENT = re.compile(r"\b(?:[2-4]\s?X|[1-4]\s?/\s?[2-4]|PAIEMENT EN [2-4] FOIS|EN [2-4] FOIS|ECHEANCE [1-4]|INSTALL?MENT [1-4](?: OF [2-4])?|[1-4] OF [2-4])\b")


def fold(text: str) -> str:
    """Step 1: NFKD decomposition, combining marks removed, upper case."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    no_marks = "".join(c for c in decomposed if not unicodedata.combining(c))
    return _SPACES.sub(" ", no_marks.replace(" ", " ")).strip().upper()


def strip_prefixes(text: str) -> tuple[str, str]:
    """Step 2: remove leading transaction-type prefixes (repeatable: "PAIEMENT CB ...").

    Returns ``(rest, kind)`` where kind is the first prefix's transaction kind or ``other``."""
    kind = "other"
    changed = True
    while changed:
        changed = False
        for rx, k in _PREFIX_RES:
            new = rx.sub("", text, count=1)
            if new != text:
                if kind == "other":
                    kind = k
                text = new
                changed = True
                break
    return text.strip(), kind


def strip_noise(text: str) -> str:
    """Step 3: dates, times, masked cards, store numbers, 3+ digit codes, SEPA tails, countries."""
    text = _SEPA_TAIL.sub("", " " + text).strip()
    text = _INSTALLMENT.sub(" ", text)
    text = _NO_FEES.sub(" ", text)
    for rx in (_DATE, _TIME, _STORE, _MASKED_CARD, _LONG_CODE):
        text = rx.sub(" ", text)
    tokens = _SPACES.sub(" ", text).strip().split(" ")
    while len(tokens) > 1 and tokens[-1].strip(".,-/") in _COUNTRIES:
        tokens.pop()
    return " ".join(tokens).strip()


def strip_punctuation(text: str) -> str:
    """Step 4: punctuation becomes a space, spaces collapse."""
    return _SPACES.sub(" ", _PUNCT.sub(" ", text)).strip()


@lru_cache(maxsize=1)
def load_aliases() -> dict[str, str]:
    raw = json.loads((DATA_DIR / "aliases.json").read_text(encoding="utf-8"))
    return {strip_punctuation(fold(k)): v for k, v in raw["aliases"].items()}


@lru_cache(maxsize=1)
def load_merchants() -> dict[str, dict]:
    return json.loads((DATA_DIR / "merchants.json").read_text(encoding="utf-8"))["merchants"]


@lru_cache(maxsize=1)
def _alias_index() -> list[tuple[tuple[str, ...], str]]:
    items = [(tuple(k.split(" ")), v) for k, v in load_aliases().items() if k]
    return sorted(items, key=lambda kv: (-len(kv[0]), -len(" ".join(kv[0]))))


def _alias_matches(alias_tokens: tuple[str, ...], tokens: list[str]) -> bool:
    n = len(alias_tokens)
    if any(tuple(tokens[i:i + n]) == alias_tokens for i in range(len(tokens) - n + 1)):
        return True
    if n == 1 and len(alias_tokens[0]) >= 5 and any(
        t.startswith(alias_tokens[0]) and t[len(alias_tokens[0]):] in _GLUED_SUFFIXES for t in tokens
    ):
        return True                                   # "NETFLIXCOM" -> NETFLIX
    return n > 1 and "".join(alias_tokens) in tokens  # "BASICFIT" -> BASIC FIT


@lru_cache(maxsize=1)
def _alias_rank() -> dict[tuple[str, ...], int]:
    """Position in the table order: longest alias first."""
    return {alias_tokens: i for i, (alias_tokens, _) in enumerate(_alias_index())}


@lru_cache(maxsize=1)
def _alias_lookup() -> tuple[dict, dict, dict]:
    """Aliases indexed by what can make them match: first token, glued single token, joined tokens."""
    by_first: dict[str, list] = {}
    glued: dict[str, list] = {}
    joined: dict[str, list] = {}
    for entry in _alias_index():
        alias_tokens = entry[0]
        by_first.setdefault(alias_tokens[0], []).append(entry)
        if len(alias_tokens) == 1 and len(alias_tokens[0]) >= 5:
            glued.setdefault(alias_tokens[0], []).append(entry)
        if len(alias_tokens) > 1:
            joined.setdefault("".join(alias_tokens), []).append(entry)
    return by_first, glued, joined


def _alias_candidates(tokens: list[str]) -> list[tuple[tuple[str, ...], str]]:
    """The few aliases worth testing for these tokens, so a label is not compared with the whole table."""
    by_first, glued, joined = _alias_lookup()
    seen, out = set(), []
    for t in set(tokens):
        found = by_first.get(t, []) + joined.get(t, [])
        for suffix in _GLUED_SUFFIXES:
            if suffix and t.endswith(suffix):
                found = found + glued.get(t[:-len(suffix)], [])
        for entry in found:
            if entry[0] not in seen:
                seen.add(entry[0])
                out.append(entry)
    return out


@lru_cache(maxsize=8192)
def resolve_alias(clean: str) -> str | None:
    """Step 5: the alias table decides the canonical key.

    Among all aliases found in the cleaned label, a specific service beats a generic
    billing name (APPLE.COM/BILL ICLOUD -> icloud, not apple); then the longest alias wins."""
    tokens = clean.split(" ")
    merchants = load_merchants()
    hits = [(alias_tokens, key) for alias_tokens, key in _alias_candidates(tokens) if _alias_matches(alias_tokens, tokens)]
    if not hits:
        joined = clean.replace(" ", "")
        return load_aliases().get(joined)
    rank = _alias_rank()
    hits.sort(key=lambda h: (merchants.get(h[1], {}).get("family") is None, rank[h[0]]))
    return hits[0][1]


def slug(clean: str, max_tokens: int = 3) -> str:
    """Step 6 fallback key: the first meaningful tokens, lower-case, dash-joined."""
    tokens = [t for t in clean.lower().split(" ") if t and not t.isdigit()]
    return "-".join(tokens[:max_tokens]) or "unknown"


@dataclass(frozen=True)
class Normalized:
    key: str
    label: str          # human label: merchant display name, or the cleaned text
    clean: str          # output of step 4
    kind: str           # card | direct_debit | transfer | withdrawal | check | refund | loan | other
    resolved: bool      # True when the alias table matched
    installment_hint: bool


def normalize_label(raw: str) -> Normalized:
    folded = fold(raw)
    installment_hint = bool(_INSTALLMENT.search(folded))
    rest, kind = strip_prefixes(folded)
    clean = strip_punctuation(strip_noise(rest))
    key = resolve_alias(clean)
    if key:
        label = load_merchants().get(key, {}).get("label") or clean or key
        return Normalized(key, label, clean, kind, True, installment_hint)
    return Normalized(slug(clean), clean or folded, clean, kind, False, installment_hint)
