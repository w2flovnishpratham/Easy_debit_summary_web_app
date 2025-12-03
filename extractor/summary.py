import re
from typing import Any, Callable, Dict, Tuple

import pandas as pd

from .extractor_axis import extract_axis_transactions
from .extractor_hdfc import extract_hdfc_transactions
from .extractor_icici import extract_icici_transactions
from .extractor_sbi import extract_sbi_transactions
from .extractor_yes import extract_yes_transactions


ExtractorFn = Callable[[str], Any]

EXTRACTOR_REGISTRY: Dict[str, ExtractorFn] = {
    "HDFC": extract_hdfc_transactions,
    "ICICI": extract_icici_transactions,
    "SBI": extract_sbi_transactions,
    "AXIS": extract_axis_transactions,
    "YES": extract_yes_transactions,
}

RENAME_MAP = {
    "transaction details": "Details",
    "description": "Details",
    "narration": "Details",
    "particulars": "Details",
    "txn date": "Date",
    "transaction date": "Date",
    "value date": "Date",
    "closing balance": "Balance",
    "balance amount": "Balance",
    "withdrawal": "Debit",
    "deposit": "Credit",
}

# PAYMENT CATEGORY PATTERNS (ordered: specific --> general)
PAYMENT_CATEGORY_PATTERNS = [
    ("CHARGES", r"\bCHARGE\b|\bFEE\b|\bDEBITCARDISSUANCEFEE\b|\bSERVICE CHARGE\b|\bCHG(?:S)?\b|\bTAX CHARGE\b"),
    ("REFUND", r"\bREFUND\b|\bREFUND?RN?\b|\bREFUND-?RN\b|\bREVERSE\b|\bREVERSAL\b|\bREVERS\b|\bRTN\b|\bCREDIT-REVERSAL\b"),
    ("CHEQUE", r"\bCHQDEP\b|\bCHQDEPRET\b|\bCHQDEPRET-FUNDSINSUFFICIENT\b|\bCHEQUE\b|\bCHQ\b"),
    ("CARD/POS", r"\bPOS\b|\bDEBITCARD\b|\bCREDITCARD\b|\bDEBIT CARD\b|\bCREDIT CARD\b|\bCARD PAYMENT\b|\bDCINTLPOSTXNDCC\b"),
    ("ATM", r"\bATM\b|\bATW\b|\bNFS\b|\bATM WITHDRAWAL\b|\bCASH WITHDRAWAL\b"),
    ("EMI", r"\bEMI\b|\bE M I\b|\bINSTAL?L?MENT\b|\bLOAN INSTALMENT\b|\bLOAN EMI\b"),
    ("SALARY", r"\bSALARY\b|\bPAYROLL\b|\bCREDIT-SALARY\b|\bSAL\.?\/|SAL\/|\bEMPLOYEE PAY\b"),
    ("TAX", r"\b(TAX PAYMENT|TAX PAID|TDS(?: DEDUCTED)?|INCOME TAX|GST PAYMENT|GST PAID)\b"),  # strict, avoids GSTIN
    ("SWEEP/INTEREST", r"\bSWEEP-?IN\b|\bSWEEP\b|\bINT\. ON SWCR\b|\bINTEREST\b|\bINT\b"),
    ("RAZORPAY", r"\bRAZORPAY\b|\bRZP\b|\bPAYVIA RAZORPAY\b|\bPAYVIARAZORPAY\b"),
    ("IMPS", r"\bIMPS\b|\bIMPS-?\d+\b|\bIMPS/|\bIMPSP2P\b"),
    ("NEFT", r"\bNEFT\b|\bNEFT/|\bNEFT-CREDIT\b|\bNEFTDR\b|\bNEFTDR-"),
    ("RTGS", r"\bRTGS\b"),
    ("ACH", r"\bACH\b|\bECS\b|\bACH-CR|\bACH-DR|\bECS-CR|\bECS-DR"),
    ("SUBSCRIPTION", r"\bSUBSCRIPTION\b|\bSUBS\b|\bRENEWAL\b|\bAUTOPAY\b|\bMANDATEEXECUTE\b|\bMONTHLY SUB\b|\bMEMBERSHIP\b|\bPLAYSTORE\b|\bGOOGLECLOUD\b|\bOPENAI\b"),
    ("RECHARGE", r"\bRECHARGE\b|\bTOP-?UP\b|\bTOPUP\b|\bRECHG\b|\bMOBILE RECHARGE\b|\bAIRTEL\b|\bVODAFONE\b|\bJIO\b"),
    ("WALLET_TOPUP", r"\bWALLET\b|\bPAYTM WALLET\b|\bMOBIQWIK\b|\bPHONEPE WALLET\b|\bFREECHARGE\b|\bWALLET TOP-?UP\b"),
    ("GROCERY", r"\bGROCERY\b|\bGROC?ERY\b|\bGROCERIES\b|\bSUPER MARKET\b|\bSUPERMARKET\b|\bBIGBASKET\b|\bFLIPKART ?GROCERY\b"),
    ("FUEL", r"\bFUEL\b|\bPETROL\b|\bDIESEL\b|\bPETRO\b|\bHP ?PETROL\b|\bIOCL?\b|\bBHARAT PETROLEUM\b|\bARAT PETROLEUM\b"),
    ("TRANSFER", r"\bTRF\b|\bFUND TRANSFER\b|\bFT\b|\bIFT\b|\bTRANSFER\b|\bTRANSFER TO\b|\bTRANSFER FROM\b"),
    ("E-COMMERCE", r"\bFLIPKART\b|\bFKART\b|\bAMAZON\b|\bAMZN\b|\bAMAZONPAY\b|\bAMZNMKT\b|\bAMAZONIN\b|\bPADDLE\.NET\b|\bPADDLE\b"),
    ("BROKERAGE", r"\bZERODHA\b|\bBROKING\b|\bSECURIT(?:Y|IES)\b|\bBROKER\b"),
    ("CASH", r"\bCASH\b|\bCASH WITHDRAWAL\b|\bCASH WITHDRAWN\b"),
    ("OTHER", r".*"),  # fallback — keep last
]

# MERCHANT PATTERNS (expanded)
MERCHANT_PATTERNS = {
    # common delivery / grocery / food
    "Blinkit": r"BLINKIT|BLNKIT",
    "Swiggy": r"SWIGGY|SWGYY",
    "Zomato": r"ZOMATO|ZMT",
    "BigBasket": r"BIGBASKET|BB\.?DAILY",
    "Grofers": r"GROFERS|GROFERSINDIA",
    # ecommerce / marketplace
    "Amazon": r"AMAZON|AMZN|AMAZONPAY|AMZNMKT|AMAZONIN|WWWAMAZONIN|AMZDQR",
    "Flipkart": r"FLIPKART|FKART|FLIPKARTPAYMENT",
    "Paddle": r"PADDLE\.NET|PADDLE",
    # travel / rides
    "Uber": r"UBER|UBR",
    "Ola": r"\BOLA\b|OLA\b",
    "Rapido": r"RAPIDO",
    # wallets & rails
    "PhonePe": r"PHONEPE|PHONE-PE|PHNPE",
    "Google Pay": r"GOOGLE ?PAY|GPAY|G-PAY|GOOGLEPAY",
    "Paytm": r"PAYTM|PAYT MU|PAYTM-?IN|PAYTMQR",
    "One97 (Paytm)": r"ONE97",
    # financial / cards / banks
    "CRED": r"CRED",
    "BajajPay": r"BAJAJ ?PAY",
    "BharatPe": r"BHARATPE|PAY TO BHARATPE",
    # subscriptions / cloud / SaaS
    "Google Cloud": r"GOOGLECLOUD|CLOUD-GOOGLE|GOOGLE ?CLOUD|CLOUD-",
    "OpenAI": r"\bOPENAI\b",
    "Hostinger": r"HOSTINGER|HOSTINGERPTE|HOSTINGERPTE LTD|HOSTINGERPTELTD",
    "QLOUDIN": r"QLOUDIN|QLOUDIN TECHNOLOGIES",
    "Razorpay": r"RAZORPAY|RZP|PAYVIA RAZORPAY|PAYVIARAZORPAY",
    # entertainment / tickets
    "PVR": r"PVR\b|PVR LIMITED",
    "BookMyShow": r"BOOKMYSHOW|BMS",
    # utilities / fuel / retail
    "Bharat Petroleum": r"BHARAT PETROLEUM|BPCL|ARAT PETROLEUM|ARAT PETROLEUM",
    "Shiva Wines": r"SHIVA WINES",
    "Myntra": r"MYNTRA",
    "Shoppers Stop": r"SHOPPERS? ?STOP",
    # misc common merchants
    "Zerodha": r"ZERODHA|ZERODHABROKING",
    "PayU": r"PAYU",
    "Airtel": r"AIRTEL|AIRP",
    "Razorpay-Refund": r"HOSTINGEREFUNDRN|REFUNDRN",
    "Citi/Bank Codes": r"BANKCODE|IFSC|NEFT|IMPS|CITI|HDFC|YESB|AXIS|SBIN|ICIC",
}

# --- Precompile patterns for speed ----------------------------------------
_COMPILED_PAYMENT_PATTERNS = [(label, re.compile(pat, re.IGNORECASE)) for label, pat in PAYMENT_CATEGORY_PATTERNS]
_COMPILED_MERCHANT_PATTERNS = [(name, re.compile(pat, re.IGNORECASE)) for name, pat in MERCHANT_PATTERNS.items()]
# ---------------------------------------------------------------------------


def generate_summary(bank: str, pdf_path: str) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """
    Central entry-point used by the Flask app.
    Returns the cleaned transaction dataframe plus a summary payload for the template layer.
    """
    bank_key = (bank or "").strip().upper()
    extractor = EXTRACTOR_REGISTRY.get(bank_key)
    if extractor is None:
        raise ValueError(f"Unsupported bank: {bank}")

    extracted = extractor(pdf_path)
    if isinstance(extracted, tuple):
        df = extracted[0]
        custom_summary = extracted[1] if len(extracted) > 1 else None
    else:
        df = extracted
        custom_summary = None

    normalized_df = _standardize_dataframe(df)
    summary_payload = _build_dashboard_summary(normalized_df)
    if isinstance(custom_summary, dict):
        override_keys = set(custom_summary.get("__override__", []))
        for key, value in custom_summary.items():
            if key == "__override__" or value is None:
                continue
            if key in override_keys:
                summary_payload[key] = value
            elif key not in summary_payload:
                summary_payload[key] = value
    summary_payload["bank"] = bank_key

    return normalized_df, summary_payload


def _derive_payment_category(details: Any) -> str:
    if pd.isna(details):
        text = ""
    else:
        text = str(details)
    for label, cre in _COMPILED_PAYMENT_PATTERNS:
        if cre.search(text):
            return label
    return "OTHER"


def _detect_merchant(details: str) -> str:
    if pd.isna(details):
        return ""
    text = str(details)
    for merchant, cre in _COMPILED_MERCHANT_PATTERNS:
        if cre.search(text):
            return merchant
    return ""


def _standardize_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame(columns=["Date", "Details", "Debit", "Credit", "Balance", "Transaction Type", "Payment Category", "Merchant"])

    normalized = df.copy()
    normalized.columns = [
        col.strip() if isinstance(col, str) else col
        for col in normalized.columns
    ]

    for original in list(normalized.columns):
        if not isinstance(original, str):
            continue
        key = original.strip().lower()
        if key in RENAME_MAP:
            normalized.rename(columns={original: RENAME_MAP[key]}, inplace=True)

    if "Details" not in normalized.columns:
        normalized["Details"] = ""

    normalized["Details"] = normalized["Details"].astype(str).str.replace(r"\s+", " ", regex=True).str.strip()

    normalized["Date"] = pd.to_datetime(normalized.get("Date"), errors="coerce")
    normalized = normalized.dropna(subset=["Date"]).sort_values("Date")

    for col in ("Debit", "Credit", "Balance", "Amount"):
        if col in normalized.columns:
            normalized[col] = _clean_amount_series(normalized[col])

    if "Debit" not in normalized.columns:
        normalized["Debit"] = 0.0
    if "Credit" not in normalized.columns:
        normalized["Credit"] = 0.0

    if "Amount" in normalized.columns and "Transaction Type" in normalized.columns:
        amounts = normalized["Amount"]
        tx_type = normalized["Transaction Type"].astype(str).str.upper()
        debit_mask = tx_type.str.contains("DEBIT|DR", regex=True, na=False)
        credit_mask = tx_type.str.contains("CREDIT|CR", regex=True, na=False)
        normalized.loc[debit_mask, "Debit"] = amounts[debit_mask]
        normalized.loc[credit_mask, "Credit"] = amounts[credit_mask]

    if "Balance" not in normalized.columns:
        normalized["Balance"] = normalized["Credit"].cumsum() - normalized["Debit"].cumsum()

    if "Transaction Type" not in normalized.columns:
        normalized["Transaction Type"] = normalized.apply(
            lambda row: "CREDIT" if row["Credit"] > 0 else ("DEBIT" if row["Debit"] > 0 else ""),
            axis=1
        )

    normalized["Payment Category"] = normalized["Details"].apply(_derive_payment_category)

    normalized["Merchant"] = normalized["Details"].apply(_detect_merchant)

    # If merchant is empty, use Transaction Type instead
    normalized["Merchant"] = normalized.apply(
        lambda row: row["Merchant"] if row["Merchant"] else row["Transaction Type"],
        axis=1
    )


    return normalized.reset_index(drop=True)


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

    return series.apply(_normalize).astype(float)


def _build_dashboard_summary(df: pd.DataFrame) -> Dict[str, Any]:
    if df.empty:
        empty = pd.DataFrame(columns=["Date", "Credit", "Debit"])
        return {
            "opening_balance": 0.0,
            "closing_balance": 0.0,
            "total_credit": 0.0,
            "total_debit": 0.0,
            "daily_data": empty,
            "top_debits": empty.to_html(index=False),
            "top_credits": empty.to_html(index=False),
            "top_debits_rows": [],
            "top_credits_rows": [],
            "balance_trend": [],
            "net_flow": 0.0,
            "avg_daily_debit": 0.0,
            "avg_daily_credit": 0.0,
            "category_breakdown": [],
            "category_topline": {},
            "category_total_transactions": 0,
        }

    df = df.sort_values("Date").reset_index(drop=True)

    daily_df = (
        df.groupby(df["Date"].dt.normalize())[["Credit", "Debit"]]
        .sum()
        .reset_index()
    )

    daily_df.rename(columns={"Date": "Date"}, inplace=True)
    daily_df["Net"] = daily_df["Credit"] - daily_df["Debit"]

    if "Payment Category" in df.columns:
        category_breakdown_df = (
            df.groupby("Payment Category")
            .agg(
                transactions=("Payment Category", "size"),
                debit=("Debit", "sum"),
                credit=("Credit", "sum"),
            )
            .reset_index()
            .sort_values(["transactions", "debit"], ascending=[False, False])
        )
    else:
        category_breakdown_df = pd.DataFrame(columns=["Payment Category", "transactions", "debit", "credit"])

    total_category_transactions = int(category_breakdown_df["transactions"].sum()) if not category_breakdown_df.empty else 0
    category_topline = {}
    if not category_breakdown_df.empty and total_category_transactions:
        leader = category_breakdown_df.iloc[0]
        category_topline = {
            "label": leader["Payment Category"],
            "transactions": int(leader["transactions"]),
            "debit": float(leader["debit"]),
            "credit": float(leader["credit"]),
            "share": float(leader["transactions"] / total_category_transactions) if total_category_transactions else 0.0,
        }

    debit_columns = ["Date", "Details", "Merchant", "Payment Category", "Debit"]
    credit_columns = ["Date", "Details", "Merchant", "Payment Category", "Credit"]
    if "Bank" in df.columns:
        # keep Bank near the front when available
        debit_columns = ["Date", "Bank", "Details", "Merchant", "Payment Category", "Debit"]
        credit_columns = ["Date", "Bank", "Details", "Merchant", "Payment Category", "Credit"]


    top_debits_df = (
        df[df["Debit"] > 0][debit_columns]
        .sort_values("Debit", ascending=False)
        .head(5)
    )
    top_credits_df = (
        df[df["Credit"] > 0][credit_columns]
        .sort_values("Credit", ascending=False)
        .head(5)
    )

    reported_opening = float(df["Balance"].iloc[0]) if "Balance" in df.columns else 0.0
    opening_balance = reported_opening
    closing_balance = float(df["Balance"].iloc[-1]) if "Balance" in df.columns else 0.0
    total_credit = float(df["Credit"].sum())
    total_debit = float(df["Debit"].sum())

    # Some statements (notably SBI) list the first row *after* the transaction,
    # so the balance already reflects Debit/Credit. Reverse that first row so
    # that opening + credit - debit matches the reported closing.
    opening_entry_credit = 0.0
    opening_entry_debit = 0.0
    if "Balance" in df.columns and not df.empty:
        computed_closing = opening_balance + total_credit - total_debit
        if abs(computed_closing - closing_balance) > 0.5:  # tolerate rounding
            first_credit = float(df["Credit"].iloc[0]) if "Credit" in df.columns else 0.0
            first_debit = float(df["Debit"].iloc[0]) if "Debit" in df.columns else 0.0
            adjusted_opening = opening_balance - first_credit + first_debit
            adjusted_computed = adjusted_opening + total_credit - total_debit
            if abs(adjusted_computed - closing_balance) < abs(computed_closing - closing_balance):
                opening_balance = adjusted_opening
                opening_entry_credit = first_credit
                opening_entry_debit = first_debit

    if opening_entry_credit or opening_entry_debit:
        total_credit = max(0.0, total_credit - opening_entry_credit)
        total_debit = max(0.0, total_debit - opening_entry_debit)

    transaction_count = len(df)
    total_volume = float(df["Debit"].sum() + df["Credit"].sum())
    avg_ticket_size = total_volume / transaction_count if transaction_count else 0.0
    active_days = int(daily_df["Date"].nunique())
    spend_to_income = (total_debit / total_credit) if total_credit else None

    tx_columns = ["Date", "Details", "Merchant", "Payment Category", "Debit", "Credit", "Balance"]
    if "Bank" in df.columns:
        tx_columns.insert(1, "Bank")

    # only keep columns that exist in the df (prevents KeyError if field missing)
    tx_columns = [c for c in tx_columns if c in df.columns]
    transactions_js = df[tx_columns].copy()
    transactions_js["Date"] = transactions_js["Date"].dt.strftime("%Y-%m-%d")


    daily_series = daily_df.copy()
    daily_series["Date"] = daily_series["Date"].dt.strftime("%Y-%m-%d")

    summary = {
        "opening_balance": reported_opening,
        "closing_balance": closing_balance,
        "total_credit": total_credit,
        "total_debit": total_debit,
        "daily_data": daily_df,
        "top_debits": top_debits_df.to_html(index=False, classes="table"),
        "top_credits": top_credits_df.to_html(index=False, classes="table"),
        "top_debits_rows": top_debits_df.to_dict(orient="records"),
        "top_credits_rows": top_credits_df.to_dict(orient="records"),
        "balance_trend": df[["Date", "Balance"]].dropna().to_dict(orient="records"),
        "net_flow": float(total_credit - total_debit),
        "avg_daily_debit": float(daily_df["Debit"].mean()) if not daily_df.empty else 0.0,
        "avg_daily_credit": float(daily_df["Credit"].mean()) if not daily_df.empty else 0.0,
        "peak_debit_day": _peak_day(daily_df, "Debit"),
        "peak_credit_day": _peak_day(daily_df, "Credit"),
        "kpi_avg_ticket": float(avg_ticket_size),
        "kpi_active_days": active_days,
        "kpi_spend_to_income": float(spend_to_income) if spend_to_income is not None else None,
        "transactions_js": transactions_js.to_dict(orient="records"),
        "daily_series": daily_series.to_dict(orient="records"),
        "date_min": transactions_js["Date"].iloc[0] if not transactions_js.empty else "",
        "date_max": transactions_js["Date"].iloc[-1] if not transactions_js.empty else "",
        "category_breakdown": category_breakdown_df.to_dict(orient="records"),
        "category_topline": category_topline,
        "category_total_transactions": total_category_transactions,
    }


    # FIX: Apply credit adjustment ONLY for OVERALL card 
    if "bank" not in summary:
        f_total_credit = (
            summary["closing_balance"]
            - summary["opening_balance"]
            + summary["total_debit"]
        )
        summary["total_credit"] = f_total_credit


    return summary


def _peak_day(daily_df: pd.DataFrame, column: str) -> Dict[str, Any]:
    if daily_df.empty or column not in daily_df:
        return {}
    idx = daily_df[column].idxmax()
    if pd.isna(idx):
        return {}
    record = daily_df.loc[idx]
    return {
        "date": record["Date"].strftime("%Y-%m-%d") if isinstance(record["Date"], pd.Timestamp) else record["Date"],
        "value": float(record[column]),
    }