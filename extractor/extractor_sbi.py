import pdfplumber
import pandas as pd

def extract_sbi_transactions(pdf_path):
    rows = []

    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            tables = page.extract_tables()
            if not tables:
                continue
            for table in tables:
                for row in table:
                    if not row or "Date" in str(row[0]):
                        continue
                    rows.append(row)

    # Build DataFrame
    df = pd.DataFrame(
        rows,
        columns=[
            "Date",
            "Details",
            "Ref No./Cheque No",
            "Withdrawal",
            "Deposit",
            "Balance"
        ]
    )

    df = df.dropna(how='all').reset_index(drop=True)

    # Normalize narration
    df["Details"] = (
        df["Details"]
        .astype(str)
        .str.replace(r"\s+", " ", regex=True)
        .str.strip()
    )

    # Normalize numeric columns
    for col in ["Withdrawal", "Deposit", "Balance"]:
        df[col] = (
            df[col]
            .fillna("0")
            .astype(str)
            .str.replace(",", "")
            .str.replace("-", "0")
            .str.strip()
            .replace("", "0")
            .astype(float)
        )

    # Transaction type
    df["Transaction Type"] = df.apply(
        lambda x: "DEBIT" if x["Withdrawal"] > 0 else (
            "CREDIT" if x["Deposit"] > 0 else ""
        ),
        axis=1
    )

    # Ensure numeric consistency
    for col in ["Withdrawal", "Deposit", "Balance"]:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
    
    return df
