#!/usr/bin/env python
# coding: utf-8

# In[10]:


import pdfplumber
import pandas as pd
import re

# Input PDF
pdf_path = "statement_full.pdf"
# Strict regex patterns
date_pattern = r"\b\d{2}/\d{2}/\d{2}\b"
# Avoid matching 7.9810027@... etc.
amount_pattern = r"(?<![A-Z0-9@])\b\d{1,3}(?:,\d{3})*\.\d{2}\b(?![A-Z0-9@])"


# In[11]:


def to_float(s):
    try:
        return float(s.replace(",", ""))
    except:
        return None
all_lines = []
with pdfplumber.open(pdf_path) as pdf:
    for page in pdf.pages:
        text = page.extract_text()
        if not text:
            continue
        for line in text.split("\n"):
            line = line.strip()
            # Skip headers / footers
            if any(skip in line for skip in [
                "HDFC BANK", "Page No", "Statement of account", "Account Branch", 
                "Cust ID", "Account No", "Nomination", "MICR", "GSTN"
            ]):
                continue
            all_lines.append(line)

print(f"✅ Extracted {len(all_lines)} lines from {len(pdf.pages)} pages.")


# In[12]:


transactions = []
current = None

# -------------------------
# Y-axis: group by date
# -------------------------
for line in all_lines:
    if re.match(date_pattern, line):
        # 🚫 Skip summary lines that start with a date
        raw_upper = line.upper().replace(" ", "")
        content = re.sub(date_pattern, "", line).strip()
        content_upper = content.upper().replace(" ", "")

        if any(k in raw_upper for k in ("STATEMENTSUMMARY", "SUMMARY", "STATEMENT")) or \
            any(k in content_upper for k in ("STATEMENTSUMMARY", "SUMMARY", "STATEMENT")):
            current = None
            continue

        if current:
            transactions.append(current)

        current = {
            "Date": re.findall(date_pattern, line)[0],
            "Narration": content,
            "Withdrawal": "",
            "Deposit": "",
            "Closing Balance": "",
            "Raw Line": line  # ✅ Add this field to track original line
        }


    elif current:
        clean_line = line.strip()
        upper_clean = clean_line.replace(" ", "").upper()

        # 🚫 Stop appending narration if summary/footer line detected
        if any(k in upper_clean for k in ("STATEMENTSUMMARY", "STATEMENTOFACCOUNT", "STATEMENT", "SUMMARY")):
            print(f"🛑 Stopped merging narration at summary/footer: {clean_line}")
            # Save current before breaking out (don’t lose it)
            transactions.append(current)
            current = None
            continue

        current["Narration"] += " " + clean_line



# ✅ Final check before appending last transaction

if current:
    narration_upper = current["Narration"].upper().replace(" ", "")
    closing_candidates = re.findall(amount_pattern, current["Narration"])
    last_amount = closing_candidates[-1].replace(",", "") if closing_candidates else ""

    # 🚫 Skip true summary lines (but not interest)
    if (
        any(k in narration_upper for k in ("STATEMENTSUMMARY", "SUMMARY"))
        and not any(k in narration_upper for k in ("INTERESTPAID", "INTEREST"))
    ):
        print("🚫 Skipped summary line at end.")
    else:
        transactions.append(current)
        print(f"✅ Added final transaction: {current['Date']} | {current['Narration'][:40]}...")




print(f"✅ Identified {len(transactions)} potential transactions.")


# -------------------------
# X-axis: assign amounts robustly (right-to-left + previous-balance fallback)
# -------------------------
prev_closing = None
tolerance = 0.01

for idx, tx in enumerate(transactions):
    nums = re.findall(amount_pattern, tx["Narration"])

    # ✅ Force include Interest Paid row even if numbers missing
    if "INTEREST PAID" in tx["Narration"].upper():
        narration_upper = tx["Narration"].upper().replace(" ", "")
        # 🚫 Skip if it's part of a summary block
        if any(k in narration_upper for k in ("STATEMENTSUMMARY", "SUMMARY", "STATEMENT")):
            print(f"🚫 Skipped summary interest line: {tx['Narration']}")
            continue

        nums = re.findall(amount_pattern, tx["Narration"])
        if len(nums) >= 2:
            tx["Deposit"] = nums[-2]
            tx["Closing Balance"] = nums[-1]
            tx["Narration"] = re.sub(amount_pattern, "", tx["Narration"]).strip()
            prev_closing = to_float(nums[-1])
            continue


    # normalize list (keep order left->right)
    nums = [n for n in nums]

    # Remove trailing meaningless zeros that cause shifts (e.g., '0.00' as dummy)
    # but only if it appears to be a placeholder (last numeric is 0 and there are >=2 numbers)
    if len(nums) >= 2 and re.match(r"^0(?:\.0+)?$", nums[-1].replace(",", "")):
        # drop trailing zero placeholder
        nums = nums[:-1]

    # If still empty, nothing to assign
    if not nums:
        transactions[idx] = tx
        continue

    # last numeric is closing balance (right-most)
    closing_str = nums[-1]
    closing = to_float(closing_str)

    # helper values
    second_str = nums[-2] if len(nums) >= 2 else None
    second = to_float(second_str) if second_str else None
    third_str = nums[-3] if len(nums) >= 3 else None
    third = to_float(third_str) if third_str else None

    # Default clear
    tx["Withdrawal"], tx["Deposit"], tx["Closing Balance"] = "", "", ""

    if len(nums) == 1:
        # only closing balance available (rare) — treat as balance
        tx["Closing Balance"] = closing_str

    else:
        # len >= 2: use right-to-left logic
        # If second is clearly greater than closing -> deposit (increase)
        # If second < closing -> withdrawal (decrease)
        assigned = False
        if second is not None and closing is not None:
            if second > closing + tolerance:
                tx["Deposit"] = second_str
                tx["Closing Balance"] = closing_str
                assigned = True
            elif second < closing - tolerance:
                tx["Withdrawal"] = second_str
                tx["Closing Balance"] = closing_str
                assigned = True

        # fallback: use previous closing balance to determine direction
        if not assigned and prev_closing is not None and closing is not None and second is not None:
            # if closing > prev_closing => net credit; attempt to treat second as deposit
            if closing > prev_closing + tolerance:
                # credit likely happened
                tx["Deposit"] = second_str
                tx["Closing Balance"] = closing_str
            else:
                # otherwise treat as withdrawal
                tx["Withdrawal"] = second_str
                tx["Closing Balance"] = closing_str
            assigned = True

        # if still ambiguous (no prev_closing or equal values), use narration hints
        if not assigned:
            narration_text = tx["Narration"].upper()
            if any(k in narration_text for k in ("REVERS", "REFUND", "CR", "CREDIT", "REVERSED")):
                tx["Deposit"] = second_str
                tx["Closing Balance"] = closing_str
            else:
                # default to withdrawal (bank statements more often show withdrawal on left)
                tx["Withdrawal"] = second_str
                tx["Closing Balance"] = closing_str

        # if there is a third number before these two, assign it as additional withdrawal (common pattern)
        if third is not None:
            # if deposit already set and third exists, third could be previous withdrawal
            if tx["Deposit"] and not tx["Withdrawal"]:
                tx["Withdrawal"] = third_str
            # if withdrawal already set and deposit empty, keep deposit empty
            # if both empty, assign third to withdrawal
            elif not tx["Deposit"] and not tx["Withdrawal"]:
                tx["Withdrawal"] = third_str

    # remove numeric tokens from narration string
    tx["Narration"] = re.sub(amount_pattern, "", tx["Narration"]).strip()

    # update prev_closing if we have a numeric closing
    if tx["Closing Balance"]:
        prev_closing_val = to_float(tx["Closing Balance"])
        if prev_closing_val is not None:
            prev_closing = prev_closing_val

# -------------------------
# Build DataFrame and clean text
# -------------------------
df_final = pd.DataFrame(transactions)
df_final["Narration"] = df_final["Narration"].str.replace(r"\s+", " ", regex=True)
df_final = df_final[df_final["Date"].notna()].reset_index(drop=True)

# 🚫 Drop rows with summary keywords
df_final = df_final[
    ~df_final["Narration"].str.contains(r"STATEMENT|SUMMARY", case=False, na=False)
].reset_index(drop=True)


print(f"✅ Final transaction table has {len(df_final)} rows.")


# In[13]:


# Define numeric columns
num_cols = ["Withdrawal", "Deposit", "Closing Balance"]

# Clean and convert each column
for col in num_cols:
    df_final[col] = (
        df_final[col]
        .astype(str)                       # Ensure string type
        .str.replace(",", "", regex=False) # Remove commas
        .str.strip()                       # Trim spaces
        .replace("", None)                 # Replace empty with None
        .astype(float)                     # Convert to float
    )

print("✅ Numeric columns converted successfully.")
display(df_final.dtypes)


# In[14]:


# Ensure numeric types (in case they're strings)
for col in ["Withdrawal", "Deposit", "Closing Balance"]:
    df_final[col] = pd.to_numeric(df_final[col], errors="coerce").fillna(0.0)

# Add previous balance and balance change
df_final["Prev Balance"] = df_final["Closing Balance"].shift(1)
df_final["Balance Change"] = df_final["Closing Balance"] - df_final["Prev Balance"]

# Correct deposit/withdrawal placement using balance movement
for i, row in df_final.iterrows():
    if pd.notna(row["Prev Balance"]):
        if row["Balance Change"] > 0:
            # Balance increased → it was a Deposit
            if row["Withdrawal"] != 0 and row["Deposit"] == 0:
                df_final.at[i, "Deposit"] = row["Withdrawal"]
                df_final.at[i, "Withdrawal"] = 0
        elif row["Balance Change"] < 0:
            # Balance decreased → it was a Withdrawal
            if row["Deposit"] != 0 and row["Withdrawal"] == 0:
                df_final.at[i, "Withdrawal"] = row["Deposit"]
                df_final.at[i, "Deposit"] = 0

# Add a transaction type column for clarity
df_final["Transaction Type"] = df_final.apply(
    lambda r: "CREDIT" if r["Deposit"] > 0 else ("DEBIT" if r["Withdrawal"] > 0 else ""), axis=1
)

# Summary totals
total_deposit = df_final["Deposit"].sum()
total_withdrawal = df_final["Withdrawal"].sum()

print("✅ Corrected deposit/withdrawal using balance movement logic")
print(f"💰 Total Deposits  : {total_deposit:,.2f}")
print(f"💸 Total Withdrawals: {total_withdrawal:,.2f}")
print(f"📊 Net Change       : {(total_deposit - total_withdrawal):,.2f}")





# In[15]:


print("🔍 Checking balance consistency until SUMMARY...")

df_final["Expected Balance"] = None
df_final["Balance Error"] = None

for idx, row in df_final.iterrows():
    row_text = " ".join(str(x) for x in row.values)

    # ✅ Stop when summary section starts
    if "STATEMENT SUMMARY" in row_text.upper():
        print("🛑 Stopped at Statement Summary section")
        break

    # ✅ Skip interest-only entries
    if "INTEREST" in row_text.upper():
        continue

    prev_bal = df_final.loc[idx-1, "Closing Balance"] if idx > 0 else None
    if prev_bal is not None:
        expected = prev_bal - row["Withdrawal"] + row["Deposit"]
        df_final.at[idx, "Expected Balance"] = expected
        df_final.at[idx, "Balance Error"] = round(row["Closing Balance"] - expected, 2)

df_final["Balance Error"] = pd.to_numeric(df_final["Balance Error"], errors="coerce").fillna(0)
errors = df_final[df_final["Balance Error"].abs() > 1]

if errors.empty:
    print("✅ All balance movements are consistent!")
else:
    print(f"⚠️ Mismatches detected: {len(errors)}")
    print(errors[["Date", "Narration", "Balance Error"]])







# In[16]:


df_cleaned = [df_final.iloc[0]]  # keep the first row

for i in range(1, len(df_final)):
    prev = df_cleaned[-1]["Closing Balance"]
    expected = prev - df_final.iloc[i]["Withdrawal"] + df_final.iloc[i]["Deposit"]
    actual = df_final.iloc[i]["Closing Balance"]

    # ✅ Always keep the last row (closing balance from bank)
    if i == len(df_final) - 1 or abs(expected - actual) < 1:
        df_cleaned.append(df_final.iloc[i])


# In[17]:


# -------------------------------
# Compute expected closing balance
# -------------------------------
# ---------------------------------
# Compute opening & closing balance
# ---------------------------------
first_row = df_final.iloc[0]
opening_balance = first_row["Closing Balance"] + first_row["Withdrawal"] - first_row["Deposit"]

closing_balance = df_final["Closing Balance"].iloc[-1]

# ---------------------------------
# Compute total credits & debits
# ---------------------------------
total_credit = df_final["Deposit"].sum()
total_debit = df_final["Withdrawal"].sum()

calculated_closing = opening_balance + total_credit - total_debit

# -------------------------------
# Display results neatly
# -------------------------------
print("📘 BALANCE VALIDATION EQUATION\n")
print(f"Opening Balance : {opening_balance:,.2f}")
print(f"+ Total Credit  : {total_credit:,.2f}")
print(f"- Total Debit   : {total_debit:,.2f}")
print("------------------------------------------------")
print(f"= Calculated Closing Balance : {calculated_closing:,.2f}")
print(f"Actual Closing Balance       : {closing_balance:,.2f}")
print("------------------------------------------------")

# Check if it matches
diff = closing_balance - calculated_closing
if abs(diff) < 1:
    print("✅ Equation verified! Balances match perfectly.")
else:
    print(f"⚠️  Difference detected: {diff:,.2f}")


# In[18]:


df_final.to_excel("statement_final.xlsx", index=False)


# In[ ]:





# In[ ]:





# In[ ]:





# In[ ]:




