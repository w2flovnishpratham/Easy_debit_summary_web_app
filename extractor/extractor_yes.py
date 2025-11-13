import pdfplumber, pandas as pd, re
from typing import Any

# ------------------ Patterns ------------------
date_full = re.compile(r"\b\d{1,2}-[A-Za-z]{3}-\d{4}\b")
date_two  = re.compile(r"\b\d{1,2}-[A-Za-z]{3}-\d{2}\b")
day       = re.compile(r"^\d{1,2}$")
mon_yr    = re.compile(r"^[A-Za-z]{3}-\d{2,4}$")
amt_pat   = re.compile(r"(?<!\S)(-?\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?)(?!\S)")
ref_pat   = re.compile(r"\b([A-Za-z0-9]{5,})\b")


# ------------- Helper Functions --------------
def _norm_year(tok):
    m = date_two.search(tok)
    if not m: return tok
    d,mn,y = tok.split("-")
    return f"{d}-{mn}-{2000+int(y)}" if int(y)<100 else tok

def _find_date(cells):
    for c in cells:
        if c and (m:=date_full.search(c)): return m.group(0)
    for c in cells:
        if c and (m:=date_two.search(c)): return _norm_year(m.group(0))

    for a,b in zip(cells, cells[1:]):
        if a and b:
            if day.match(a) and mon_yr.match(b): return _norm_year(f"{a}-{b}")
            if mon_yr.match(a) and day.match(b): return _norm_year(f"{b}-{a}")

    joined = " ".join(cells)
    if (m:=date_full.search(joined)): return m.group(0)
    if (m:=date_two.search(joined)):   return _norm_year(m.group(0))
    return None

def _amt(x):
    if not x: return 0.0
    t = re.sub(r"\(([\d,\.]+)\)", r"-\1", str(x).strip().replace("\u00A0",""))
    m = amt_pat.search(t)
    try: return float(m.group(1).replace(",","")) if m else 0.0
    except: return 0.0

def _ref_narr(txt):
    cheque = ""
    for m in ref_pat.findall(txt):
        if len(m)>=5 and m.upper() not in ("FROM","TO","UPI","NA","INT","SWEEP","CREDIT","DEBIT"):
            cheque = m
            txt = txt.replace(m,"",1)
            break
    return cheque, re.sub(r"\s{2,}"," ",txt).strip()


# ------------------ MAIN ------------------
def extract_yes_transactions(pdf_path):
    rows, last = [], None

    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            for table in (page.extract_tables() or []):
                for raw in table:
                    if not raw: continue
                    cells = [str(c).strip() if c else "" for c in raw]
                    if not any(cells): continue

                    date = _find_date(cells)
                    row_txt = " ".join(cells)

                    if date:
                        # narration without amounts
                        narr_src = " ".join(cells[2:-3]).strip() if len(cells)>4 else ""
                        ref, narr = _ref_narr(narr_src)
                        narr = narr or (cells[2] if len(cells)>=3 else "")

                        w,d,b = (_amt(cells[-3]), _amt(cells[-2]), _amt(cells[-1])) if len(cells)>=3 else (0,0,0)

                        rows.append({
                            "Date": date,
                            "ChequeRef": ref,
                            "Narration": narr,
                            "Withdrawal": w,
                            "Deposit": d,
                            "Balance": b
                        })
                        last = rows[-1]
                    else:
                        if last and row_txt.strip():
                            last["Narration"] = (last["Narration"]+" "+row_txt).strip()

    df = pd.DataFrame(rows)
    if df.empty: return df, {}

    df["Narration"] = df["Narration"].str.replace(r"\b\d{1,2}-[A-Za-z]{3}-\d{4}\b","",regex=True)\
                                     .str.replace(r"\s{2,}"," ",regex=True).str.strip()

    # Convert amounts numeric
    for col in ["Withdrawal", "Deposit", "Balance"]:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)

    # Summary
    opening_balance = df.iloc[0]["Balance"] if not df.empty else 0
    closing_balance = df.iloc[-1]["Balance"] if not df.empty else 0
    total_credit = df["Deposit"].sum()
    total_debit = df["Withdrawal"].sum()

    # Daily summary
    daily_df = df.copy()
    daily_df["Date"] = pd.to_datetime(daily_df["Date"]).dt.date
    daily_summary = (
        daily_df.groupby("Date")
        .agg({"Deposit": "sum", "Withdrawal": "sum"})
        .reset_index()
    )
    daily_data = daily_summary.to_dict(orient="records")

    # Top transactions
    top_credits = (
        df[df["Deposit"] > 0]
        .sort_values(by="Deposit", ascending=False)
        .head(5)[["Date", "Narration", "Deposit"]]
        .to_html(index=False, classes="table table-sm table-striped")
    )

    top_debits = (
        df[df["Withdrawal"] > 0]
        .sort_values(by="Withdrawal", ascending=False)
        .head(5)[["Date", "Narration", "Withdrawal"]]
        .to_html(index=False, classes="table table-sm table-striped")
    )

    summary_dict = {
        "opening_balance": opening_balance,
        "closing_balance": closing_balance,
        "total_credit": total_credit,
        "total_debit": total_debit,
        "daily_data": daily_data,
        "top_credits": top_credits,
        "top_debits": top_debits,
    }

    return df, summary_dict
