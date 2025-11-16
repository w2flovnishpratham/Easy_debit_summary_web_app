from flask import Flask, render_template, request, send_file, session, jsonify, abort
import os
import pandas as pd
from datetime import datetime
from extractor.summary import generate_summary, _build_dashboard_summary
from dotenv import load_dotenv
import base64
import json
import hmac
import hashlib
import urllib.request
import urllib.error
import tempfile
from typing import Optional, Tuple

from PyPDF2 import PdfReader, PdfWriter
from PyPDF2.errors import DependencyError

PAYMENT_GATE_ENABLED = os.environ.get('ENABLE_PAYMENT_GATE', 'true').lower() in {'1', 'true', 'yes', 'on'}

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-change-me")
UPLOAD_FOLDER = 'uploads'
OUTPUT_FOLDER = 'outputs'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)


def _prepare_pdf_for_processing(source_path: str, password: Optional[str], display_name: str) -> Tuple[str, Optional[str]]:
    """
    Returns the path that should be fed into the extractor and, if we had to
    create a decrypted copy, the temporary file that needs cleanup.
    """
    cleaned_password = (password or "").strip()
    writer: Optional[PdfWriter] = None

    try:
        with open(source_path, "rb") as input_file:
            try:
                reader = PdfReader(input_file)
            except DependencyError as exc:
                raise ValueError(
                    f"{display_name or 'PDF'} uses AES encryption. PyCryptodome must be installed on the server to decrypt it. ({exc})"
                )

            if not reader.is_encrypted:
                return source_path, None

            decrypt_ok = 0
            if cleaned_password:
                try:
                    decrypt_ok = reader.decrypt(cleaned_password)
                except Exception as exc:
                    raise ValueError(f"Failed to decrypt {display_name or 'PDF'}: {exc}")
                if decrypt_ok == 0:
                    raise ValueError(f"Incorrect password for {display_name or 'PDF'}.")
            else:
                # Some banks ship PDFs marked as encrypted but readable with a blank password.
                try:
                    decrypt_ok = reader.decrypt("")
                    if decrypt_ok == 0:
                        decrypt_ok = reader.decrypt(b"")
                except Exception as exc:
                    raise ValueError(f"Failed to inspect {display_name or 'PDF'} encryption: {exc}")
                if decrypt_ok == 0:
                    raise ValueError(f"{display_name or 'PDF'} is password protected. Please provide the password.")

            writer = PdfWriter()
            for page in reader.pages:
                writer.add_page(page)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"Unable to read {display_name or 'PDF'}: {exc}")

    if writer is None:
        return source_path, None

    fd, temp_path = tempfile.mkstemp(suffix=".pdf")
    with os.fdopen(fd, "wb") as tmp_file:
        writer.write(tmp_file)
    return temp_path, temp_path


@app.route('/')
def index():
    return render_template('index.html')

@app.route('/upload', methods=['POST'])
def upload_file():
    banks = request.form.getlist('bank[]') or request.form.getlist('bank')
    files = request.files.getlist('pdf_file[]') or request.files.getlist('pdf_file')
    passwords = request.form.getlist('pdf_password[]') or request.form.getlist('pdf_password')

    entries = []
    for idx, file in enumerate(files):
        bank = banks[idx] if idx < len(banks) else ""
        password = passwords[idx] if idx < len(passwords) else ""
        if bank and file and file.filename:
            entries.append((bank, file, password))

    if not entries:
        return 'No file uploaded', 400

    batch_id = datetime.now().strftime('%Y%m%d_%H%M%S')
    normalized_frames = []
    statement_sources = []

    for idx, (bank, file, password) in enumerate(entries, start=1):
        safe_filename = f"{batch_id}_{idx}_{file.filename}".replace(" ", "_")
        filepath = os.path.join(UPLOAD_FOLDER, safe_filename)
        file.save(filepath)

        pdf_to_process = filepath
        temp_copy = None
        try:
            pdf_to_process, temp_copy = _prepare_pdf_for_processing(filepath, password, file.filename)
            df, summary = generate_summary(bank, pdf_to_process)
        except ValueError as exc:
            return str(exc), 400
        finally:
            if temp_copy and os.path.exists(temp_copy):
                try:
                    os.remove(temp_copy)
                except OSError:
                    pass

        bank_label = summary.get("bank", (bank or "").upper())
        df["Bank"] = bank_label
        normalized_frames.append(df)

        date_min = df["Date"].min() if not df["Date"].empty else None
        date_max = df["Date"].max() if not df["Date"].empty else None
        source_entry = {
            "bank": bank_label,
            "filename": file.filename,
            "transactions": int(len(df)),
            "date_min": date_min.strftime("%Y-%m-%d") if pd.notna(date_min) else "",
            "date_max": date_max.strftime("%Y-%m-%d") if pd.notna(date_max) else "",
            "total_credit": float(df["Credit"].sum()),
            "total_debit": float(df["Debit"].sum()),
        }
        statement_sources.append(source_entry)

    if not normalized_frames:
        return 'Failed to process uploads', 400

    combined_df = pd.concat(normalized_frames, ignore_index=True)
    combined_summary = _build_dashboard_summary(combined_df)
    combined_summary["bank"] = "MULTI"

    bank_daily_series = []
    bank_totals = []
    accuracy_breakdown = []

    for bank_name, bank_df in combined_df.groupby("Bank"):
        bank_summary = _build_dashboard_summary(bank_df)
        bank_summary["bank"] = bank_name
        accuracy_breakdown.append({
            "bank": bank_name,
            "opening_balance": bank_summary["opening_balance"],
            "closing_balance": bank_summary["closing_balance"],
            "total_credit": bank_summary["total_credit"],
            "total_debit": bank_summary["total_debit"],
        })

        daily = bank_summary["daily_data"]
        daily_dates = pd.to_datetime(daily["Date"], errors='coerce').dt.strftime("%Y-%m-%d")
        bank_daily_series.append({
            "bank": bank_name,
            "series": [
                {"Date": date, "Credit": credit, "Debit": debit}
                for date, credit, debit in zip(daily_dates, daily["Credit"], daily["Debit"])
            ],
        })

        bank_totals.append({
            "bank": bank_name,
            "credit": bank_summary["total_credit"],
            "debit": bank_summary["total_debit"],
        })

    daily_df = combined_summary['daily_data']
    daily_dates = pd.to_datetime(daily_df["Date"], errors='coerce').dt.strftime("%Y-%m-%d")
    daily_data = {
        "Date": daily_dates.tolist(),
        "Credit": daily_df["Credit"].tolist(),
        "Debit": daily_df["Debit"].tolist()
    }
    daily_series = combined_summary.get("daily_series", [])

    table_df = combined_df.copy()
    table_df["Date"] = table_df["Date"].dt.strftime("%Y-%m-%d")
    table_columns = table_df.columns.tolist()
    table_rows = table_df.to_dict(orient="records")

    # Save combined CSV
    output_csv_name = f"{batch_id}_combined_output.csv"
    output_csv_path = os.path.join(OUTPUT_FOLDER, output_csv_name)
    combined_df.to_csv(output_csv_path, index=False)

    session['download_file'] = output_csv_name
    session['download_files'] = [output_csv_name]
    session['paid'] = not PAYMENT_GATE_ENABLED

    return render_template(
        'summary.html',
        opening_balance=combined_summary['opening_balance'],
        closing_balance=combined_summary['closing_balance'],
        total_credit=combined_summary['total_credit'],
        total_debit=combined_summary['total_debit'],
        daily_data=daily_data,
        daily_series=daily_series,
        top_debits=combined_summary['top_debits'],
        top_credits=combined_summary['top_credits'],
        date_min=combined_summary.get('date_min', ''),
        date_max=combined_summary.get('date_max', ''),
        transactions_js=combined_summary.get('transactions_js', []),
        kpi_avg_ticket=combined_summary.get('kpi_avg_ticket', 0.0),
        kpi_active_days=combined_summary.get('kpi_active_days', 0),
        kpi_spend_to_income=combined_summary.get('kpi_spend_to_income'),
        statement_sources=statement_sources,
        bank_accuracy=accuracy_breakdown,
        bank_daily_series=bank_daily_series,
        bank_totals=bank_totals,
        transactions_table=table_rows,
        transactions_columns=table_columns,
        category_breakdown=combined_summary.get('category_breakdown', []),
        category_topline=combined_summary.get('category_topline', {}),
        category_total_transactions=combined_summary.get('category_total_transactions', 0),
        download_link=f"/download/{output_csv_name}",
        payment_required=PAYMENT_GATE_ENABLED,
        razorpay_key_id=os.environ.get('RAZORPAY_KEY_ID', ''),
        razorpay_amount=int(os.environ.get('RAZORPAY_AMOUNT', '0')),
        razorpay_currency=os.environ.get('RAZORPAY_CURRENCY', 'INR'),
        insight_payload={
            "net_flow": combined_summary.get("net_flow"),
            "avg_daily_debit": combined_summary.get("avg_daily_debit"),
            "avg_daily_credit": combined_summary.get("avg_daily_credit"),
            "peak_debit_day": combined_summary.get("peak_debit_day"),
            "peak_credit_day": combined_summary.get("peak_credit_day"),
            "top_category": combined_summary.get("category_topline", {}),
        }
    )

@app.route('/download/<path:filename>')
def download_file(filename):
    # Enforce payment check and ensure filename matches the one created this session
    allowed = []
    stored = session.get('download_files')
    if isinstance(stored, list):
        allowed.extend(stored)
    legacy = session.get('download_file')
    if isinstance(legacy, str):
        allowed.append(legacy)

    if not allowed or filename not in allowed:
        abort(403)
    if PAYMENT_GATE_ENABLED and not session.get('paid'):
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
    if not PAYMENT_GATE_ENABLED:
        return jsonify({"error": "Payment gateway disabled"}), 400
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
    if not PAYMENT_GATE_ENABLED:
        return jsonify({"ok": False, "error": "Payment gateway disabled"}), 400
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
