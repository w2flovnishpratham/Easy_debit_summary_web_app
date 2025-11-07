import pdfplumber
import pandas as pd
import re
from datetime import datetime

def extract_sbi_transactions(pdf_path):
    rows = []
    
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            tables = page.extract_tables()
            if not tables:
                continue
            
            for table in tables:
                for row in table:
                    # Skip header rows / empty rows
                    if not row or "Date" in str(row[0]):
                        continue
                    
                    rows.append(row)

    # Create DataFrame
    df = pd.DataFrame(rows, columns=["Date", "Details", "Ref No./Cheque No", "Debit", "Credit", "Balance"])

    # Remove fully blank rows
    df = df.dropna(how='all').reset_index(drop=True)

    # ✅ Clean narration into one single line
    df["Details"] = (
        df["Details"]
        .astype(str)
        .str.replace(r"\s+", " ", regex=True)  # Merge line breaks & spaces
        .str.strip()
    )

    # ✅ Normalize amount fields (Debit, Credit, Balance)
    for col in ["Debit", "Credit", "Balance"]:
        df[col] = (
            df[col]
            .fillna("0")
            .astype(str)
            .str.replace(",", "", regex=False)  # Remove commas
            .str.replace("-", "0", regex=False) # Replace "-" with 0
            .str.strip()
            .replace("", "0")
            .astype(float)
        )

    # ✅ Create Transaction Type column
    df["Transaction Type"] = df.apply(
        lambda x: "DEBIT" if x["Debit"] > 0 else ("CREDIT" if x["Credit"] > 0 else ""),
        axis=1
    )

    # Ensure numeric columns
    for col in ["Debit", "Credit", "Balance"]:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)

    # ✅ Convert Date column to datetime
    df["Date"] = pd.to_datetime(df["Date"], format="%d %b %Y", errors='coerce')
    df = df.dropna(subset=["Date"])
    df = df.sort_values(by="Date")

    # ✅ Compute summary values
    opening_balance = df.loc[0, "Balance"]
    closing_balance = df["Balance"].iloc[-1]
    
    # ✅ Totals (excluding first row, because first row is already BALANCE AFTER transaction)
    total_debit = df["Debit"].iloc[1:].sum()
    total_credit = df["Credit"].iloc[1:].sum()

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