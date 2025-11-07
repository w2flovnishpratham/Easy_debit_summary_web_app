import pdfplumber
import pandas as pd
import re
from datetime import datetime

def extract_icici_transactions(pdf_path):
    rows = []
    
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            tables = page.extract_tables()
            if not tables:
                continue
            
            for table in tables:
                for row in table:
                    if not row:
                        continue

                    # Ensure list has exactly 4 columns: Date, Description, Amount, Type
                    row = [cell if cell is not None else "" for cell in row]
                    if len(row) < 4:
                        row += [""] * (4 - len(row))
                    else:
                        row = row[:4]

                    # Skip header
                    if "date" in row[0].lower():
                        continue

                    rows.append(row)

    # Create DataFrame
    df = pd.DataFrame(rows, columns=["Date", "Details", "Amount", "Type"])

    # Remove blank rows
    df = df.dropna(how='all').reset_index(drop=True)

    # Clean text columns
    df["Details"] = (
        df["Details"]
        .astype(str)
        .str.replace(r"\s+", " ", regex=True)
        .str.strip()
    )

    df["Type"] = df["Type"].astype(str).str.upper().str.strip()

    # ✅ Clean Amount column properly
    df["Amount"] = (
        df["Amount"]
        .astype(str)
        .str.replace(",", "", regex=False)
        .str.replace(r"[^\d.]", "", regex=True)
        .str.strip()
    )

    df["Amount"] = pd.to_numeric(df["Amount"], errors="coerce").fillna(0.0)

    # ✅ Convert to Debit / Credit based on CR/DR
    df["Debit"] = df.apply(lambda x: x["Amount"] if x["Type"] == "DR" else 0.0, axis=1)
    df["Credit"] = df.apply(lambda x: x["Amount"] if x["Type"] == "CR" else 0.0, axis=1)

    # ✅ Convert Date column to datetime (handle multiple ICICI date formats)
    df["Date"] = pd.to_datetime(df["Date"], errors='coerce')
    df = df.dropna(subset=["Date"])
    df = df.sort_values(by="Date")

    # ✅ For v1: Assume opening balance = 0
    opening_balance = 0.0
    
    # ✅ Calculate Running Balance
    df["Balance"] = df["Credit"].cumsum() - df["Debit"].cumsum() + opening_balance

    # ✅ Compute summary values
    closing_balance = float(df["Balance"].iloc[-1])
    total_debit = float(df["Debit"].sum())
    total_credit = float(df["Credit"].sum())

    # ✅ Daily data aggregation
    daily_data = df.groupby(df["Date"].dt.date)[["Credit", "Debit"]].sum().reset_index()
    daily_data.columns = ["Date", "Credit", "Debit"]
    daily_data["Date"] = pd.to_datetime(daily_data["Date"])

    # ✅ Top transactions
    top_debits = df[df["Debit"] > 0].sort_values(by="Debit", ascending=False).head(5)[["Date", "Details", "Debit"]]
    top_credits = df[df["Credit"] > 0].sort_values(by="Credit", ascending=False).head(5)[["Date", "Details", "Credit"]]

    return df, {
        "opening_balance": opening_balance,
        "closing_balance": closing_balance,
        "total_credit": total_credit,
        "total_debit": total_debit,
        "daily_data": daily_data,
        "top_debits": top_debits.to_html(index=False, classes='table'),
        "top_credits": top_credits.to_html(index=False, classes='table'),
    }