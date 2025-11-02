# extractor.py

import pdfplumber
import pandas as pd
import re

def extract_data_from_pdf(input_pdf_path: str) -> pd.DataFrame:
    date_pattern = r"\b\d{2}/\d{2}/\d{2}\b"
    amount_pattern = r"(?<![A-Z0-9@])\b\d{1,3}(?:,\d{3})*\.\d{2}"
    account_number_pattern = r"\d{9,18}"

    data = []

    with pdfplumber.open(input_pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if not text:
                continue

            lines = text.split('\n')
            for line in lines:
                if re.search(date_pattern, line):
                    date_match = re.search(date_pattern, line).group()
                    amounts = re.findall(amount_pattern, line)
                    account_numbers = re.findall(account_number_pattern, line)

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

        df = pd.DataFrame(data, columns=[
        "Date", "Description", "Amount", "Closing Balance"
    ])

    # Convert numeric columns to float for calculations
    df["Amount"] = df["Amount"].str.replace(",", "").astype(float)
    df["Closing Balance"] = df["Closing Balance"].str.replace(",", "").astype(float)

    # Infer Credit/Debit based on balance comparison
    transaction_types = []
    for i in range(len(df)):
        if i == 0:
            transaction_types.append("Unknown")  # Can't determine for first row
        else:
            prev_balance = df.loc[i - 1, "Closing Balance"]
            current_balance = df.loc[i, "Closing Balance"]
            amt = df.loc[i, "Amount"]

            # If previous balance + amount = current, it's Credit
            # If previous balance - amount = current, it's Debit
            # If mismatch, fallback to comparison
            if abs(prev_balance + amt - current_balance) < 0.05:
                transaction_types.append("Credit")
            elif abs(prev_balance - amt - current_balance) < 0.05:
                transaction_types.append("Debit")
            else:
                # Fallback: increase = Credit, decrease = Debit
                if current_balance > prev_balance:
                    transaction_types.append("Credit?")
                else:
                    transaction_types.append("Debit?")

    df["Transaction Type"] = transaction_types
    return df

