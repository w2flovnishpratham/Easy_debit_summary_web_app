import pdfplumber
import pandas as pd
import re


def extract_indus_transactions(pdf_path):
    lines = []

    # Read all lines of text (NOT tables)
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if text:
                for line in text.split("\n"):
                    lines.append(line.strip())

    # Date pattern: 07 Dec 2025
    date_pattern = r"^\d{1,2}\s+[A-Za-z]{3}\s+\d{4}"

    transactions = []

    for line in lines:
        if re.match(date_pattern, line):
            parts = line.split()
            # first 3 parts = date
            date = " ".join(parts[:3])
            
            # remainder row content
            rest = line[len(date):].strip()

            # Extract Withdrawal / Deposit / Balance
            nums = re.findall(r"\d+\.\d{2}", rest)
            if len(nums) >= 3:
                withdrawal, deposit, balance = nums[-3:]
            else:
                continue
            
            # Remove numeric parts to keep details only
            details = re.sub(r"\d+\.\d{2}", "", rest).strip()

            transactions.append([date, details, withdrawal, deposit, balance])

    # Build DataFrame
    df = pd.DataFrame(transactions, columns=["Date", "Details", "Withdrawal", "Deposit", "Balance"])

    # Clean numeric
    for col in ["Withdrawal", "Deposit", "Balance"]:
        df[col] = df[col].astype(float)

    # Parse date
    df["Date"] = pd.to_datetime(df["Date"], format="%d %b %Y")

    # Determine Transaction Type
    df["Transaction Type"] = df.apply(
        lambda r: "DEBIT" if r["Withdrawal"] > 0 else ("CREDIT" if r["Deposit"] > 0 else ""),
        axis=1
    )

    # Rename financial columns
    df["Debit"] = df["Withdrawal"]
    df["Credit"] = df["Deposit"]

    return df[["Date", "Details", "Debit", "Credit", "Balance", "Transaction Type"]]

