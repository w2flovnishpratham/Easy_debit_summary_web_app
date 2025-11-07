from flask import Flask, render_template, request, send_file, session, jsonify, abort
import os
import pandas as pd
from datetime import datetime
from extractor.extractor_hdfc import extract_hdfc_transactions
from extractor.extractor_icici import extract_icici_transactions
from extractor.extractor_sbi import extract_sbi_transactions
from dotenv import load_dotenv
import base64
import json
import hmac
import hashlib
import urllib.request
import urllib.error

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-change-me")
UPLOAD_FOLDER = 'uploads'
OUTPUT_FOLDER = 'outputs'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/upload', methods=['POST'])
def upload_file():
    bank = request.form['bank']
    file = request.files['pdf_file']
    if not file:
        return 'No file uploaded', 400

    # Save uploaded file
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    safe_filename = f"{timestamp}_{file.filename}".replace(" ", "_")
    filepath = os.path.join(UPLOAD_FOLDER, safe_filename)
    file.save(filepath)

    # Extract data
    if bank == 'HDFC':
        df, summary = extract_hdfc_transactions(filepath)
    elif bank == 'ICICI':
        df, summary = extract_icici_transactions(filepath)
    elif bank == 'SBI':
        df, summary = extract_sbi_transactions(filepath)
    else:
        return 'Unsupported bank selected', 400

    # Convert daily_data to JSON-safe format
    daily_df = summary['daily_data']
    daily_data = {
        "Date": pd.to_datetime(daily_df["Date"], errors='coerce').dt.strftime("%Y-%m-%d").tolist(),
        "Credit": daily_df["Credit"].tolist(),
        "Debit": daily_df["Debit"].tolist()
    }

    # Save CSV
    output_csv_name = f"{timestamp}_output.csv"
    output_csv_path = os.path.join(OUTPUT_FOLDER, output_csv_name)
    df.to_csv(output_csv_path, index=False)

    # Track download file in session and mark unpaid until checkout completes
    session['download_file'] = output_csv_name
    session['paid'] = False

    # Render summary page
    return render_template(
        'summary.html',
        opening_balance=summary['opening_balance'],
        closing_balance=summary['closing_balance'],
        total_credit=summary['total_credit'],
        total_debit=summary['total_debit'],
        daily_data=daily_data,
        top_debits=summary['top_debits'],
        top_credits=summary['top_credits'],
        download_link=f"/download/{output_csv_name}",
        razorpay_key_id=os.environ.get('RAZORPAY_KEY_ID', ''),
        razorpay_amount=int(os.environ.get('RAZORPAY_AMOUNT', '0')),
        razorpay_currency=os.environ.get('RAZORPAY_CURRENCY', 'INR')
    )

@app.route('/download/<path:filename>')
def download_file(filename):
    # Enforce payment check and ensure filename matches the one created this session
    expected = session.get('download_file')
    if not expected or expected != filename:
        abort(403)
    if not session.get('paid'):
        abort(402)  # Payment Required
    return send_file(os.path.join(OUTPUT_FOLDER, filename), as_attachment=True)


def _create_razorpay_order(amount: int, currency: str, receipt: str):
    key_id = os.environ.get('RAZORPAY_KEY_ID')
    key_secret = os.environ.get('RAZORPAY_KEY_SECRET')
    if not key_id or not key_secret:
        raise RuntimeError('Razorpay credentials not configured')

    payload = {
        "amount": int(amount),  # in subunits (paise)
        "currency": currency,
        "receipt": receipt,
        "payment_capture": 1
    }

    auth_str = f"{key_id}:{key_secret}".encode('utf-8')
    auth_b64 = base64.b64encode(auth_str).decode('utf-8')

    req = urllib.request.Request(
        url='https://api.razorpay.com/v1/orders',
        data=json.dumps(payload).encode('utf-8'),
        headers={
            'Authorization': f'Basic {auth_b64}',
            'Content-Type': 'application/json'
        },
        method='POST'
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode('utf-8')
            return json.loads(body)
    except urllib.error.HTTPError as e:
        err_body = e.read().decode('utf-8', errors='ignore')
        raise RuntimeError(f"Razorpay order creation failed: {e.code} {err_body}")


@app.route('/create_order', methods=['POST'])
def create_order():
    # Amount and currency from env
    amount = int(os.environ.get('RAZORPAY_AMOUNT', '0'))
    currency = os.environ.get('RAZORPAY_CURRENCY', 'INR')
    if amount <= 0:
        return jsonify({"error": "Invalid or missing RAZORPAY_AMOUNT in .env"}), 400

    # Tie receipt to the session file for traceability
    receipt = session.get('download_file') or datetime.now().strftime('%Y%m%d_%H%M%S')

    try:
        order = _create_razorpay_order(amount=amount, currency=currency, receipt=receipt)
    except Exception as ex:
        return jsonify({"error": str(ex)}), 500

    # Keep latest order_id in session for verification
    session['order_id'] = order.get('id')
    return jsonify({
        "order_id": order.get('id'),
        "amount": order.get('amount'),
        "currency": order.get('currency'),
        "key_id": os.environ.get('RAZORPAY_KEY_ID', '')
    })


@app.route('/verify_payment', methods=['POST'])
def verify_payment():
    data = request.get_json(silent=True) or {}
    razorpay_order_id = data.get('razorpay_order_id')
    razorpay_payment_id = data.get('razorpay_payment_id')
    razorpay_signature = data.get('razorpay_signature')

    if not (razorpay_order_id and razorpay_payment_id and razorpay_signature):
        return jsonify({"ok": False, "error": "Missing fields"}), 400

    expected_order_id = session.get('order_id')
    if expected_order_id and expected_order_id != razorpay_order_id:
        return jsonify({"ok": False, "error": "Order mismatch"}), 400

    key_secret = os.environ.get('RAZORPAY_KEY_SECRET', '')
    if not key_secret:
        return jsonify({"ok": False, "error": "Server missing key secret"}), 500

    message = f"{razorpay_order_id}|{razorpay_payment_id}".encode('utf-8')
    expected_sig = hmac.new(key_secret.encode('utf-8'), msg=message, digestmod=hashlib.sha256).hexdigest()

    if hmac.compare_digest(expected_sig, razorpay_signature):
        session['paid'] = True
        return jsonify({"ok": True})
    else:
        return jsonify({"ok": False, "error": "Signature verification failed"}), 400

if __name__ == '__main__':
    app.run(debug=True)
