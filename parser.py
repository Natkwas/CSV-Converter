"""Extract transaction rows (date, description, amount) from bank statement PDFs.

Bank statement layouts vary a lot, so this uses a best-effort pipeline:
1. Try to read real tables out of the PDF and map columns by header keywords.
2. Fall back to line-by-line regex matching (date ... amount) for PDFs with
   no detectable table grid.

Results are meant to be reviewed/edited by the user before export, not trusted blindly.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import pdfplumber
from dateutil import parser as dateparser

MONTH_RE_PART = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?"

DATE_RE = re.compile(
    r"(\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4})"
    r"|(\d{4}[/\-.]\d{1,2}[/\-.]\d{1,2})"
    r"|(\b(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\b)"  # YYYYMMDD
    rf"|({MONTH_RE_PART}\s*\d{{1,2}}(?:,?\s*\d{{2,4}})?)"  # Month Day[, Year] (space is optional -- some PDFs' text extraction drops it)
    rf"|(\d{{1,2}}\s*{MONTH_RE_PART}(?:,?\s*\d{{2,4}})?)",  # Day Month[, Year]
    re.IGNORECASE,
)

MONTH_ABBR_RE = re.compile(rf"^{MONTH_RE_PART}$", re.IGNORECASE)

SECTION_END_RE = re.compile(
    r"statement\s*summary|closing\s*balance|turnover\s*for\s*statement|no\.?\s*(?:credit|debit)\s*transactions?",
    re.IGNORECASE,
)

# A standalone "Bal(ance) Brought Forward" row with its own date and amount
# is a snapshot of the OPENING balance, not a transaction -- including it
# would wrongly count the whole opening balance as a debit/credit movement.
# (Mid-statement "carried/brought forward" page-break markers are harmless
# either way since their amount is always 0.00 in every format seen so far.)
OPENING_BALANCE_RE = re.compile(r"\bbal(?:ance)?\s*brought\s*forward\b", re.IGNORECASE)

# An interest-rate disclosure line (e.g. "DEBIT INTEREST RATE AS AT
# 07/10/2026 0.00 - 999999999999999.99 0.0000%") happens to contain both a
# date and a trivial "0.00" value, making it indistinguishable from a real
# zero-amount transaction by shape alone -- so exclude it by name instead.
INTEREST_RATE_DISCLOSURE_RE = re.compile(r"interest\s*rate\s*as\s*at", re.IGNORECASE)

FOOTER_RE = re.compile(
    r"page \d+ of \d+|@|www\.|reg(?:istration)?\.?\s*no|client care|includes vat|authorised financial",
    re.IGNORECASE,
)

AMOUNT_RE = re.compile(
    r"\(?-?\$?\s?\d{1,3}(?:[,\.\s]\d{3})*\.\d{2}\)?(?:\s?(?:CR|DR))?-?"  # period as decimal point (most banks)
    r"|\(?-?\$?\s?\d{1,3}(?:[.\s]\d{3})*,\d{2}\)?(?:\s?(?:CR|DR))?-?",  # comma as decimal point (e.g. ABSA)
    re.IGNORECASE,
)


def _normalize_numeral(t: str) -> str | None:
    """Normalize a numeral that may use EITHER convention for the decimal
    point (comma or period, with the other -- plus spaces -- as thousands
    separators) into a plain "123456.78" string. Whichever of the last
    comma/period is followed by exactly 2 digits is the decimal point;
    everything before it is thousands grouping and gets stripped. Returns
    None if it doesn't look like a valid 2-decimal amount at all.
    """
    last_sep = max(t.rfind(","), t.rfind("."))
    if last_sep == -1:
        return None  # no decimal point at all -- not a money amount (e.g. a bare reference number)
    decimal_part = t[last_sep + 1 :]
    if len(decimal_part) != 2 or not decimal_part.isdigit():
        return None

    # Validate thousands grouping shape, same as AMOUNT_RE enforces: either
    # no separator at all (and a sane length -- not a 15-digit rate-table
    # placeholder like "999999999999999.99"), or a first group of 1-3
    # digits followed by groups of exactly 3.
    groups = re.split(r"[,.\s]", t[:last_sep])
    if not groups or not all(g.isdigit() for g in groups):
        return None
    if len(groups) == 1:
        if len(groups[0]) > 7:
            return None
    elif not (1 <= len(groups[0]) <= 3) or any(len(g) != 3 for g in groups[1:]):
        return None

    integer_part = "".join(groups)
    if not integer_part:
        return None
    return f"{integer_part}.{decimal_part}"

HEADER_DATE_WORDS = ("date",)
HEADER_DESC_WORDS = ("description", "details", "transaction", "particulars", "narrative", "memo")
HEADER_DEBIT_WORDS = ("debit", "withdrawal", "payment", "out")
HEADER_CREDIT_WORDS = ("credit", "deposit", "in")
HEADER_FEE_WORDS = ("fee", "charges")
HEADER_AMOUNT_WORDS = ("amount",)
HEADER_BALANCE_WORDS = ("balance",)


@dataclass
class Row:
    date: str
    description: str
    amount: str
    fee: str = ""


def clean_amount(raw: str) -> float | None:
    if not raw:
        return None
    text = raw.strip()
    negative = False
    if text.startswith("(") and text.endswith(")"):
        negative = True
        text = text[1:-1]
    if text.endswith("-"):  # trailing minus (e.g. "1.20-"), an older accounting convention some banks still use
        negative = True
        text = text[:-1]
    upper = text.upper()
    if upper.endswith("DR"):
        negative = True
        text = text[:-2]
    elif upper.endswith("CR"):
        text = text[:-2]
    text = text.replace("$", "").strip()
    if text.startswith("-"):
        negative = True
        text = text[1:]
    normalized = _normalize_numeral(text)
    if normalized is None:
        return None
    value = float(normalized)
    return -value if negative else value


def parse_date(raw: str):
    # Bank statements seen so far use the day-first convention (DD/MM/YYYY),
    # not US-style MM/DD/YYYY. Unambiguous dates (day > 12, or a named month)
    # parse the same either way.
    try:
        return dateparser.parse(raw, dayfirst=True, fuzzy=True)
    except (ValueError, OverflowError):
        return None


def _classify_header(cell: str) -> str | None:
    if not cell:
        return None
    text = cell.lower()
    if any(w in text for w in HEADER_DATE_WORDS):
        return "date"
    if any(w in text for w in HEADER_FEE_WORDS):
        return "fee"
    if any(w in text for w in HEADER_DEBIT_WORDS):
        return "debit"
    if any(w in text for w in HEADER_CREDIT_WORDS):
        return "credit"
    if any(w in text for w in HEADER_BALANCE_WORDS):
        return "balance"
    if any(w in text for w in HEADER_AMOUNT_WORDS):
        return "amount"
    if any(w in text for w in HEADER_DESC_WORDS):
        return "description"
    return None


def _rows_from_table(table: list[list[str | None]]) -> list[Row]:
    if not table or len(table) < 2:
        return []

    header = [(_classify_header(c or "")) for c in table[0]]
    if header.count("date") == 0:
        return []

    roles = {role: idx for idx, role in enumerate(header) if role}
    # description = any unclassified column, prefer the widest one by header text length fallback
    desc_idx = roles.get("description")
    if desc_idx is None:
        for idx, c in enumerate(table[0]):
            if idx not in roles.values():
                desc_idx = idx
                break

    rows: list[Row] = []
    for raw_row in table[1:]:
        if raw_row is None:
            continue
        cells = [c.strip() if isinstance(c, str) else "" for c in raw_row]
        if "date" not in roles or roles["date"] >= len(cells):
            continue
        date_val = cells[roles["date"]]
        if not date_val or not DATE_RE.search(date_val):
            continue
        desc_val = cells[desc_idx] if desc_idx is not None and desc_idx < len(cells) else ""

        amount_val = None
        if "debit" in roles or "credit" in roles:
            debit = clean_amount(cells[roles["debit"]]) if "debit" in roles and roles["debit"] < len(cells) else None
            credit = clean_amount(cells[roles["credit"]]) if "credit" in roles and roles["credit"] < len(cells) else None
            if debit:
                amount_val = -abs(debit)
            elif credit:
                amount_val = abs(credit)
        elif "amount" in roles and roles["amount"] < len(cells):
            amount_val = clean_amount(cells[roles["amount"]])

        fee_val = clean_amount(cells[roles["fee"]]) if "fee" in roles and roles["fee"] < len(cells) else None

        if amount_val is None and fee_val is None:
            continue

        amount_str = f"{amount_val:.2f}" if amount_val is not None else "0.00"
        fee_str = f"{-abs(fee_val):.2f}" if fee_val else ""
        rows.append(Row(date=date_val, description=desc_val, amount=amount_str, fee=fee_str))
    return rows


def _rows_from_text(text: str) -> list[Row]:
    rows: list[Row] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if SECTION_END_RE.search(line):
            break  # hit a closing totals/summary section -- not individual transactions
        if OPENING_BALANCE_RE.search(line) or INTEREST_RATE_DISCLOSURE_RE.search(line):
            continue  # a balance snapshot or rate disclosure, not a transaction
        date_match = DATE_RE.search(line)
        if not date_match or date_match.start() > 20:
            # Not a new transaction line. A short, mostly-alphabetic line
            # right after one is usually a wrapped category/type tag
            # (e.g. "CHEQUE CARD PURCHASE") -- fold it into the last row.
            if rows and 0 < len(line) <= 40 and not AMOUNT_RE.search(line) and not FOOTER_RE.search(line):
                rows[-1].description = f"{rows[-1].description} {line}".strip()
            continue

        amount_matches = list(AMOUNT_RE.finditer(line))
        if not amount_matches:
            continue
        if len(amount_matches) >= 2:
            # Trailing running-balance column is common; the amount right
            # before it is the actual transaction amount.
            amount_match = amount_matches[-2]
        else:
            amount_match = amount_matches[-1]
        amount_val = clean_amount(amount_match.group())
        if amount_val is None:
            continue
        desc_start = date_match.end()
        desc_end = amount_match.start()
        description = line[desc_start:desc_end].strip(" -:\t")
        if not description:
            description = line[desc_start:].strip(" -:\t")
        rows.append(Row(date=date_match.group(), description=description, amount=f"{amount_val:.2f}"))
    return rows


def _get_ocr_engine():
    """Lazily load the (heavy) OCR engine, only when a page actually needs it."""
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError:
        return None
    return RapidOCR()


def _token_amount(token: str, default_negative: bool = False) -> float | None:
    """Parse a single word as a money value, or None if it isn't one.

    default_negative: when the token has no explicit sign/suffix at all (no
    "-", "()", "CR" or "DR"), some banks still mean "this is a debit" --
    pass True for columns where that convention is detected.
    """
    t = token.strip()
    if not t:
        return None
    negative = False
    explicit_sign = False
    if t.startswith("(") and t.endswith(")"):
        negative = True
        explicit_sign = True
        t = t[1:-1]
    if t.endswith("*"):
        t = t[:-1]
    if t.endswith("-"):  # trailing minus (e.g. "1.20-"), an older accounting convention some banks still use
        negative = True
        explicit_sign = True
        t = t[:-1]
    upper = t.upper()
    if upper.endswith("CR"):
        t = t[:-2]
        explicit_sign = True
    elif upper.endswith("DR"):
        negative = True
        explicit_sign = True
        t = t[:-2]
    t = t.strip()
    if t.startswith("-"):
        negative = True
        explicit_sign = True
        t = t[1:]
    t = re.sub(r"^[A-Za-z$]+", "", t)  # strip a leading currency symbol/code (R, ZAR, $...)
    normalized = _normalize_numeral(t)
    if normalized is None:
        return None
    value = float(normalized)
    if not explicit_sign and default_negative:
        negative = True
    return -value if negative else value


def _words_from_ocr_result(ocr_result) -> list[dict]:
    words = []
    for box, text, _conf in ocr_result:
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        words.append({"x0": min(xs), "x1": max(xs), "y0": min(ys), "y1": max(ys), "text": text.strip()})
    return words


def _words_from_page(page) -> list[dict]:
    words = []
    for w in page.extract_words():
        words.append({"x0": w["x0"], "x1": w["x1"], "y0": w["top"], "y1": w["bottom"], "text": w["text"].strip()})
    return words


def _cluster_rows_by_position(words: list[dict], y_tol: float = 6.0) -> list[list[dict]]:
    """Group word boxes into table rows by vertical position."""
    words = sorted(words, key=lambda w: (w["y0"], w["x0"]))

    rows: list[list[dict]] = []
    for w in words:
        cy = (w["y0"] + w["y1"]) / 2
        for row in rows:
            row_cy = sum((r["y0"] + r["y1"]) / 2 for r in row) / len(row)
            if abs(cy - row_cy) <= y_tol:
                row.append(w)
                break
        else:
            rows.append([w])

    rows.sort(key=lambda row: min(w["y0"] for w in row))
    for row in rows:
        row.sort(key=lambda w: w["x0"])
    return rows


def _merge_adjacent_words(row: list[dict], fullmatch_test) -> list[dict]:
    """Merge runs of adjacent words that together satisfy fullmatch_test.

    Needed for layouts where a real text layer (not OCR) splits a single
    logical value into multiple words -- a date written as "01" + "Sep", or
    a space-grouped thousands amount like "2" + "675" + "194.23".
    """
    merged: list[dict] = []
    i = 0
    while i < len(row):
        best = None
        for span in (4, 3, 2):
            if i + span > len(row):
                continue
            chunk = row[i : i + span]
            combined = " ".join(w["text"] for w in chunk)
            if fullmatch_test(combined.strip()):
                best = chunk
                break
        if best:
            merged.append(
                {
                    "x0": best[0]["x0"],
                    "x1": best[-1]["x1"],
                    "y0": min(w["y0"] for w in best),
                    "y1": max(w["y1"] for w in best),
                    "text": " ".join(w["text"] for w in best),
                }
            )
            i += len(best)
        else:
            merged.append(row[i])
            i += 1
    return merged


# Legitimate amounts routinely end with one of these glued directly onto the
# number (e.g. "468,270.87Dr", "10,292.50Cr") -- strip it before judging a
# token corrupted, or every such amount would otherwise look exactly like a
# digit-misread-as-letter OCR error.
_KNOWN_AMOUNT_SUFFIXES = ("DR", "CR")


def _page_text_looks_corrupted(words: list[dict]) -> bool:
    """A fully scanned page can already carry its own, poor-quality OCR text
    layer baked in by whatever software produced the PDF -- e.g. a digit
    misread as a letter mid-number ("104,U12A"). That corruption can't be
    recovered by regex (the original digit is simply gone), so such pages
    are better served by (re-)OCRing the rendered image ourselves. Detected
    by a token that is mostly numeric but has a letter stuck in the middle
    -- a shape normal words and alphanumeric reference codes (which start
    with a letter, not a digit) never take.
    """
    for w in words:
        t = w["text"].strip()
        if not t or not t[0].isdigit():
            continue
        body = t[1:-1] if t.startswith("(") and t.endswith(")") else t
        upper = body.upper()
        for suffix in _KNOWN_AMOUNT_SUFFIXES:
            if upper.endswith(suffix):
                body = body[: -len(suffix)]
                break
        if body.endswith(("-", "*")):
            body = body[:-1]
        # A reference/account code starting with digits (e.g. "00Cli2521868",
        # a date fragment like "31-Aug") can also mix in letters without
        # being corrupted -- the real tell is a comma/period thousands or
        # decimal separator (the shape a plain reference code never has)
        # alongside the stray letter.
        if any(ch.isalpha() for ch in body) and any(ch in ",." for ch in body):
            return True
    return False


def _bucket_for_amount(word: dict, col_x: dict[str, float]) -> str | None:
    """Which money column a word belongs to, based on the header's x-positions.

    Compares the word's RIGHT edge against each header's LEFT edge, not
    midpoints: values are right-aligned within their column, so a value's
    right edge stays anchored just before the next column's header starts,
    regardless of how wide the value itself is (its left edge can drift a
    lot -- a short "0.00" and a long "-120,000.00" in the same column have
    very different left edges but similar right edges). Concretely: a word
    belongs to the rightmost column whose header x0 is still <= the word's
    x1. The date column is never a candidate -- an amount can legitimately
    overflow past where a narrow "Date" header sits, and a value is never
    actually part of the date.

    Returns None for a word that sits to the left of EVERY money column --
    e.g. a small foreign-currency disclosure amount printed before the
    merchant name on a card purchase (" POS Purchase 9.32 Google Workspa
    ..."). That's still part of the description, not a real column value,
    and must not be mistaken for one (the leftmost column) by default.
    """
    candidates = {k: v for k, v in col_x.items() if k != "date"}
    ordered = sorted(candidates.items(), key=lambda kv: kv[1])
    x1 = word.get("x1", word["x0"])
    idx = None
    for i, (_role, x0) in enumerate(ordered):
        if x0 <= x1:
            idx = i
        else:
            break
    return ordered[idx][0] if idx is not None else None


def _classify_header_word(text: str) -> str | None:
    t = text.strip().lower()
    if not t:
        return None
    if t == "money in":
        return "money_in"
    if t == "money out":
        return "money_out"
    if t == "date":
        return "date"
    if t == "balance":
        return "balance"
    if any(w in t for w in HEADER_FEE_WORDS):
        return "fee"
    t_singular = t[:-1] if t.endswith("s") else t
    if any(w == t_singular for w in HEADER_DEBIT_WORDS):
        return "debit"
    if any(w == t_singular for w in HEADER_CREDIT_WORDS):
        return "credit"
    if t in HEADER_AMOUNT_WORDS:
        return "amount"
    return None


def _detect_header_and_start(rows: list[list[dict]]) -> tuple[dict[str, float], int]:
    """Find the header row(s) and learn each money column's x-position.

    A header can wrap across 2-3 close lines (e.g. "Bank" / "Charges"), so
    once the row containing "Date" is found, nearby rows are folded in too,
    stopping as soon as a row that looks like real data (has a date or an
    amount) is reached.
    """
    for i, row in enumerate(rows):
        if not any(w["text"].strip().lower() == "date" for w in row):
            continue

        col_x: dict[str, float] = {}
        header_rows = [row]
        j = i + 1
        while j < len(rows):
            nxt = rows[j]
            if any(DATE_RE.fullmatch(w["text"].strip()) for w in nxt):
                break
            if any(_token_amount(w["text"]) is not None for w in nxt):
                break
            if nxt[0]["y0"] - header_rows[-1][0]["y0"] > 20:
                break
            header_rows.append(nxt)
            j += 1

        for hrow in header_rows:
            for w in hrow:
                role = _classify_header_word(w["text"])
                if role and role not in col_x:
                    col_x[role] = w["x0"]

        # Require "balance" specifically, not just any second role -- a
        # stray "Date" mention elsewhere (e.g. "Statement Date: ...") could
        # otherwise coincidentally pick up one unrelated nearby role too.
        if "date" in col_x and "balance" in col_x:
            return col_x, j
    return {}, 0


def _rows_from_word_rows(rows: list[list[dict]], fallback_year: str | None = None) -> list[Row]:
    rows = [_merge_adjacent_words(row, lambda s: AMOUNT_RE.fullmatch(s)) for row in rows]
    rows = [_merge_adjacent_words(row, lambda s: DATE_RE.fullmatch(s)) for row in rows]
    col_x, start_index = _detect_header_and_start(rows)
    money_roles = {"money_in", "money_out", "debit", "credit", "amount"}

    # Decide the sign convention for a plain "amount" column: if nothing in
    # it ever uses an explicit "-", this bank likely marks credits only
    # (e.g. a trailing "Cr") and means unsigned = debit.
    amount_default_negative = False
    if col_x.get("amount") is not None:
        amount_texts = []
        for row in rows[start_index:]:
            for w in row:
                if _token_amount(w["text"]) is not None and _bucket_for_amount(w, col_x) == "amount":
                    amount_texts.append(w["text"])
        if amount_texts and not any("-" in t for t in amount_texts):
            amount_default_negative = True

    out: list[Row] = []
    last_date: str | None = None
    for row in rows[start_index:]:
        row_text = " ".join(w["text"] for w in row)
        if SECTION_END_RE.search(row_text):
            break  # hit a closing totals/summary section -- not individual transactions
        if OPENING_BALANCE_RE.search(row_text):
            continue  # a balance snapshot, not a transaction -- skip, don't count as a flow
        if INTEREST_RATE_DISCLOSURE_RE.search(row_text):
            continue  # boilerplate rate disclosure, not a transaction

        date_word = next((w for w in row if DATE_RE.fullmatch(w["text"].strip())), None)

        def is_amount(w):
            if col_x:
                bucket = _bucket_for_amount(w, col_x)
                if bucket is None:
                    return False  # left of every money column -- part of the description
                default_neg = amount_default_negative if bucket == "amount" else False
            else:
                default_neg = False
            return _token_amount(w["text"], default_neg) is not None

        amount_words = [w for w in row if w is not date_word and is_amount(w)]
        text_words = [w for w in row if w is not date_word and not is_amount(w)]

        fee_val = 0.0
        if col_x:
            net = 0.0
            found = False
            for w in amount_words:
                bucket = _bucket_for_amount(w, col_x)
                if bucket == "amount":
                    default_neg = amount_default_negative
                elif bucket == "fee":
                    # A fee is always a cost, even when its column prints no
                    # minus sign (forcing negative here is a no-op when the
                    # token already has an explicit sign/suffix).
                    default_neg = True
                else:
                    default_neg = False
                value = _token_amount(w["text"], default_neg)
                if bucket in money_roles:
                    net += value
                    found = True
                elif bucket == "fee":
                    fee_val += value
                    found = True
        else:
            found = bool(amount_words)
            net = 0.0
            if amount_words:
                key_word = amount_words[-2] if len(amount_words) >= 2 else amount_words[-1]
                net = _token_amount(key_word["text"])

        if date_word is None and not found:
            continuation = " ".join(w["text"] for w in text_words).strip()
            if FOOTER_RE.search(continuation):
                break  # hit the page footer/legal boilerplate -- nothing useful follows
            # Otherwise it's wrapped text (category/description continuation)
            # -- fold it into the row above rather than starting a new transaction.
            if out and continuation:
                out[-1].description = f"{out[-1].description} {continuation}".strip()
            continue

        if date_word is None and last_date is None:
            continue  # an amount with no date to anchor it to yet -- skip rather than guess

        if date_word is not None:
            date_text = date_word["text"].strip()
            if fallback_year and date_text[-1].isalpha():
                date_text = f"{date_text} {fallback_year}"
            last_date = date_text

        if not found:
            continue

        description = " ".join(w["text"] for w in text_words).strip()
        fee_str = f"{fee_val:.2f}" if fee_val else ""
        out.append(Row(date=last_date, description=description, amount=f"{net:.2f}", fee=fee_str))

    return out


# Per-label forced sign: a charge/purchase is always an expense (negative);
# a credit/payment is always money in (positive) -- regardless of whatever
# sign (if any) the statement itself prints on the raw number.
SECTIONED_SIGN_BY_LABEL = {
    "transactions": -1.0,  # ABSA credit card: purchases
    "bank charges": -1.0,  # ABSA credit card: fees
    "credits": 1.0,  # Standard Bank Titanium: payments/credits received
    "debit": -1.0,  # Standard Bank Titanium: purchases/charges
}
# Statements seen so far with labelled sub-sections instead of one continuous
# dated ledger with a running balance. A page must contain ALL labels of one
# whole set to use that set -- requiring just one would misfire on a normal
# ledger whose own transaction description happens to read e.g. "BANK CHARGES".
KNOWN_SECTION_SETS = (
    frozenset({"transactions", "bank charges"}),
    frozenset({"credits", "debit"}),
)
SECTIONED_STOP_WORDS = ("interest calculated", "total balance", "(subtotal)", "subtotal")


def _detect_section_labels(rows: list[list[dict]]) -> frozenset[str] | None:
    # A label that heads two side-by-side columns (e.g. a two-column credit
    # card statement's "Debit" sub-header repeated once per column) can land
    # on the same clustered row twice before the columns are split apart --
    # so a row counts as a label match when ALL its words are that one label
    # (repeated or not), not only when the row is that single word.
    found = set()
    for row in rows:
        texts = {w["text"].strip().lower() for w in row}
        if len(texts) == 1:
            found |= texts & set(SECTIONED_SIGN_BY_LABEL)
    for known in KNOWN_SECTION_SETS:
        if known.issubset(found):
            return known
    return None


def _rows_from_sectioned_rows(
    rows: list[list[dict]], fallback_year: str | None, active_labels: frozenset[str]
) -> list[Row]:
    """Parser for statements with labelled sub-sections (e.g. "Transactions"
    / "Bank charges", or "Credits" / "Debit") instead of one continuous
    dated ledger with a running balance -- seen on some credit card
    statements. Each section forces its own sign (see SECTIONED_SIGN_BY_LABEL)
    regardless of whatever sign the statement itself prints on the number.
    """
    rows = [_merge_adjacent_words(r, lambda s: AMOUNT_RE.fullmatch(s)) for r in rows]
    rows = [_merge_adjacent_words(r, lambda s: DATE_RE.fullmatch(s)) for r in rows]

    out: list[Row] = []
    in_section = False
    sign = -1.0
    for row in rows:
        row_text = " ".join(w["text"] for w in row).strip()
        low = row_text.lower()

        if low in active_labels:
            in_section = True
            sign = SECTIONED_SIGN_BY_LABEL.get(low, -1.0)
            continue
        if any(s in low for s in SECTIONED_STOP_WORDS):
            in_section = False
            continue
        if not in_section:
            continue
        if "date" in low and ("description" in low or "amount" in low):
            continue  # the sub-table's own mini header row

        date_word = next((w for w in row if DATE_RE.fullmatch(w["text"].strip())), None)
        if date_word is None:
            continue
        amount_words = [w for w in row if w is not date_word and _token_amount(w["text"]) is not None]
        if not amount_words:
            continue
        amt = _token_amount(amount_words[-1]["text"])
        if amt is None:
            continue
        text_words = [
            w
            for w in row
            if w is not date_word and w not in amount_words and not DATE_RE.fullmatch(w["text"].strip())
        ]
        description = " ".join(w["text"] for w in text_words).strip()

        date_text = date_word["text"].strip()
        if fallback_year and date_text[-1].isalpha():
            date_text = f"{date_text} {fallback_year}"
        out.append(Row(date=date_text, description=description, amount=f"{sign * abs(amt):.2f}"))

    return out


def _split_two_column_layout(words: list[dict]) -> list[list[dict]] | None:
    """Detect a 'newspaper' layout: two side-by-side Date/Description/Amount
    tables sharing one page (seen on some credit card statements, to fit
    more rows per page). If found, returns [left_column_words,
    right_column_words] so each half can be clustered into rows
    independently -- clustering the whole page at once would otherwise merge
    an unrelated left-column transaction and right-column transaction that
    just happen to sit on the same printed line into a single, corrupted row.
    """
    date_hdrs = [w for w in words if w["text"].strip().lower() == "date"]
    if len(date_hdrs) < 2:
        return None
    date_hdrs.sort(key=lambda w: w["x0"])
    left_hdr, right_hdr = date_hdrs[0], date_hdrs[-1]

    xs = [w["x0"] for w in words] + [w["x1"] for w in words]
    span = max(xs) - min(xs) if xs else 0
    if span <= 0 or right_hdr["x0"] - left_hdr["x1"] < span * 0.15:
        return None  # not meaningfully separated -- not actually two tables

    ys = [w["y0"] for w in words] + [w["y1"] for w in words]
    height = max(ys) - min(ys) if ys else 0
    if abs(left_hdr["y0"] - right_hdr["y0"]) > max(height * 0.02, 20):
        return None  # headers aren't on the same line -- not a real column pair

    # The split boundary is the right table's own "Date" x0, not the
    # midpoint between the two "Date" headers -- the left table's later
    # columns (e.g. "Amount", right-aligned) commonly extend well past that
    # midpoint, closer to the right table's Date column than to its own.
    split_x = right_hdr["x0"]
    left = [w for w in words if w["x0"] < split_x]
    right = [w for w in words if w["x0"] >= split_x]
    if not left or not right:
        return None
    return [left, right]


def _rows_from_ocr_page(page, engine, fallback_year: str | None = None) -> list[Row]:
    import numpy as np

    pil_image = page.to_image(resolution=300).original.convert("RGB")
    ocr_result, _elapse = engine(np.array(pil_image))
    if not ocr_result:
        return []

    # OCR word positions are noisier than a native text layer -- a scanned
    # (as opposed to merely rendered-to-image) page can have enough skew
    # that a row's rightmost values (e.g. Balance) drift vertically from
    # its leftmost ones by more than a native PDF ever would. A wider
    # tolerance here keeps those on one row without merging distinct lines
    # (real line spacing in practice is comfortably wider than this drift).
    return _rows_from_words(_words_from_ocr_result(ocr_result), fallback_year, y_tol=15.0)


def _rows_from_words(words: list[dict], fallback_year: str | None, y_tol: float = 6.0) -> list[Row]:
    """Shared entry point for turning a page's words (native text or OCR)
    into rows: split a two-column "newspaper" layout if present, decide once
    -- using the whole page -- whether this is a labelled-sub-section
    statement (e.g. ABSA, or Standard Bank's Titanium credit card) rather
    than a continuous dated ledger, then cluster and parse each column half
    (or the whole page, if there's no column split) the same way.
    """
    all_rows = _cluster_rows_by_position(words, y_tol=y_tol)
    active_labels = _detect_section_labels(all_rows)

    groups = _split_two_column_layout(words)
    if groups is None:
        groups = [words]
        clustered_groups = [all_rows]
    else:
        clustered_groups = [_cluster_rows_by_position(g, y_tol=y_tol) for g in groups]

    if active_labels:
        sectioned: list[Row] = []
        for rows in clustered_groups:
            sectioned.extend(_rows_from_sectioned_rows(rows, fallback_year, active_labels))
        if sectioned:
            return sectioned

    out: list[Row] = []
    for rows in clustered_groups:
        out.extend(_rows_from_word_rows(rows, fallback_year))
    return out


def _infer_fallback_year(pdf) -> str | None:
    """Guess the statement's year, for dates printed without one (e.g. "01 Sep")."""
    from collections import Counter

    years = Counter()
    for page in pdf.pages:
        text = page.extract_text() or ""
        years.update(re.findall(r"\b(?:19|20)\d{2}\b", text))
    if not years:
        return None
    return years.most_common(1)[0][0]


def _is_password_error(exc: BaseException) -> bool:
    """True if exc (or anything it wraps) is pdfminer's password/encryption error."""
    from pdfminer.pdfdocument import PDFEncryptionError, PDFPasswordIncorrect

    seen = exc
    while seen is not None:
        if isinstance(seen, (PDFPasswordIncorrect, PDFEncryptionError)):
            return True
        seen = seen.__cause__ or seen.__context__
    return False


class PdfPasswordRequired(Exception):
    """The PDF is encrypted and needs a password (wrong or no password given)."""


def extract_rows(pdf_path: str, password: str | None = None) -> list[Row]:
    rows: list[Row] = []
    ocr_engine = "not loaded yet"
    try:
        pdf_cm = pdfplumber.open(pdf_path, password=password or "")
    except Exception as exc:
        if _is_password_error(exc):
            raise PdfPasswordRequired(str(pdf_path)) from exc
        raise
    with pdf_cm as pdf:
        fallback_year = _infer_fallback_year(pdf)
        for page in pdf.pages:
            page_rows: list[Row] = []
            text = page.extract_text() or ""
            words = _words_from_page(page) if text.strip() else []

            # A fully scanned page (one page-covering image) can arrive with
            # its own, already-poor-quality OCR text layer baked in by
            # whatever software produced the PDF -- unlike a missing text
            # layer, this can't be detected by `not text.strip()`, so check
            # for the tell-tale corruption pattern instead. That text can't
            # be trusted (the real digits are gone), so prefer re-OCRing the
            # rendered image ourselves rather than parsing the garbage.
            corrupted_scan = bool(words) and bool(page.images) and _page_text_looks_corrupted(words)

            if text.strip() and not corrupted_scan:
                page_rows = _rows_from_words(words, fallback_year)

            if not page_rows and text.strip() and not corrupted_scan:
                for settings in (None, {"vertical_strategy": "text", "horizontal_strategy": "text"}):
                    tables = page.extract_tables(settings) if settings else page.extract_tables()
                    for table in tables:
                        page_rows.extend(_rows_from_table(table))
                    if page_rows:
                        break

            if not page_rows and not corrupted_scan:
                page_rows = _rows_from_text(text)

            if not page_rows:
                # No real text layer at all -- likely a scanned/rendered-as-image
                # page (including some "print to PDF" exports) -- or one whose
                # embedded text turned out to be unusable. Fall back to OCR.
                if ocr_engine == "not loaded yet":
                    ocr_engine = _get_ocr_engine()
                if ocr_engine is not None:
                    page_rows = _rows_from_ocr_page(page, ocr_engine, fallback_year)
                elif corrupted_scan:
                    # No OCR engine available -- fall back to the (known-flawed)
                    # native reading rather than returning nothing for this page.
                    page_rows = _rows_from_words(words, fallback_year)

            rows.extend(page_rows)
    return rows
