import pdfplumber
import pandas as pd
import re
from datetime import datetime
from typing import Any

def extract_axis_transactions(pdf_path):
    rows = []

    with pdfplumber.open(pdf_path) as pdf:
        for page_num, page in enumerate(pdf.pages, start=1):
            tables = page.extract_tables()
         

            for table in tables:
                for row in table:
            
                    if row and len(row) >= 6 and re.match(r"\d{2}-\d{2}-\d{4}", row[0]):
                        rows.append(row[:6])

    df = pd.DataFrame(rows, columns=["Date", "Details", "Ref No./Cheque No", "Debit", "Credit", "Balance"])

    # Clean details
    df["Details"] = df["Details"].astype(str).str.replace(r"\s+", " ", regex=True)

    # Replace NaNs and clean numeric columns. Some Axis statements use "-" or blanks for zero,
    # but we must preserve genuine negative numbers, so rely on a sanitizer instead of naive replace.
    for col in ["Debit", "Credit", "Balance"]:
        df[col] = _clean_amount_series(df[col])

    # Parse date
    df["Date"] = pd.to_datetime(df["Date"], format="%d-%m-%Y", errors="coerce")
    df = df.dropna(subset=["Date"]).sort_values(by="Date")

    # Add Transaction Type
    df["Transaction Type"] = df.apply(
        lambda x: "DEBIT" if x["Debit"] > 0 else ("CREDIT" if x["Credit"] > 0 else ""),
        axis=1
    )

    # Calculate summary values
    opening_balance = df.iloc[0]["Balance"] if not df.empty else 0
    closing_balance = df.iloc[-1]["Balance"] if not df.empty else 0
    total_credit = df["Credit"].sum()
    total_debit = df["Debit"].sum()

    # Daily summary for plotting
    daily_df = df.copy()
    daily_df["Date"] = pd.to_datetime(daily_df["Date"]).dt.date
    daily_summary = daily_df.groupby("Date").agg({"Credit": "sum", "Debit": "sum"}).reset_index()
    daily_data = daily_summary.to_dict(orient="records")

    # Top 5 credits and debits
    top_credits = df[df["Credit"] > 0].sort_values(by="Credit", ascending=False).head(5)[["Date", "Details", "Credit"]]
    top_debits = df[df["Debit"] > 0].sort_values(by="Debit", ascending=False).head(5)[["Date", "Details", "Debit"]]

    summary_dict = {
        "opening_balance": opening_balance,
        "closing_balance": closing_balance,
        "total_credit": total_credit,
        "total_debit": total_debit,
        "daily_data": daily_data,
        "top_credits": top_credits.to_html(index=False, classes='table table-sm table-striped'),
        "top_debits": top_debits.to_html(index=False, classes='table table-sm table-striped'),
    }

    return df, summary_dict


def _clean_amount_series(series: pd.Series) -> pd.Series:
    def _normalize(value: Any) -> float:
        if pd.isna(value):
            return 0.0
        text = str(value).strip()
        if text in {"", "-", "--", "—"}:
            return 0.0
        text = text.replace(",", "")
        if text.startswith("(") and text.endswith(")"):
            text = f"-{text[1:-1]}"
        text = re.sub(r"[^\d\.\-]", "", text)
        try:
            return float(text)
        except ValueError:
            return 0.0

    return series.astype(object).apply(_normalize).astype(float)
