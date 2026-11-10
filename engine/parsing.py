"""Statement parsing: CSV (bank profiles + fallback mapping) and OFX 1.x / 2.x.

Pure standard library. Nothing here raises to the caller for bad input:
``parse_statement`` returns a ``ParseResult`` whose ``error`` field explains what
went wrong and what to do about it.
"""
from __future__ import annotations

import csv
import io
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path

from .normalize import fold
from .privacy import mask_account, sanitize

PROFILES_DIR = Path(__file__).resolve().parent.parent / "profiles"
MAX_BYTES = 50 * 1024 * 1024
_BINARY_MAGIC = {                        # file signatures, as hex to keep the source plain text
    bytes.fromhex("25504446"): "a PDF",
    bytes.fromhex("504b0304"): "a ZIP archive or an Excel .xlsx workbook",
    bytes.fromhex("d0cf11e0"): "an Excel .xls workbook",
    bytes.fromhex("89504e47"): "a PNG image",
    bytes.fromhex("ffd8ff"): "a JPEG image",
}
_UTF16_BOMS = (bytes.fromhex("fffe"), bytes.fromhex("feff"))
_FOOTER_LABELS = ("SOLDE", "ANCIEN SOLDE", "NOUVEAU SOLDE", "TOTAL", "OPENING BALANCE", "CLOSING BALANCE", "BEGINNING BALANCE", "ENDING BALANCE", "BALANCE BROUGHT", "BALANCE CARRIED")


@dataclass
class RawTx:
    date: date
    amount: float
    label: str
    currency: str
    category: str = ""
    line: int = 0


@dataclass
class ParseResult:
    profile_used: str = ""
    profile_name: str = ""
    transactions: list[RawTx] = field(default_factory=list)
    read: int = 0
    skipped: int = 0
    rejected: int = 0
    skipped_reasons: Counter = field(default_factory=Counter)
    rejected_reasons: Counter = field(default_factory=Counter)
    rejected_examples: list[dict] = field(default_factory=list)
    account_masked: str = ""
    cards_removed: int = 0
    warnings: list[str] = field(default_factory=list)
    error: str = ""
    columns_found: list[str] = field(default_factory=list)

    def skip(self, reason: str) -> None:
        self.skipped += 1
        self.skipped_reasons[reason] += 1

    def reject(self, reason: str, line: int) -> None:
        self.rejected += 1
        self.rejected_reasons[reason] += 1
        if len(self.rejected_examples) < 5:
            self.rejected_examples.append({"line": line, "reason": reason})

    def summary(self) -> dict:
        return {
            "profile_used": self.profile_used,
            "profile_name": self.profile_name,
            "rows": {"read": self.read, "skipped": self.skipped, "rejected": self.rejected},
            "skipped_reasons": dict(self.skipped_reasons),
            "rejected_reasons": dict(self.rejected_reasons),
            "rejected_examples": self.rejected_examples,
        }


class StatementError(Exception):
    """Unreadable input; the message is meant for the user."""


# --------------------------------------------------------------------------- profiles

@lru_cache(maxsize=1)
def load_profiles() -> list[dict]:
    profiles = [json.loads(p.read_text(encoding="utf-8")) for p in PROFILES_DIR.glob("*.json")]
    return sorted(profiles, key=lambda p: p.get("order", 99))


def profile_ids() -> list[str]:
    return [p["id"] for p in load_profiles()]


def col_key(name: str) -> str:
    """Header comparison key: accents folded, lower case, letters and digits only."""
    return re.sub(r"[^a-z0-9]", "", fold(name.replace("’", "'")).lower())


def mapping_to_profile(mapping: dict) -> dict:
    """Turn the user's ``column_mapping`` setting into a one-off profile."""
    cols = {}
    for f in ("date", "label", "amount", "debit", "credit", "currency", "category", "detail"):
        if mapping.get(f) not in (None, ""):
            v = mapping[f]
            cols[f] = v if isinstance(v, int) else [str(v)]
    fmt = mapping.get("date_format")
    return {
        "id": "custom", "name": "Custom mapping (column_mapping setting)",
        "delimiter": mapping.get("delimiter") or "auto", "encoding": mapping.get("encoding") or "auto",
        "skip_rows": int(mapping.get("skip_rows") or 0), "has_header": bool(mapping.get("has_header", True)),
        "date_formats": [fmt] if fmt else load_profiles()[-1]["date_formats"],
        "decimal": mapping.get("decimal") or "auto", "sign": mapping.get("sign") or "debit_negative",
        "currency_default": mapping.get("currency") if isinstance(mapping.get("currency"), str) and len(mapping.get("currency")) == 3 else "EUR",
        "columns": cols, "distinctive": [], "exact": True,
    }


# --------------------------------------------------------------------------- low level

def read_file(path: Path) -> bytes:
    if not path.exists():
        raise StatementError(f"File not found: {path.name}. Check the path (absolute, or relative to the session's working folder).")
    if path.is_dir():
        raise StatementError(f"{path.name} is a folder, not a statement file.")
    size = path.stat().st_size
    if size == 0:
        raise StatementError("The file is empty (0 bytes). Export the statement again from your bank as CSV or OFX.")
    if size > MAX_BYTES:
        raise StatementError(f"File too large ({size // 1_048_576} MB, limit 50 MB). Export a shorter period.")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise StatementError(f"Cannot read the file ({exc.strerror or exc}). Check its permissions.") from None


def check_bytes(data: bytes) -> None:
    if not data or not data.strip():
        raise StatementError("The file is empty. Export the statement again from your bank as CSV or OFX.")
    for magic, what in _BINARY_MAGIC.items():
        if data.startswith(magic):
            raise StatementError(f"This file is {what}, not a CSV or OFX statement. In your online banking, choose the CSV, OFX or Money/Quicken export.")
    head = data[:4096]
    if bytes(1) in head and not head.startswith(_UTF16_BOMS):
        raise StatementError("Unreadable file: binary content. Export the statement as CSV or OFX.")


def decode(data: bytes, encoding: str = "auto") -> tuple[str, str]:
    if data.startswith(_UTF16_BOMS):
        return data.decode("utf-16", errors="replace"), "utf-16"     # a truncated export must not raise
    # Valid UTF-8 is almost never accidental, so it is tried first whatever the profile says
    # (banks change their exports); then the profile's single-byte encoding, cp1252, latin-1.
    order = ["utf-8-sig"]
    if encoding and encoding not in ("auto", "utf-8", "utf-8-sig"):
        order.append(encoding)
    order += ["cp1252", "latin-1"]
    for enc in order:
        try:
            return data.decode(enc), ("utf-8" if enc == "utf-8-sig" else enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("latin-1", errors="replace"), "latin-1"


def sniff_delimiter(lines: list[str]) -> str:
    best, best_score = ";", -1
    sample = [ln for ln in lines[:60] if ln.strip()]
    for d in (";", ",", "\t", "|"):
        counts = [ln.count(d) for ln in sample]
        if not counts:
            continue
        common = Counter(c for c in counts if c).most_common(1)
        if not common:
            continue
        n, freq = common[0]
        score = freq * min(n, 12)
        if score > best_score:
            best, best_score = d, score
    return best


_NUM_JUNK = re.compile(r"[\s  €$£]|EUR|USD|GBP|CHF", re.IGNORECASE)


def parse_amount(text: str, decimal: str = "auto") -> float | None:
    s = _NUM_JUNK.sub("", (text or "").strip()).replace("'", "")
    if not s:
        return None
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg, s = True, s[1:-1]
    if s.endswith("-"):
        neg, s = True, s[:-1]
    if s.startswith("+"):
        s = s[1:]
    if s.startswith("-"):
        neg, s = not neg, s[1:]
    if "," in s and "." in s:
        dec = "," if s.rfind(",") > s.rfind(".") else "."
    elif "," in s or "." in s:
        sep = "," if "," in s else "."
        tail = s.rsplit(sep, 1)[1]
        if s.count(sep) > 1:
            dec = "." if sep == "," else ","          # repeated: thousands separator
        elif len(tail) == 3 and decimal not in ("auto", sep):
            dec = "." if sep == "," else ","          # "1.234" with comma decimals
        else:
            dec = sep
    else:
        dec = "."
    thousands = "." if dec == "," else ","
    s = s.replace(thousands, "").replace(dec, ".")
    if not re.fullmatch(r"\d+(?:\.\d+)?|\.\d+", s):
        return None
    value = round(float(s), 2)
    return -value if neg else value


_GENERIC_DATE_FORMATS = ["%d/%m/%Y", "%Y-%m-%d", "%d/%m/%y", "%d-%m-%Y", "%d.%m.%Y", "%Y%m%d"]


def parse_date(text: str, formats: list[str]) -> date | None:
    s = (text or "").strip().strip('"')
    if not s:
        return None
    for fmt in list(formats) + _GENERIC_DATE_FORMATS:
        for candidate in (s, s[:19], s[:10]):
            try:
                return datetime.strptime(candidate, fmt).date()
            except ValueError:
                continue
    return None


def choose_date_formats(values: list[str], formats: list[str]) -> list[str]:
    """Order date formats by how many sample values each one parses.

    Settles day-first vs month-first for the whole file instead of row by row
    ("03/04/2026" means 3 April only if no row reads like "04/13/2026")."""
    if not values:
        return list(formats)
    candidates = list(dict.fromkeys(list(formats) + _GENERIC_DATE_FORMATS))

    def hits(fmt: str) -> int:
        n = 0
        for v in values[:300]:
            for c in (v.strip(), v.strip()[:19], v.strip()[:10]):
                try:
                    datetime.strptime(c, fmt)
                    n += 1
                    break
                except ValueError:
                    continue
        return n
    ranked = sorted(candidates, key=lambda f: (-hits(f), candidates.index(f)))
    return ranked


# --------------------------------------------------------------------------- CSV

def _match_columns(header: list[str], profile: dict) -> dict[str, int]:
    keys = [col_key(h) for h in header]
    found: dict[str, int] = {}
    loose = profile["id"] in ("generic", "custom") and not profile.get("exact")
    for fld, cands in profile["columns"].items():
        if isinstance(cands, int):
            continue
        ck = [col_key(c) for c in cands]
        for c in ck:                                    # exact first, in candidate order
            if c in keys and keys.index(c) not in found.values():
                found[fld] = keys.index(c)
                break
        else:
            if loose:
                for i, k in enumerate(keys):
                    if i not in found.values() and any(k.startswith(c) for c in ck if c):
                        found[fld] = i
                        break
    return found


def _usable(found: dict[str, int]) -> bool:
    return "date" in found and "label" in found and ("amount" in found or "debit" in found or "credit" in found)


def _rows(text: str, delimiter: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text), delimiter=delimiter))


def _evaluate_profile(data: bytes, profile: dict, explicit: bool = False) -> tuple[int, dict] | None:
    """Return (score, plan) if the profile can read this file, else None.

    When the profile was not asked for by name, a bank profile only applies if the header shows at least
    one of its distinctive columns: "Date, Description, Amount" alone is the generic layout, not a bank's."""
    text, enc = decode(data, profile.get("encoding", "auto"))
    lines = text.splitlines()
    delim = profile["delimiter"] if profile.get("delimiter") not in (None, "auto") else sniff_delimiter(lines)
    rows = _rows(text, delim)
    if not profile.get("has_header", True):
        cols = profile["columns"]
        candidates = [r for r in rows[profile.get("skip_rows", 0):] if any(c.strip() for c in r)]
        if not candidates:
            return None
        ok = sum(
            1 for r in candidates
            if len(r) >= profile.get("min_fields", 3)
            and parse_date(r[cols["date"]], profile["date_formats"])
            and parse_amount(r[cols["amount"]], profile["decimal"]) is not None
        )
        if ok / len(candidates) < 0.8:
            return None
        fmts = choose_date_formats([r[cols["date"]] for r in candidates if len(r) > cols["date"]], profile["date_formats"])
        return 3, {"profile": profile, "rows": rows, "header_idx": profile.get("skip_rows", 0) - 1,
                   "found": {k: v for k, v in cols.items() if isinstance(v, int)}, "encoding": enc,
                   "date_formats": fmts}
    best = None
    for idx, row in enumerate(rows[:40]):
        if len(row) < 2:
            continue
        found = _match_columns(row, profile)
        if not _usable(found):
            continue
        keys = {col_key(h) for h in row}
        distinct = sum(1 for d in profile.get("distinctive", []) if col_key(d) in keys)
        if not explicit and not distinct and profile.get("distinctive") and profile["id"] not in ("generic", "custom"):
            continue
        score = len(found) + 3 * distinct - (5 if profile["id"] == "generic" else 0)
        sample = [_cell(r, found["date"]) for r in rows[idx + 1: idx + 400]]
        sample = [v for v in sample if v]
        fmts = choose_date_formats(sample, profile["date_formats"])
        if sample and sum(1 for v in sample if parse_date(v, fmts[:1])) / len(sample) < 0.5:
            break                                       # the "date" column does not hold dates
        best = (score, {"profile": profile, "rows": rows, "header_idx": idx, "found": found,
                        "encoding": enc, "date_formats": fmts})
        break                                           # first usable header line wins
    return best


def _cell(row: list[str], idx: int | None) -> str:
    if idx is None or idx >= len(row):
        return ""
    return (row[idx] or "").strip()


def _read_rows(plan: dict, result: ParseResult) -> None:
    profile, rows, found = plan["profile"], plan["rows"], plan["found"]
    fmts, dec = plan.get("date_formats") or profile["date_formats"], profile.get("decimal", "auto")
    keep_states = {s.upper() for s in profile.get("keep_states", [])}
    default_cur = profile.get("currency_default", "EUR")
    width = max(found.values()) + 1 if found else 1
    for i in range(plan["header_idx"] + 1, len(rows)):
        row, line = rows[i], i + 1
        if not any(c.strip() for c in row):
            continue                                    # blank lines are not rows
        result.read += 1
        if len(row) < min(width, 2):
            result.skip("row outside the table (header or footer)")
            continue
        raw_date = _cell(row, found.get("date"))
        label = _cell(row, found.get("label"))
        for extra in ("merchant",):
            if _cell(row, found.get(extra)):
                label = f"{_cell(row, found.get(extra))} {label}".strip()
        detail = _cell(row, found.get("detail"))
        if detail and fold(detail) not in fold(label):
            # Many banks give a short label plus a longer detail that repeats it ("PRLV SEPA NETFLIX" /
            # "PRLV SEPA NETFLIX INTERNATIONAL ECH/0510"): keep the detail alone rather than both.
            words = set(re.findall(r"[A-Z0-9]+", fold(label)))
            if words and words <= set(re.findall(r"[A-Z0-9]+", fold(detail))):
                label = detail.strip()
            else:
                label = f"{label} {detail}".strip()
        if fold(label).startswith(_FOOTER_LABELS) or fold(raw_date).startswith(_FOOTER_LABELS):
            result.skip("balance or total row")
            continue
        if keep_states and found.get("state") is not None:
            state = _cell(row, found.get("state")).upper()
            if state and state not in keep_states:
                result.skip(f"transaction not completed ({state.lower()})")
                continue
        if not raw_date and not any(_cell(row, found.get(k)) for k in ("amount", "debit", "credit")):
            result.skip("row without a transaction")
            continue
        d = parse_date(raw_date, fmts)
        if d is None:
            result.reject(f"unreadable date '{raw_date[:20]}'" if raw_date else "missing date", line)
            continue
        amount = None
        if "amount" in found:
            amount = parse_amount(_cell(row, found["amount"]), dec)
            fee = parse_amount(_cell(row, found.get("fee")), dec) if "fee" in found else None
            if amount is not None and fee:
                amount = round(amount - abs(fee), 2)
        else:
            deb = parse_amount(_cell(row, found.get("debit")), dec)
            cre = parse_amount(_cell(row, found.get("credit")), dec)
            if deb:
                amount = -abs(deb)
            elif cre:
                amount = abs(cre)
            elif deb == 0 or cre == 0:
                amount = 0.0
        if amount is None:
            result.reject("unreadable amount", line)
            continue
        if amount == 0:
            result.skip("zero amount")
            continue
        if profile.get("sign") == "debit_positive":
            amount = -amount
        if not label:
            result.reject("empty description", line)
            continue
        cur = (_cell(row, found.get("currency")) or default_cur).upper()[:3]
        if not re.fullmatch(r"[A-Z]{3}", cur):
            cur = default_cur
        clean, removed = sanitize(label)
        result.cards_removed += removed
        cat, _ = sanitize(_cell(row, found.get("category")))
        result.transactions.append(RawTx(d, amount, clean, cur, cat, line))


def _columns_preview(data: bytes) -> tuple[list[str], str]:
    text, _ = decode(data)
    lines = text.splitlines()
    delim = sniff_delimiter(lines)
    for row in _rows(text, delim)[:40]:
        cells = [c.strip() for c in row if c.strip()]
        if len(cells) >= 2:
            return cells[:30], delim
    return [], delim


def parse_csv(data: bytes, profile_id: str | None = None, mapping: dict | None = None) -> ParseResult:
    profiles = load_profiles()
    if profile_id == "custom" or (profile_id is None and mapping and mapping.get("force")):
        if not mapping:
            r = ParseResult(error="Profile 'custom' requested but no column_mapping setting is configured.")
            return r
        candidates = [mapping_to_profile(mapping)]
    elif profile_id:
        candidates = [p for p in profiles if p["id"] == profile_id]
        if not candidates:
            return ParseResult(error=f"Unknown profile '{profile_id}'. Available profiles: {', '.join(profile_ids())}, ofx, custom.")
    else:
        candidates = list(profiles) + ([mapping_to_profile(mapping)] if mapping else [])

    best = None
    for prof in candidates:
        try:
            ev = _evaluate_profile(data, prof, explicit=bool(profile_id))
        except (csv.Error, IndexError, KeyError, ValueError):
            ev = None
        if ev and (best is None or ev[0] > best[0]):
            best = ev
    if best is None:
        cols, delim = _columns_preview(data)
        tried = ", ".join(p["id"] for p in candidates)
        msg = ("Statement format not recognised (profiles tried: " + tried + "). "
               "Columns found: " + (", ".join(f"'{c}'" for c in cols) if cols else "none") +
               f" (delimiter '{delim}'). Set the column_mapping plugin setting, for example "
               '{"date": "<date column>", "label": "<description column>", "amount": "<amount column>"} '
               '(or "debit"/"credit" when the amount is split in two columns), then import again with profile="custom".')
        return ParseResult(error=msg, columns_found=cols)
    plan = best[1]
    result = ParseResult(profile_used=plan["profile"]["id"], profile_name=plan["profile"]["name"])
    if plan["encoding"] not in ("utf-8", "utf-8-sig") :
        result.warnings.append(f"Detected encoding: {plan['encoding']}.")
    _read_rows(plan, result)
    txs = result.transactions
    if (plan["profile"].get("sign", "debit_negative") != "debit_positive" and len(txs) >= 10
            and sum(1 for t in txs if t.amount > 0) >= 0.8 * len(txs)):
        result.warnings.append(
            "Most amounts in this file are positive, so they were read as money coming in. Card exports such as "
            "American Express list purchases as positive amounts: if that is the case here, import again with "
            'profile="amex", or set column_mapping with "sign": "debit_positive".')
    if not result.transactions and not result.error:
        result.error = (f"Format recognised ({result.profile_name}) but no readable transaction "
                        f"({result.rejected} row(s) rejected). Check the export or set column_mapping.")
    return result


# --------------------------------------------------------------------------- OFX

def is_ofx(data: bytes) -> bool:
    head = data[:4096].upper()
    return b"OFXHEADER" in head or b"<OFX>" in head


def _ofx_tag(block: str, tag: str) -> str:
    m = re.search(r"<" + tag + r">\s*([^<\r\n]*)", block, re.IGNORECASE)
    return m.group(1).strip() if m else ""


def parse_ofx(data: bytes) -> ParseResult:
    head = data[:2048].decode("ascii", errors="ignore").upper()
    version = "ofx-2" if "<?XML" in head or "<?OFX" in head else "ofx-1"
    enc = "auto"
    m = re.search(r'ENCODING="?([A-Z0-9\-]+)', head)
    if m and m.group(1) not in ("USASCII",):
        enc = m.group(1).lower()
    m = re.search(r"CHARSET:\s*(\d+)", head)
    if m and m.group(1) != "NONE":
        enc = "cp" + m.group(1)
    text, _ = decode(data, enc)
    from html import unescape
    result = ParseResult(profile_used=version, profile_name="OFX 2.x (XML)" if version == "ofx-2" else "OFX 1.x (SGML)")
    acct = _ofx_tag(text, "ACCTID")
    result.account_masked = mask_account(acct)
    default_cur = (_ofx_tag(text, "CURDEF") or "EUR").upper()[:3]
    chunks = re.split(r"<STMTTRN>", text, flags=re.IGNORECASE)[1:]
    if not chunks:
        result.error = "OFX file without any transaction (<STMTTRN>). Check the exported period."
        return result
    for n, chunk in enumerate(chunks, start=1):
        block = re.split(r"</STMTTRN>|</BANKTRANLIST>", chunk, maxsplit=1, flags=re.IGNORECASE)[0]
        result.read += 1
        raw_d = _ofx_tag(block, "DTPOSTED") or _ofx_tag(block, "DTUSER")
        d = parse_date(raw_d[:8], ["%Y%m%d"]) if raw_d else None
        if d is None:
            result.reject("unreadable OFX date" if raw_d else "missing OFX date", n)
            continue
        amount = parse_amount(_ofx_tag(block, "TRNAMT"))
        if amount is None:
            result.reject("unreadable OFX amount", n)
            continue
        if amount == 0:
            result.skip("zero amount")
            continue
        name, memo = unescape(_ofx_tag(block, "NAME")), unescape(_ofx_tag(block, "MEMO"))
        label = name if not memo or fold(memo) in fold(name) else f"{name} {memo}".strip()
        if not label:
            result.reject("empty description", n)
            continue
        cur = (_ofx_tag(block, "CURSYM") or _ofx_tag(block, "CURRENCY") or default_cur).upper()[:3]
        clean, removed = sanitize(label)
        result.cards_removed += removed
        result.transactions.append(RawTx(d, amount, clean, cur if re.fullmatch(r"[A-Z]{3}", cur) else default_cur, "", n))
    if not result.transactions:
        result.error = f"OFX file read but no usable transaction ({result.rejected} rejected)."
    return result


# --------------------------------------------------------------------------- entry point

def parse_statement(data: bytes, profile_id: str | None = None, mapping: dict | None = None) -> ParseResult:
    try:
        check_bytes(data)
    except StatementError as exc:
        return ParseResult(error=str(exc))
    if profile_id in ("ofx", "ofx-1", "ofx-2") or (profile_id is None and is_ofx(data)):
        result = parse_ofx(data)
    else:
        result = parse_csv(data, profile_id, mapping)
    if result.cards_removed:
        result.warnings.append(
            f"{result.cards_removed} full payment-card number(s) found in descriptions: removed, never stored.")
    return result
