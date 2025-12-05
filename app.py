from flask import Flask, render_template, request, send_file, send_from_directory, session, jsonify, abort, redirect, url_for
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
from functools import wraps

from pymongo import MongoClient, errors as pymongo_errors
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests
from PyPDF2 import PdfReader, PdfWriter
from PyPDF2.errors import DependencyError

load_dotenv()


def _env(key: str, default: str = "") -> str:
    value = os.environ.get(key, default)
    return value.strip() if isinstance(value, str) else value


PAYMENT_GATE_ENABLED = (_env('ENABLE_PAYMENT_GATE', 'true') or 'true').lower() in {'1', 'true', 'yes', 'on'}
GOOGLE_CLIENT_ID = _env("GOOGLE_CLIENT_ID")
MONGO_URI = _env("MONGO_URI")
MONGO_DB_NAME = _env("MONGO_DB_NAME")
GOOGLE_CLOCK_SKEW = int(_env("GOOGLE_CLOCK_SKEW", "120") or "120")
LOGIN_REQUIRED = (_env("LOGIN_REQUIRED", "false") or "false").lower() in {"1", "true", "yes", "on"}

app = Flask(__name__)
app.secret_key = _env("SECRET_KEY") or _env("FLASK_SECRET_KEY") or "dev-secret-change-me"
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=(_env("SESSION_COOKIE_SECURE", "false") or "false").lower() in {"1", "true", "yes", "on"},
)

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads')
OUTPUT_FOLDER = os.path.join(BASE_DIR, 'outputs')
SAMPLE_PDF_DIR = os.path.join(BASE_DIR, 'Sample pdf')
SAMPLE_STATEMENT_FILES = {
    "hdfc": {"filename": "sample_hdfc.pdf", "bank": "HDFC"},
    "sbi": {"filename": "sample_sbi.pdf", "bank": "SBI"},
}
SAMPLE_SELECTIONS = {
    "hdfc": ["hdfc"],
    "sbi": ["sbi"],
    "both": ["hdfc", "sbi"],
}

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

mongo_client: Optional[MongoClient] = None
users_collection = None
normalized_collection = None

if MONGO_URI:
    try:
        mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        mongo_client.admin.command("ping")
        db = mongo_client[MONGO_DB_NAME] if MONGO_DB_NAME else mongo_client.get_default_database()
        if db is None:
            raise RuntimeError("Mongo URI must include a database name or set MONGO_DB_NAME.")
        users_collection = db["users"]
        users_collection.create_index("email", unique=True)
        normalized_collection = db["normalized_data"]
        normalized_collection.create_index("user_email", unique=True)
    except Exception as exc:
        print(f"MongoDB connection failed: {exc}")
        users_collection = None
        normalized_collection = None


def get_users_collection():
    if users_collection is None:
        raise RuntimeError("User store unavailable. Check MONGO_URI configuration.")
    return users_collection


def get_normalized_collection():
    if normalized_collection is None:
        raise RuntimeError("Normalized data store unavailable. Check MONGO_URI / MONGO_DB_NAME.")
    return normalized_collection


@app.context_processor
def inject_user():
    return {
        "user_email": session.get("user_email"),
        "full_name": session.get("full_name"),
        "user_picture": session.get("picture"),
        "login_required": LOGIN_REQUIRED,
    }


def login_required(view_func):
    @wraps(view_func)
    def wrapped_view(*args, **kwargs):
        if not LOGIN_REQUIRED:
            return view_func(*args, **kwargs)
        if not session.get('user_email'):
            next_target = request.full_path.rstrip('?') if request.method == 'GET' else url_for('dashboard')
            return redirect(url_for('login', next=next_target))
        return view_func(*args, **kwargs)
    return wrapped_view


def _require_login_json():
    """Guard JSON/API calls that should require auth when LOGIN_REQUIRED is enabled."""
    if not LOGIN_REQUIRED:
        return None
    if not session.get("user_email"):
        return jsonify({"ok": False, "error": "Login required"}), 401
    return None


def _verify_google_credential(token: str):
    if not GOOGLE_CLIENT_ID:
        raise RuntimeError("GOOGLE_CLIENT_ID is not configured on the server.")
    return id_token.verify_oauth2_token(
        token,
        google_requests.Request(),
        GOOGLE_CLIENT_ID,
        clock_skew_in_seconds=GOOGLE_CLOCK_SKEW,
    )


def _upsert_user(email: str, full_name: str, picture: str):
    users = get_users_collection()
    now = datetime.utcnow()
    try:
        users.update_one(
            {"email": email},
            {
                "$set": {"full_name": full_name, "picture": picture},
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )
    except pymongo_errors.PyMongoError as exc:
        raise RuntimeError(f"User database error: {exc}") from exc
    return users.find_one({"email": email})


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


def _render_summary_from_entries(entries, batch_id: Optional[str] = None):
    if not entries:
        raise ValueError('No file uploaded')

    normalized_frames = []
    statement_sources = []
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    batch_identifier = batch_id or timestamp

    for entry in entries:
        bank = entry.get("bank", "")
        password = entry.get("password", "")
        source_path = entry.get("source_path")
        display_name = entry.get("display_name") or os.path.basename(source_path or "")
        if not source_path or not os.path.exists(source_path):
            raise ValueError(f"Missing or invalid file for {display_name or 'statement'}.")

        pdf_to_process = source_path
        temp_copy = None
        try:
            pdf_to_process, temp_copy = _prepare_pdf_for_processing(source_path, password, display_name)
            df, summary = generate_summary(bank, pdf_to_process)
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError(str(exc))
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
        statement_sources.append({
            "bank": bank_label,
            "filename": display_name,
            "transactions": int(len(df)),
            "date_min": date_min.strftime("%Y-%m-%d") if pd.notna(date_min) else "",
            "date_max": date_max.strftime("%Y-%m-%d") if pd.notna(date_max) else "",
            "total_credit": float(df["Credit"].sum()),
            "total_debit": float(df["Debit"].sum()),
        })

    if not normalized_frames:
        raise ValueError('Failed to process uploads')

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

    output_csv_name = f"{batch_identifier}_combined_output.csv"
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


@app.route('/login')
def login():
    if session.get('user_email'):
        return redirect(url_for('dashboard'))

    client_error = None if GOOGLE_CLIENT_ID else "Google client ID is not configured. Set GOOGLE_CLIENT_ID in .env."
    return render_template('login.html', google_client_id=GOOGLE_CLIENT_ID or "", google_client_error=client_error)


@app.route('/google_auth', methods=['POST'])
def google_auth():
    data = request.get_json(silent=True) or {}
    credential = data.get('credential')
    if not credential:
        return jsonify({"ok": False, "error": "Missing credential"}), 400

    if not GOOGLE_CLIENT_ID:
        return jsonify({"ok": False, "error": "Server missing Google client ID"}), 500

    try:
        id_info = _verify_google_credential(credential)
    except Exception as exc:
        return jsonify({"ok": False, "error": f"Token verification failed: {exc}"}), 401

    email = id_info.get("email")
    full_name = id_info.get("name") or email or ""
    picture = id_info.get("picture") or ""
    if not email:
        return jsonify({"ok": False, "error": "Email not available in Google profile"}), 400

    if users_collection is None:
        return jsonify({"ok": False, "error": "User store unavailable. Configure MONGO_URI / MONGO_DB_NAME."}), 500

    try:
        user = _upsert_user(email=email, full_name=full_name, picture=picture)
    except Exception as exc:
        return jsonify({"ok": False, "error": f"Unable to save user: {exc}"}), 500

    session.permanent = True
    session["user_email"] = email
    session["full_name"] = user.get("full_name") or full_name or email
    session["picture"] = user.get("picture") or picture

    return jsonify({"ok": True, "redirect": url_for('dashboard')})


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/dashboard')
@login_required
def dashboard():
    history_columns = []
    history_rows_full = []
    history_preview = []
    history_total_rows = 0
    history_saved_at = ""
    history_entries = []
    history_error = None
    user_email = session.get('user_email')

    if user_email:
        try:
            collection = get_normalized_collection()
            record = collection.find_one({"user_email": user_email}) or {}
            history_entries = record.get("entries") or []

            # If legacy fields exist without entries, fabricate one entry
            if not history_entries and record.get("rows"):
                history_entries = [{
                    "columns": record.get("columns") or [],
                    "rows": record.get("rows") or [],
                    "row_count": len(record.get("rows") or []),
                    "saved_at": record.get("saved_at"),
                    "saved_at_ist": record.get("saved_at_ist") or "",
                }]

            # Sort entries by saved_at (latest first)
            history_entries = sorted(
                history_entries,
                key=lambda e: e.get("saved_at") or datetime.min,
                reverse=True
            )

            if history_entries:
                latest = history_entries[0]
                history_columns = latest.get("columns") or []
                history_rows_full = latest.get("rows") or []
                history_total_rows = len(history_rows_full)
                history_preview = history_rows_full[-5:] if history_rows_full else []
                saved_at_dt = latest.get("saved_at")
                if saved_at_dt:
                    try:
                        history_saved_at = saved_at_dt.strftime("%Y-%m-%d %H:%M UTC")
                    except Exception:
                        history_saved_at = str(saved_at_dt)
                if not history_saved_at:
                    history_saved_at = latest.get("saved_at_ist") or ""
        except Exception as exc:
            history_error = str(exc)

    return render_template(
        'dashboard.html',
        full_name=session.get('full_name'),
        email=session.get('user_email'),
        picture=session.get('picture'),
        history_columns=history_columns,
        history_rows=history_rows_full,
        history_preview=history_preview,
        history_total_rows=history_total_rows,
        history_saved_at=history_saved_at,
        history_entries=history_entries,
        history_error=history_error,
    )


@app.route('/summary')
@login_required

def summary():
    return redirect(url_for('index'))


@app.route('/')
def index():
    return render_template('index.html')


def _build_sample_entries(selection_key: str):
    keys = SAMPLE_SELECTIONS.get(selection_key.lower())
    if not keys:
        raise ValueError('Select at least one sample statement to continue.')

    entries = []
    for key in keys:
        config = SAMPLE_STATEMENT_FILES.get(key)
        if not config:
            continue
        source_path = os.path.join(SAMPLE_PDF_DIR, config["filename"])
        if not os.path.exists(source_path):
            raise ValueError(f"Sample file {config['filename']} is unavailable.")
        entries.append({
            "bank": config.get("bank", ""),
            "password": config.get("password", ""),
            "source_path": source_path,
            "display_name": f"{config.get('bank', key).upper()} Sample Statement",
        })

    if not entries:
        raise ValueError('No sample statements to process.')
    return entries


@app.route('/sample', methods=['POST'])
def run_sample_statements():
    selection = (request.form.get('sample_option') or '').lower()

    try:
        entries = _build_sample_entries(selection)
    except ValueError as exc:
        return str(exc), 400

    demo_batch = f"demo_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    try:
        return _render_summary_from_entries(entries, batch_id=demo_batch)
    except ValueError as exc:
        return str(exc), 400

@app.route('/upload', methods=['POST'])
@login_required
def upload_file():
    banks = request.form.getlist('bank[]') or request.form.getlist('bank')
    files = request.files.getlist('pdf_file[]') or request.files.getlist('pdf_file')
    passwords = request.form.getlist('pdf_password[]') or request.form.getlist('pdf_password')

    entries = []
    batch_id = datetime.now().strftime('%Y%m%d_%H%M%S')
    for idx, file in enumerate(files):
        bank = banks[idx] if idx < len(banks) else ""
        password = passwords[idx] if idx < len(passwords) else ""
        if bank and file and file.filename:
            safe_filename = f"{batch_id}_{idx + 1}_{file.filename}".replace(" ", "_")
            filepath = os.path.join(UPLOAD_FOLDER, safe_filename)
            file.save(filepath)
            entries.append({
                "bank": bank,
                "password": password,
                "source_path": filepath,
                "display_name": file.filename,
            })

    if not entries:
        return 'No file uploaded', 400
    try:
        return _render_summary_from_entries(entries, batch_id=batch_id)
    except ValueError as exc:
        return str(exc), 400

@app.route('/download/<path:filename>')
@login_required
def download_file(filename):
    if LOGIN_REQUIRED and not session.get('user_email'):
        return redirect(url_for('login', next=request.path))
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


@app.route('/how-to-use/<path:filename>')
def how_to_use_asset(filename):
    assets_dir = os.path.join(BASE_DIR, 'How to use')
    safe_name = os.path.normpath(filename).replace("\\", "/")
    if safe_name.startswith(("..", "/")):
        abort(404)
    try:
        return send_from_directory(assets_dir, safe_name)
    except FileNotFoundError:
        abort(404)



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
@login_required
def create_order():
    auth_block = _require_login_json()
    if auth_block:
        return auth_block
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
@login_required
def verify_payment():
    auth_block = _require_login_json()
    if auth_block:
        return auth_block
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


@app.route('/save_normalized', methods=['POST'])
@login_required
def save_normalized():
    auth_block = _require_login_json()
    if auth_block:
        return auth_block
    user_email = session.get("user_email")
    if not user_email:
        return jsonify({"ok": False, "error": "User not logged in"}), 401

    filenames = []
    stored = session.get('download_files')
    if isinstance(stored, list):
        filenames.extend(stored)
    legacy = session.get('download_file')
    if isinstance(legacy, str):
        filenames.append(legacy)

    if not filenames:
        return jsonify({"ok": False, "error": "No normalized data found for this session"}), 400

    latest_file = filenames[-1]
    csv_path = os.path.join(OUTPUT_FOLDER, latest_file)
    if not os.path.exists(csv_path):
        return jsonify({"ok": False, "error": "Normalized file missing on server"}), 400

    try:
        df = pd.read_csv(csv_path)
    except Exception as exc:
        return jsonify({"ok": False, "error": f"Unable to load normalized data: {exc}"}), 500

    df = df.fillna("")
    new_columns = df.columns.tolist()
    new_rows = df.to_dict(orient="records")

    try:
        collection = get_normalized_collection()
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500

    try:
        existing = collection.find_one({"user_email": user_email}) or {}
    except Exception as exc:
        return jsonify({"ok": False, "error": f"Unable to read existing normalized data: {exc}"}), 500

    def to_primitive(value):
        try:
            if pd.isna(value):
                return ""
        except Exception:
            pass
        if isinstance(value, (int, float, str, bool)):
            return value
        if hasattr(value, "item"):
            try:
                return value.item()
            except Exception:
                pass
        return str(value)

    def normalize_rows(columns, rows):
        normalized = []
        for row in rows:
            normalized.append({col: to_primitive(row.get(col, "")) for col in columns})
        return normalized

    ist_now = datetime.utcnow() + pd.Timedelta(hours=5, minutes=30)
    saved_at_ist = ist_now.strftime("%Y-%m-%d %H:%M:%S IST")

    entry = {
        "columns": new_columns,
        "rows": normalize_rows(new_columns, new_rows),
        "row_count": len(new_rows),
        "saved_at": datetime.utcnow(),
        "saved_at_ist": saved_at_ist,
    }

    entries = existing.get("entries") or []
    entries.append(entry)

    payload = {
        "user_email": user_email,
        "full_name": session.get("full_name", ""),
        # legacy fields point to latest entry for backward compatibility
        "columns": entry["columns"],
        "rows": entry["rows"],
        "saved_at": entry["saved_at"],
        "saved_at_ist": entry["saved_at_ist"],
        "entries": entries,
    }

    try:
        collection.update_one({"user_email": user_email}, {"$set": payload}, upsert=True)
    except Exception as exc:
        return jsonify({"ok": False, "error": f"Unable to save normalized data: {exc}"}), 500

    return jsonify({"ok": True, "saved": entry["row_count"], "entries": len(entries)})

if __name__ == '__main__':
    app.run(debug=os.environ.get("FLASK_DEBUG", "false").lower() in {"1", "true", "yes", "on"})

