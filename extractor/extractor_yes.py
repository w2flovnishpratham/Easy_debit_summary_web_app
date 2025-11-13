import pdfplumber
import pandas as pd
import re


# -------------------------------------------------------
# 🔹 REGEX PATTERNS
# -------------------------------------------------------
date_full_year = re.compile(r"\b\d{1,2}-[A-Za-z]{3}-\d{4}\b")
date_two_digit = re.compile(r"\b\d{1,2}-[A-Za-z]{3}-\d{2}\b")
day_only = re.compile(r"^\d{1,2}$")
month_year_frag = re.compile(r"^[A-Za-z]{3}-\d{2,4}$")
amount_pattern = re.compile(r"(?<!\S)(-?\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?)(?!\S)")
cheque_ref_pattern = re.compile(r"\b([A-Za-z0-9]{5,})\b")


# -------------------------------------------------------
# 🔹 HELPERS
# -------------------------------------------------------
def normalize_year_token(tok):
    if date_two_digit.search(tok):
        d, m, y = tok.split("-")
        y = int(y) + 2000 if int(y) < 100 else int(y)
        return f"{d}-{m}-{y}"
    return tok


def find_best_date_from_cells(cells):
    for c in cells:                                # full-year
        if c and (m := date_full_year.search(c)):
            return m.group(0)

    for c in cells:                                # two-digit
        if c and (m := date_two_digit.search(c)):
            return normalize_year_token(m.group(0))

    for a, b in zip(cells, cells[1:]):             # day + month-year
        a, b = a.strip(), b.strip()
        if day_only.match(a) and month_year_frag.match(b):
            return normalize_year_token(f"{a}-{b}")
        if month_year_frag.match(a) and day_only.match(b):
            return normalize_year_token(f"{b}-{a}")

    # fallback
    joined = " ".join([c for c in cells if c])
    if m := date_full_year.search(joined):
        return m.group(0)
    if m := date_two_digit.search(joined):
        return normalize_year_token(m.group(0))

    return None


def parse_single_amount(text):
    if not text:
        return 0.0

    t = str(text).replace("\u00A0", " ").strip()
    if not t:
        return 0.0

    t = re.sub(r"\(([\d,]+\.\d{1,2})\)", r"-\1", t)      # (xxxx) → -xxxx

    m = amount_pattern.search(t)
    return float(m.group(1).replace(",", "")) if m else 0.0


def extract_cheque_and_narration(text):
    ref = ""
    for token in cheque_ref_pattern.findall(text):
        if token.upper() not in ("FROM", "TO", "UPI", "NA", "INT", "SWEEP", "CREDIT", "DEBIT"):
            ref = token
            text = text.replace(token, "", 1)
            break
    narration = re.sub(r"\s{2,}", " ", text).strip()
    return ref, narration


# -------------------------------------------------------
# 🔹 MAIN EXTRACTOR WITH SUMMARY
# -------------------------------------------------------
def extract_yes_transactions(pdf_path):
    rows = []
    last_record = None

    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            for table in (page.extract_tables() or []):
                for raw_row in table:
                    if raw_row is None:
                        continue

                    cells = [(c or "").strip() for c in raw_row]
                    if not any(cells):
                        continue

                    best_date = find_best_date_from_cells(cells)
                    row_text = " ".join([c for c in cells if c])

                    if best_date:   # NEW ROW
                        withdrawal = parse_single_amount(cells[-3]) if len(cells) >= 3 else 0.0
                        deposit    = parse_single_amount(cells[-2]) if len(cells) >= 2 else 0.0
                        balance    = parse_single_amount(cells[-1]) if len(cells) >= 1 else 0.0

                        cheque_ref, narration = extract_cheque_and_narration(row_text)
                        if not narration and len(cells) >= 4:
                            narration = cells[3]

                        record = {
                            "Date": best_date,
                            "ChequeRef": cheque_ref,
                            "Narration": narration,
                            "Withdrawal": withdrawal,
                            "Deposit": deposit,
                            "Balance": balance,
                        }
                        rows.append(record)
                        last_record = record

                    else:           # CONTINUATION ROW
                        if last_record:
                            last_record["Narration"] = (
                                last_record["Narration"] + " " + row_text
                            ).strip()

    df = pd.DataFrame(rows)
    if df.empty:
        return df, {}

    # -----------------------------
    # CLEAN & SORT
    # -----------------------------
    df["Withdrawal"] = pd.to_numeric(df["Withdrawal"], errors="coerce").fillna(0.0)
    df["Deposit"]    = pd.to_numeric(df["Deposit"], errors="coerce").fillna(0.0)
    df["Balance"]    = pd.to_numeric(df["Balance"], errors="coerce").fillna(0.0)

    df["Narration"] = (
        df["Narration"]
        .str.replace(r"\b\d{1,2}-[A-Za-z]{3}-\d{4}\b", "", regex=True)
        .str.replace(r"\s{2,}", " ", regex=True)
        .str.strip()
    )

    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    df = df.dropna(subset=["Date"]).sort_values("Date")

    # -------------------------------------------------------
    # 🔥 SUMMARY VALUES
    # -------------------------------------------------------
    opening_balance = float(df["Balance"].iloc[0])
    closing_balance = float(df["Balance"].iloc[-1])
    total_debit = float(df["Withdrawal"].sum())
    total_credit = float(df["Deposit"].sum())

    # 🔥 DAILY AGGREGATION
    daily_df = df.copy()
    daily_df["Date"] = daily_df["Date"].dt.date
    daily_summary = (
        daily_df.groupby("Date")[["Deposit", "Withdrawal"]].sum().reset_index()
    )
    daily_data = daily_summary.to_dict(orient="records")

    # 🔥 TOP TRANSACTIONS
    top_debits = df[df["Withdrawal"] > 0].sort_values("Withdrawal", ascending=False).head(5)[
        ["Date", "Narration", "Withdrawal"]
    ].to_dict(orient="records")

    top_credits = df[df["Deposit"] > 0].sort_values("Deposit", ascending=False).head(5)[
        ["Date", "Narration", "Deposit"]
    ].to_dict(orient="records")

    # -------------------------------------------------------
    # OUTPUT
    # -------------------------------------------------------
    summary = {
        "opening_balance": opening_balance,
        "closing_balance": closing_balance,
        "total_credit": total_credit,
        "total_debit": total_debit,
        "daily_data": daily_data,
        "top_debits": top_debits,
        "top_credits": top_credits,
    }

    return df, summary
