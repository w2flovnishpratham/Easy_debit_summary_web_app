import pdfplumber
import pandas as pd
import re
from datetime import datetime

def extract_hdfc_transactions(pdf_path):
    # Define patterns
    date_pattern = r"\b\d{2}/\d{2}/\d{2}\b"
    amount_pattern = r"(?<![A-Z0-9@])\b\d{1,3}(?:,\d{3})*\.\d{2}"
    account_number_pattern = r"\d{9,18}"

    data = []

    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if not text:
                continue

            lines = text.split('\n')
            for line in lines:
                if re.search(date_pattern, line):
                    try:
                        date_match = re.search(date_pattern, line).group()
                        amounts = re.findall(amount_pattern, line)

                        if len(amounts) >= 2:
                            credit_or_debit = amounts[-2]
                            closing_balance = amounts[-1]
                            desc = line.replace(date_match, '').strip()

                            data.append([
                                date_match,
                                desc,
                                credit_or_debit,
                                closing_balance
                            ])
                    except Exception as e:
                        print(f"Error processing line: {line}\n{e}")

    # Create DataFrame
    df = pd.DataFrame(data, columns=[
        "Date", "Transaction Details", "Amount", "Closing Balance"
    ])

    # Clean and convert
    df["Date"] = pd.to_datetime(df["Date"], format="%d/%m/%y", errors='coerce')
    df = df.dropna(subset=["Date"])
    df = df.sort_values(by="Date")

    df["Amount"] = df["Amount"].str.replace(",", "").astype(float)
    df["Closing Balance"] = df["Closing Balance"].str.replace(",", "").astype(float)

    # Infer Credit/Debit
    transaction_type = []
    credit_list = []
    debit_list = []

    for i in range(len(df)):
        if i == 0:
            transaction_type.append("Unknown")
            credit_list.append(0)
            debit_list.append(0)
        else:
            prev_balance = df.loc[i - 1, "Closing Balance"]
            current_balance = df.loc[i, "Closing Balance"]
            amt = df.loc[i, "Amount"]

            if abs(prev_balance + amt - current_balance) < 0.05:
                transaction_type.append("Credit")
                credit_list.append(amt)
                debit_list.append(0)
            elif abs(prev_balance - amt - current_balance) < 0.05:
                transaction_type.append("Debit")
                credit_list.append(0)
                debit_list.append(amt)
            else:
                transaction_type.append("Unknown")
                credit_list.append(0)
                debit_list.append(0)

    df["Transaction Type"] = transaction_type
    df["Credit"] = credit_list
    df["Debit"] = debit_list

    # Compute summary values
    opening_balance = df.iloc[0]["Closing Balance"]
    closing_balance = df.iloc[-1]["Closing Balance"]
    total_credit = df["Credit"].sum()
    total_debit = df["Debit"].sum()

    daily_data = df.groupby(df["Date"].dt.date)[["Credit", "Debit"]].sum().reset_index()
    daily_data.columns = ["Date", "Credit", "Debit"]
    daily_data["Date"] = pd.to_datetime(daily_data["Date"])

    top_debits = df.sort_values(by="Debit", ascending=False).head(5)[["Date", "Transaction Details", "Debit"]]
    top_credits = df.sort_values(by="Credit", ascending=False).head(5)[["Date", "Transaction Details", "Credit"]]

    return df, {
        "opening_balance": opening_balance,
        "closing_balance": closing_balance,
        "total_credit": total_credit,
        "total_debit": total_debit,
        "daily_data": daily_data,
        "top_debits": top_debits.to_html(index=False, classes='table'),
        "top_credits": top_credits.to_html(index=False, classes='table'),
    }
