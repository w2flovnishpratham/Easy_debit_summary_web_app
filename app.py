import os
import uuid
from flask import Flask, request, render_template, send_file
import pandas as pd
from extractor import extract_data_from_pdf
from markupsafe import Markup

app = Flask(__name__)
UPLOAD_FOLDER = 'uploads'
DOWNLOAD_FOLDER = 'downloads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/upload', methods=['POST'])
def upload_file():
    file = request.files['pdf']
    if not file:
        return "No file uploaded", 400

    unique_id = str(uuid.uuid4())
    filename = f"{unique_id}.pdf"
    pdf_path = os.path.join(UPLOAD_FOLDER, filename)
    file.save(pdf_path)

    df = extract_data_from_pdf(pdf_path)
    csv_filename = f"{unique_id}.csv"
    csv_path = os.path.join(DOWNLOAD_FOLDER, csv_filename)
    df.to_csv(csv_path, index=False)

    return send_file(csv_path, as_attachment=True)

@app.route('/summary', methods=['GET', 'POST'])
def summary_view():
    if request.method == 'GET':
        # Allow direct GET access to /summary
        return render_template('summary.html')

    file = request.files['pdf']
    if not file:
        return "No file uploaded", 400

    unique_id = str(uuid.uuid4())
    filename = f"{unique_id}.pdf"
    pdf_path = os.path.join(UPLOAD_FOLDER, filename)
    file.save(pdf_path)

    df = extract_data_from_pdf(pdf_path)
    df["Date"] = pd.to_datetime(df["Date"], dayfirst=True)
    df["Amount"] = df["Amount"].astype(float)
    df["Closing Balance"] = df["Closing Balance"].astype(float)

    opening_balance = df.iloc[0]["Closing Balance"]
    closing_balance = df.iloc[-1]["Closing Balance"]
    total_credit = df[df["Transaction Type"] == "Credit"]["Amount"].sum()
    total_debit = df[df["Transaction Type"] == "Debit"]["Amount"].sum()

    daily_summary = df.groupby(["Date", "Transaction Type"])["Amount"].sum().unstack().fillna(0).reset_index()
    daily_summary["Date"] = daily_summary["Date"].dt.strftime("%Y-%m-%d")

    top_5_debits = df[df["Transaction Type"] == "Debit"].nlargest(5, "Amount")
    top_5_credits = df[df["Transaction Type"] == "Credit"].nlargest(5, "Amount")

    return render_template("summary.html",
        opening_balance=opening_balance,
        closing_balance=closing_balance,
        total_credit=total_credit,
        total_debit=total_debit,
        daily_data=daily_summary.to_dict(orient="list"),
        top_debits=Markup(top_5_debits.to_html(index=False, classes="table")),
        top_credits=Markup(top_5_credits.to_html(index=False, classes="table"))
    )

if __name__ == '__main__':
    app.run(debug=False)
