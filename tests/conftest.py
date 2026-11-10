"""Test helpers. The engine is pure Python: these tests never import Hermes."""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.detect import Tx  # noqa: E402
from engine.normalize import normalize_label  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"

# Non-text inputs are built at test time so the repository holds text files only.
BINARY_SAMPLES = {
    "garbage.bin": bytes([0, 159, 146, 150, 0, 1, 2, 3]) * 64,
    "fake.pdf": b"%PDF-1.7\n%" + bytes([226, 227, 207, 211]) + b"\n1 0 obj\n",
}
AS_OF = date(2026, 11, 9)


class Book:
    """Hand-built synthetic ledger: each test adds the charges it needs."""

    def __init__(self) -> None:
        self.txs: list[Tx] = []

    def add(self, d: date, label: str, amount: float, currency: str = "EUR") -> "Book":
        n = normalize_label(label)
        self.txs.append(Tx(id=len(self.txs) + 1, date=d, amount=round(amount, 2), currency=currency, key=n.key,
                           label=n.label, kind=n.kind, resolved=n.resolved, installment_hint=n.installment_hint,
                           raw_label=label))
        return self

    def every(self, start: date, days: int, count: int, label: str, amounts, currency: str = "EUR") -> "Book":
        for i in range(count):
            amount = amounts[i] if isinstance(amounts, (list, tuple)) else amounts
            self.add(start + timedelta(days=days * i), label, -amount, currency)
        return self

    def monthly(self, start: date, count: int, label: str, amounts, day_jitter: tuple = (), currency: str = "EUR") -> "Book":
        y, m, d0 = start.year, start.month, start.day
        for i in range(count):
            amount = amounts[i] if isinstance(amounts, (list, tuple)) else amounts
            jitter = day_jitter[i % len(day_jitter)] if day_jitter else 0
            self.add(date(y, m, d0) + timedelta(days=jitter), label, -amount, currency)
            m += 1
            if m == 13:
                y, m = y + 1, 1
        return self


def sub(result: dict, key: str) -> dict:
    for s in result["subscriptions"]:
        if s["key"] == key or s["id"] == key:
            return s
    raise AssertionError(f"{key} not in subscriptions: {[s['id'] for s in result['subscriptions']]}")


def anomalies(result: dict, kind: str, key: str | None = None) -> list[dict]:
    return [a for a in result["anomalies"] if a["type"] == kind and (key is None or key in a["key"].split(","))]
