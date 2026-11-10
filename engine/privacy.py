"""Privacy filters applied to every text field before it is stored.

- IBANs are masked down to their last four characters.
- Anything that looks like a full payment-card number (13-19 digits, Luhn-valid,
  spaces or dashes allowed) is removed outright and counted, never stored.
- Account identifiers are reduced to their last four characters.
"""
from __future__ import annotations

import hashlib
import re

_IBAN = re.compile(r"\b([A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){2,7}(?:[ ]?[A-Z0-9]{1,4})?)\b", re.IGNORECASE)
_CARD = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")


def luhn_ok(digits: str) -> bool:
    total, parity = 0, len(digits) % 2
    for i, ch in enumerate(digits):
        d = ord(ch) - 48
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def mask_iban(text: str) -> str:
    def repl(m: re.Match) -> str:
        compact = m.group(1).replace(" ", "")
        if len(compact) < 15:          # too short for any IBAN: leave it alone
            return m.group(0)
        return f"IBAN ****{compact[-4:]}"
    return _IBAN.sub(repl, text)


def strip_card_numbers(text: str) -> tuple[str, int]:
    """Remove Luhn-valid 13-19 digit sequences. Returns (clean_text, removed_count)."""
    removed = 0

    def repl(m: re.Match) -> str:
        nonlocal removed
        digits = re.sub(r"[ -]", "", m.group(0))
        if 13 <= len(digits) <= 19 and luhn_ok(digits):
            removed += 1
            return "[CARTE SUPPRIMEE]"
        return m.group(0)
    return _CARD.sub(repl, text), removed


def sanitize(text: str) -> tuple[str, int]:
    """Card numbers removed first (an IBAN never passes as a card: it starts with letters)."""
    clean, removed = strip_card_numbers(text or "")
    return mask_iban(clean), removed


def mask_account(account: str) -> str:
    account = re.sub(r"\s", "", account or "")
    return f"****{account[-4:]}" if account else ""


def fingerprint(data: bytes) -> str:
    """Content hash used to recognise a re-imported file. The source path is never stored."""
    return hashlib.sha256(data).hexdigest()
