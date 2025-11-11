import re
from typing import Any, Dict, List

import pdfplumber
import pandas as pd


DATE_PATTERN = r"(?:\b\d{2}/\d{2}/\d{2,4}\b|\b\d{2}\s+[A-Za-z]{3}\s+\d{2,4}\b)"
AMOUNT_PATTERN = r"(?<![A-Z0-9@])\b\d{1,3}(?:,\d{3})*\.\d{2}\b(?![A-Z0-9@])"
HEADER_KEYWORDS = [
    "HDFC BANK",
    "STATEMENT OF ACCOUNT",
    "STATEMENTSUMMARY",
    "STATEMENT SUMMARY",
    "STATEMENT",
    "SUMMARY",
    "ACCOUNT BRANCH",
    "CUST ID",
    "ACCOUNT NO",
    "NOMINATION",
    "MICR",
    "GSTN",
    "PAGE NO",
]


def extract_hdfc_transactions(pdf_path: str):
    lines = _extract_lines(pdf_path)
    transactions = _group_transactions(lines)
    enriched = _assign_amounts(transactions)
    df = _finalize_dataframe(enriched)

    opening_balance = df["Balance"].iloc[0] if not df.empty else 0.0
    closing_balance = df["Balance"].iloc[-1] if not df.empty else 0.0
    total_credit = df["Credit"].sum()
    total_debit = df["Debit"].sum()

    daily_data = (
        df.groupby(df["Date"].dt.date)[["Credit", "Debit"]]
        .sum()
        .reset_index()
    )
    daily_data.columns = ["Date", "Credit", "Debit"]
    daily_data["Date"] = pd.to_datetime(daily_data["Date"])

    top_debits = (
        df[df["Debit"] > 0][["Date", "Details", "Debit"]]
        .sort_values("Debit", ascending=False)
        .head(5)
    )
    top_credits = (
        df[df["Credit"] > 0][["Date", "Details", "Credit"]]
        .sort_values("Credit", ascending=False)
        .head(5)
    )

    summary = {
        "opening_balance": opening_balance,
        "closing_balance": closing_balance,
        "total_credit": total_credit,
        "total_debit": total_debit,
        "daily_data": daily_data,
        "top_debits": top_debits.to_html(index=False, classes="table"),
        "top_credits": top_credits.to_html(index=False, classes="table"),
    }

    return df, summary


def _extract_lines(pdf_path: str) -> List[str]:
    relevant = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if not text:
                continue
            for raw in text.split("\n"):
                line = raw.strip()
                if not line:
                    continue
                if re.match(DATE_PATTERN, line):
                    relevant.append(line)
                    continue
                upper = line.upper()
                if any(keyword in upper for keyword in HEADER_KEYWORDS):
                    continue
                relevant.append(line)
    return relevant


def _looks_like_summary(text: str) -> bool:
    if not text:
        return False
    normalized = text.replace(" ", "").upper()
    return any(keyword in normalized for keyword in ("STATEMENTSUMMARY", "STATEMENTOFACCOUNT", "SUMMARY"))


def _group_transactions(lines: List[str]) -> List[Dict[str, Any]]:
    transactions = []
    current = None

    for raw_line in lines:
        line = raw_line.strip()
        if re.match(DATE_PATTERN, line):
            raw_upper = line.upper().replace(" ", "")
            content = re.sub(DATE_PATTERN, "", line, count=1).strip()
            content_upper = content.upper().replace(" ", "")

            if _looks_like_summary(raw_upper) or _looks_like_summary(content_upper):
                current = None
                continue

            if current:
                transactions.append(current)

            current = {
                "Date": re.findall(DATE_PATTERN, line)[0],
                "Narration": content,
                "Withdrawal": "",
                "Deposit": "",
                "Closing Balance": "",
                "Raw Line": line,
            }
            continue

        if not current:
            continue

        upper_clean = line.replace(" ", "").upper()

        if _looks_like_summary(upper_clean):
            transactions.append(current)
            current = None
            continue

        if re.search(AMOUNT_PATTERN, line):
            current["Narration"] = current["Narration"].strip()
            continue

        # Preserve multi-line narration with newline separator
        current["Narration"] = f"{current['Narration']}\n{line}".strip()

    if current and not _looks_like_summary(current["Narration"].upper().replace(" ", "")):
        transactions.append(current)

    return transactions


def _assign_amounts(transactions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    prev_closing = None
    tolerance = 0.01

    for tx in transactions:
        nums = re.findall(AMOUNT_PATTERN, tx["Narration"])

        if not nums:
            continue

        closing_str = nums[-1]
        closing_val = _to_float(closing_str)
        second_str = nums[-2] if len(nums) >= 2 else None
        second_val = _to_float(second_str) if second_str else None
        third_str = nums[-3] if len(nums) >= 3 else None

        tx["Withdrawal"] = ""
        tx["Deposit"] = ""
        tx["Closing Balance"] = closing_str if closing_str else ""

        if second_str is None:
            tx["Closing Balance"] = closing_str
        else:
            assigned = False
            if second_val is not None and closing_val is not None:
                if second_val > closing_val + tolerance:
                    tx["Deposit"] = second_str
                    assigned = True
                elif second_val < closing_val - tolerance:
                    tx["Withdrawal"] = second_str
                    assigned = True

            if not assigned and prev_closing is not None and closing_val is not None and second_val is not None:
                if closing_val > prev_closing + tolerance:
                    tx["Deposit"] = second_str
                else:
                    tx["Withdrawal"] = second_str
                assigned = True

            if not assigned:
                narr_upper = tx["Narration"].upper()
                if "DEPRET" in narr_upper or "RET-" in narr_upper or ("FUND" in narr_upper and "RET" in narr_upper):
                    tx["Withdrawal"] = second_str
                elif any(keyword in narr_upper for keyword in ("REVERS", "REFUND", "CR", "CREDIT", "REVERSED", "DEP")):
                    tx["Deposit"] = second_str
                else:
                    tx["Withdrawal"] = second_str

            if third_str and not tx["Withdrawal"]:
                tx["Withdrawal"] = third_str

        tx["Narration"] = re.sub(AMOUNT_PATTERN, "", tx["Narration"]).strip()

        if tx["Closing Balance"]:
            prev_val = _to_float(tx["Closing Balance"])
            if prev_val is not None:
                prev_closing = prev_val

    return transactions


def _finalize_dataframe(transactions: List[Dict[str, Any]]) -> pd.DataFrame:
    df = pd.DataFrame(transactions)
    if df.empty:
        return pd.DataFrame(columns=["Date", "Details", "Debit", "Credit", "Balance", "Transaction Type"])

    def _clean_multiline(text: str) -> str:
        parts = [part.strip() for part in str(text).splitlines() if part.strip()]
        return "\n".join(parts)

    df["Narration"] = df["Narration"].apply(_clean_multiline)
    df = df[df["Date"].notna()].reset_index(drop=True)

    for col in ["Withdrawal", "Deposit", "Closing Balance"]:
        df[col] = (
            df[col]
            .astype(str)
            .str.replace(",", "", regex=False)
            .str.strip()
            .replace({"": "0"})
            .astype(float)
        )

    df["Date"] = pd.to_datetime(df["Date"], dayfirst=True, errors="coerce")
    df = df.dropna(subset=["Date"]).reset_index(drop=True)

    df["Prev Balance"] = df["Closing Balance"].shift(1)
    df["Balance Change"] = df["Closing Balance"] - df["Prev Balance"]

    for idx, row in df.iterrows():
        if pd.isna(row["Prev Balance"]):
            continue
        if row["Balance Change"] > 0 and row["Withdrawal"] > 0 and row["Deposit"] == 0:
            df.at[idx, "Deposit"] = row["Withdrawal"]
            df.at[idx, "Withdrawal"] = 0.0
        elif row["Balance Change"] < 0 and row["Deposit"] > 0 and row["Withdrawal"] == 0:
            df.at[idx, "Withdrawal"] = row["Deposit"]
            df.at[idx, "Deposit"] = 0.0

    df["Transaction Type"] = df.apply(
        lambda r: "CREDIT" if r["Deposit"] > 0 else ("DEBIT" if r["Withdrawal"] > 0 else ""),
        axis=1,
    )

    df["Details"] = df["Narration"]
    df["Credit"] = df["Deposit"]
    df["Debit"] = df["Withdrawal"]
    df["Balance"] = df["Closing Balance"]

    return df[["Date", "Details", "Debit", "Credit", "Balance", "Transaction Type"]]


def _to_float(value: Any) -> float:
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _is_zero(value: str) -> bool:
    try:
        return float(value.replace(",", "")) == 0.0
    except ValueError:
        return False
