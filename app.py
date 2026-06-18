from flask import Flask, render_template, request, send_file, send_from_directory, session, jsonify, abort, redirect, url_for
import os
import pandas as pd
from datetime import datetime
from extractor.summary import generate_summary, _build_dashboard_summary, _standardize_dataframe
from dotenv import load_dotenv
import base64
import json
import hmac
import hashlib
import secrets
import urllib.request
import urllib.error
import urllib.parse
import tempfile
from typing import Optional, Tuple
from functools import wraps
import requests

from pymongo import MongoClient, errors as pymongo_errors
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests
from PyPDF2 import PdfReader, PdfWriter
from PyPDF2.errors import DependencyError

load_dotenv()



def _env(key: str, default: str = "") -> str:
    value = os.environ.get(key, default)
    return value.strip() if isinstance(value, str) else value


def _env_required(key: str, default: str | None = None) -> str:
    val = os.getenv(key, default)
    if val is None or str(val).strip() == "":
        raise RuntimeError(f"Missing required env var: {key}")
    return str(val).strip()


PAYMENT_GATE_ENABLED = (_env('ENABLE_PAYMENT_GATE', 'true') or 'true').lower() in {'1', 'true', 'yes', 'on'}
GOOGLE_CLIENT_ID = _env("GOOGLE_CLIENT_ID")
GOOGLE_REDIRECT_URI = _env("GOOGLE_REDIRECT_URI") or "/auth/gsi-login"
GOOGLE_CLIENT_SECRET = _env("GOOGLE_CLIENT_SECRET")
MONGO_URI = _env("MONGO_URI")
MONGO_DB_NAME = _env("MONGO_DB_NAME")
GOOGLE_CLOCK_SKEW = int(_env("GOOGLE_CLOCK_SKEW", "120") or "120")
LOGIN_REQUIRED = (_env("LOGIN_REQUIRED", "false") or "false").lower() in {"1", "true", "yes", "on"}
LLM_MODEL = _env_required("LLM_MODEL", "llama3.1:8b")
LLM_TIMEOUT = int(_env_required("LLM_TIMEOUT", "90"))
OLLAMA_API_KEY = _env_required("OLLAMA_API_KEY")
OLLAMA_HOST = _env_required("OLLAMA_HOST", "https://ollama.com").rstrip("/")
combined_df: Optional[pd.DataFrame] = None

_original_requests_post = requests.post


def _guarded_post(url, *args, **kwargs):
    url_str = str(url)
    lowered = url_str.lower()
    if "localhost" in lowered or "127.0.0.1" in lowered or "11434" in lowered:
        raise RuntimeError(f"BLOCKED LOCAL OLLAMA CALL: {url_str}")
    return _original_requests_post(url, *args, **kwargs)


requests.post = _guarded_post


app = Flask(__name__)
app.secret_key = _env("SECRET_KEY") or _env("FLASK_SECRET_KEY") or "dev-secret-change-me"
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    # WebView + local dev need Lax/False; do not force SameSite=None+Secure globally.
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=False,
)

@app.after_request
def fix_webview_headers(response):
    # Allow WebView to keep cookies
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Credentials"] = "true"

    # Make Google Identity Services / popups behave inside WebView
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin-allow-popups"
    response.headers["Cross-Origin-Embedder-Policy"] = "unsafe-none"
    return response


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
        # In production, avoid verbose logging; keep the app running without exposing details.
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
    user_email = session.get("user_email")
    picture = session.get("picture") or _fallback_picture(user_email or "") or _placeholder_avatar(session.get("full_name") or user_email or "U")
    return {
        "user_email": user_email,
        "full_name": session.get("full_name"),
        "user_picture": picture,
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


# -----updated or added-----
def _fallback_picture(email: str) -> str:
    if not email:
        return ""
    try:
        digest = hashlib.md5(email.strip().lower().encode("utf-8")).hexdigest()
        return f"https://www.gravatar.com/avatar/{digest}?d=identicon"
    except Exception:
        return ""
# -----updated or added-----


# -----updated or added-----
def _placeholder_avatar(initial: str = "U") -> str:
    letter = (initial or "U").strip()[:1].upper() or "U"
    svg = f"""
    <svg xmlns='http://www.w3.org/2000/svg' width='96' height='96' viewBox='0 0 96 96'>
      <defs>
        <linearGradient id='g' x1='0%' y1='0%' x2='100%' y2='100%'>
          <stop offset='0%' stop-color='#00d4ff'/>
          <stop offset='100%' stop-color='#0078ff'/>
        </linearGradient>
      </defs>
      <rect width='96' height='96' rx='18' fill='url(#g)'/>
      <text x='50%' y='55%' dominant-baseline='middle' text-anchor='middle'
            font-family='Arial, sans-serif' font-size='42' fill='#ffffff' font-weight='700'>{letter}</text>
    </svg>
    """
    encoded = base64.b64encode(svg.encode("utf-8")).decode("utf-8")
    return f"data:image/svg+xml;base64,{encoded}"
# -----updated or added-----


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


def _load_history_entries(record: dict):
    entries = record.get("entries") or []
    if not entries and record.get("rows"):
        entries = [{
            "columns": record.get("columns") or [],
            "rows": record.get("rows") or [],
            "row_count": len(record.get("rows") or []),
            "saved_at": record.get("saved_at"),
            "saved_at_ist": record.get("saved_at_ist") or "",
        }]
    return sorted(
        entries,
        key=lambda e: e.get("saved_at") or datetime.min,
        reverse=True
    )


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

    global combined_df
    combined_df = pd.concat(normalized_frames, ignore_index=True)

    summary_context = _build_summary_page_context(
        combined_df,
        statement_sources=statement_sources,
    )
    output_csv_name = f"{batch_identifier}_combined_output.csv"
    output_csv_path = os.path.join(OUTPUT_FOLDER, output_csv_name)

    combined_df.to_csv(output_csv_path, index=False)

    session['download_file'] = output_csv_name
    session['download_files'] = [output_csv_name]
    session['paid'] = not PAYMENT_GATE_ENABLED

    summary_context.update({
        "download_link": f"/download/{output_csv_name}",
        "payment_required": PAYMENT_GATE_ENABLED,
        "razorpay_key_id": os.environ.get('RAZORPAY_KEY_ID', ''),
        "razorpay_amount": int(os.environ.get('RAZORPAY_AMOUNT', '0')),
        "razorpay_currency": os.environ.get('RAZORPAY_CURRENCY', 'INR'),
    })
    return render_template('summary.html', **summary_context)


def _build_summary_page_context(df: pd.DataFrame, statement_sources=None):
    statement_sources = statement_sources or []
    combined_summary = _build_dashboard_summary(df)

    daily_df = combined_summary["daily_data"]
    daily_dates = pd.to_datetime(daily_df["Date"], errors="coerce").dt.strftime("%Y-%m-%d")
    daily_data = {
        "Date": daily_dates.tolist(),
        "Credit": daily_df["Credit"].tolist(),
        "Debit": daily_df["Debit"].tolist(),
    }
    daily_series = combined_summary.get("daily_series", [])

    table_df = df.copy()
    if "Date" in table_df.columns:
        table_df["Date"] = table_df["Date"].dt.strftime("%Y-%m-%d")
    table_columns = table_df.columns.tolist()
    table_rows = table_df.to_dict(orient="records")

    bank_frame = df.copy()
    if "Bank" not in bank_frame.columns:
        bank_frame["Bank"] = "Saved extract"
    else:
        bank_frame["Bank"] = bank_frame["Bank"].fillna("Saved extract")

    bank_groups = list(bank_frame.groupby("Bank"))
    if not bank_groups:
        bank_groups = [("Saved extract", bank_frame)]

    bank_daily_series = []
    bank_totals = []
    accuracy_breakdown = []

    for bank_name, bank_df in bank_groups:
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
        bank_dates = pd.to_datetime(daily["Date"], errors="coerce").dt.strftime("%Y-%m-%d")
        bank_daily_series.append({
            "bank": bank_name,
            "series": [
                {"Date": date, "Credit": credit, "Debit": debit}
                for date, credit, debit in zip(bank_dates, daily["Credit"], daily["Debit"])
            ],
        })

        bank_totals.append({
            "bank": bank_name,
            "credit": bank_summary["total_credit"],
            "debit": bank_summary["total_debit"],
        })

    insight_payload = {
        "net_flow": combined_summary.get("net_flow"),
        "avg_daily_debit": combined_summary.get("avg_daily_debit"),
        "avg_daily_credit": combined_summary.get("avg_daily_credit"),
        "peak_debit_day": combined_summary.get("peak_debit_day"),
        "peak_credit_day": combined_summary.get("peak_credit_day"),
        "top_category": combined_summary.get("category_topline", {}),
    }


    return {
        "opening_balance": combined_summary["opening_balance"],
        "closing_balance": combined_summary["closing_balance"],
        "total_credit": combined_summary["total_credit"],
        "total_debit": combined_summary["total_debit"],
        "daily_data": daily_data,
        "daily_series": daily_series,
        "top_debits": combined_summary["top_debits"],
        "top_credits": combined_summary["top_credits"],
        "date_min": combined_summary.get("date_min", ""),
        "date_max": combined_summary.get("date_max", ""),
        "transactions_js": combined_summary.get("transactions_js", []),
        "kpi_avg_ticket": combined_summary.get("kpi_avg_ticket", 0.0),
        "kpi_active_days": combined_summary.get("kpi_active_days", 0),
        "kpi_spend_to_income": combined_summary.get("kpi_spend_to_income"),
        "statement_sources": statement_sources,
        "bank_accuracy": accuracy_breakdown,
        "bank_daily_series": bank_daily_series,
        "bank_totals": bank_totals,
        "transactions_table": table_rows,
        "transactions_columns": table_columns,
        "category_breakdown": combined_summary.get("category_breakdown", []),
        "category_topline": combined_summary.get("category_topline", {}),
        "category_total_transactions": combined_summary.get("category_total_transactions", 0),
        "insight_payload": insight_payload,
    }

@app.route('/login')
def login():
    if session.get('user_email'):
        return redirect(url_for('dashboard'))

    client_error = None if GOOGLE_CLIENT_ID else "Google client ID is not configured. Set GOOGLE_CLIENT_ID in .env."
    next_target = request.args.get("next") or ""
    force_webview = (request.args.get("wv") == "1")
    return render_template(
        'login.html',
        google_client_id=GOOGLE_CLIENT_ID or "",
        google_client_error=client_error,
        next_target=next_target,
        google_redirect_uri=GOOGLE_REDIRECT_URI,
        force_webview=force_webview,
    )


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
    picture = id_info.get("picture") or _fallback_picture(email) or _placeholder_avatar(full_name or email)
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


def _handle_gsi_login():
    data = request.get_json(silent=True) or request.form or {}
    credential = data.get("credential") or request.args.get("credential")

    if request.method == "GET" and not credential:
        return redirect(url_for("login"))

    if not credential:
        if request.form:
            # Redirect mode posted without credential (blocked/stripped); recover gracefully.
            return redirect(url_for("login"))
        return jsonify({"ok": False, "error": "Missing credential"}), 400

    if not GOOGLE_CLIENT_ID:
        return jsonify({"ok": False, "error": "Server missing GOOGLE_CLIENT_ID"}), 500

    try:
        idinfo = id_token.verify_oauth2_token(
            credential,
            google_requests.Request(),
            GOOGLE_CLIENT_ID,
            clock_skew_in_seconds=GOOGLE_CLOCK_SKEW,
        )
    except Exception as e:
    
        return jsonify({"ok": False, "error": "invalid_credential"}), 400

    email = idinfo.get("email")
    name = idinfo.get("name")
    picture = idinfo.get("picture")

    if not email:
        return jsonify({"ok": False, "error": "no_email"}), 400

    session["user_email"] = email
    session["name"] = name
    session["full_name"] = name
    session["picture"] = picture

    # If invoked via redirect mode (form POST), send a real redirect.
    if request.form or request.args.get("mode") == "redirect":
        return redirect("/dashboard")

    return jsonify({"ok": True, "success": True, "redirect": "/dashboard"})


@app.route("/auth/gsi-login", methods=["GET", "POST"])
def gsi_login():
    """
    Handles Google Identity Services sign-in.
    - JSON body -> popup mode
    - form POST -> redirect mode (WebView safe)
    - GET       -> recover to /login instead of blank page
    """
    return _handle_gsi_login()


@app.route("/auth/callback", methods=["GET", "POST"])
def gsi_callback():
    """Alternate redirect URI to match GOOGLE_REDIRECT_URI from env."""
    return _handle_gsi_login()


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
            history_entries = _load_history_entries(record)

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

    fallback_picture = _fallback_picture(session.get('user_email') or "")
    placeholder_picture = _placeholder_avatar(session.get("full_name") or session.get("user_email") or "U")
    if not session.get("picture"):
        session["picture"] = fallback_picture or placeholder_picture

    return render_template(
        'dashboard.html',
        full_name=session.get('full_name'),
        email=session.get('user_email'),
        picture=session.get('picture') or fallback_picture or placeholder_picture,
        history_columns=history_columns,
        history_rows=history_rows_full,
        history_preview=history_preview,
        history_total_rows=history_total_rows,
        history_saved_at=history_saved_at,
        history_entries=history_entries,
        history_error=history_error,
    )


@app.route('/history_summary/<int:entry_index>')
@login_required
def history_summary(entry_index):
    user_email = session.get("user_email")
    if not user_email:
        return redirect(url_for('login'))

    try:
        collection = get_normalized_collection()
        record = collection.find_one({"user_email": user_email}) or {}
        history_entries = _load_history_entries(record)
    except Exception as exc:
        return str(exc), 500

    if not history_entries or entry_index < 0 or entry_index >= len(history_entries):
        abort(404)

    entry = history_entries[entry_index]
    rows = entry.get("rows") or []
    if not rows:
        return "Saved entry has no rows", 400

    df = pd.DataFrame(rows)
    columns = entry.get("columns") or []
    if columns:
        ordered_columns = [col for col in columns if col in df.columns]
        if ordered_columns:
            df = df[ordered_columns]

    normalized_df = _standardize_dataframe(df)
    if normalized_df.empty:
        return "Saved extract has no valid rows to summarize", 400

    date_min = normalized_df["Date"].min() if "Date" in normalized_df else None
    date_max = normalized_df["Date"].max() if "Date" in normalized_df else None
    bank_label = (
        normalized_df["Bank"].iloc[0]
        if "Bank" in normalized_df and not normalized_df["Bank"].isna().all()
        else "Saved extract"
    )
    filename_label = entry.get("saved_at_ist") or entry.get("saved_at") or f"Saved extract #{entry_index + 1}"
    statement_sources = [{
        "bank": bank_label,
        "filename": filename_label,
        "transactions": len(normalized_df),
        "date_min": date_min.strftime("%Y-%m-%d") if pd.notna(date_min) else "",
        "date_max": date_max.strftime("%Y-%m-%d") if pd.notna(date_max) else "",
        "total_credit": float(normalized_df["Credit"].sum()) if "Credit" in normalized_df else 0.0,
        "total_debit": float(normalized_df["Debit"].sum()) if "Debit" in normalized_df else 0.0,
    }]

    summary_context = _build_summary_page_context(
        normalized_df,
        statement_sources=statement_sources,
    )
    safe_hash = hashlib.sha1(user_email.encode('utf-8')).hexdigest()[:8]
    timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    history_csv_name = f"history_{safe_hash}_{entry_index}_{timestamp}.csv"
    history_csv_path = os.path.join(OUTPUT_FOLDER, history_csv_name)
    try:
        normalized_df.to_csv(history_csv_path, index=False)
    except Exception as exc:
        return f"Unable to prepare CSV for download: {exc}", 500

    allowed = session.get('download_files') or []
    if history_csv_name not in allowed:
        allowed.append(history_csv_name)
    session['download_files'] = allowed
    session['download_file'] = history_csv_name
    session['paid'] = True

    summary_context.update({
        "download_link": f"/download/{history_csv_name}",
        "payment_required": False,
        "razorpay_key_id": os.environ.get('RAZORPAY_KEY_ID', ''),
        "razorpay_amount": int(os.environ.get('RAZORPAY_AMOUNT', '0')),
        "razorpay_currency": os.environ.get('RAZORPAY_CURRENCY', 'INR'),
    })

    return render_template('summary.html', **summary_context)


@app.route('/history_entry/<int:entry_index>/label', methods=['POST'])
@login_required
def history_entry_label(entry_index):
    user_email = session.get("user_email")
    if not user_email:
        return jsonify({"ok": False, "error": "Login required"}), 401

    data = request.get_json(silent=True) or {}
    label = (data.get("label") or "").strip()
    try:
        collection = get_normalized_collection()
        record = collection.find_one({"user_email": user_email}) or {}
        entries = _load_history_entries(record)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500

    if entry_index < 0 or entry_index >= len(entries):
        return jsonify({"ok": False, "error": "Entry not found"}), 404

    entries[entry_index]["label"] = label or None

    try:
        collection.update_one({"user_email": user_email}, {"$set": {"entries": entries}})
    except Exception as exc:
        return jsonify({"ok": False, "error": f"Unable to update label: {exc}"}), 500

    return jsonify({"ok": True, "label": label})


@app.route('/summary')
@login_required

def summary():
    return redirect(url_for('index'))


@app.route('/')
def index():
    return render_template('index.html')


def _load_session_dataframe() -> pd.DataFrame:
    """
    Load the latest normalized CSV for the current session.
    Raises ValueError if unavailable.
    """
    allowed = []
    stored = session.get('download_files')
    if isinstance(stored, list):
        allowed.extend(stored)
    legacy = session.get('download_file')
    if isinstance(legacy, str):
        allowed.append(legacy)

    if not allowed:
        raise ValueError("No normalized file available for this session.")

    latest_file = allowed[-1]
    csv_path = os.path.join(OUTPUT_FOLDER, latest_file)
    if not os.path.exists(csv_path):
        raise ValueError("Normalized file missing on server.")

    df = pd.read_csv(csv_path)
    if "Date" in df.columns:
        df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
        df = df.dropna(subset=["Date"])
    return df


def _filter_ledger(df: pd.DataFrame, start_date: str = "", end_date: str = "") -> pd.DataFrame:
    filtered = df.copy()
    if start_date:
        try:
            start = pd.to_datetime(start_date, errors="coerce")
            if not pd.isna(start):
                filtered = filtered[filtered["Date"] >= start]
        except Exception:
            pass
    if end_date:
        try:
            end = pd.to_datetime(end_date, errors="coerce")
            if not pd.isna(end):
                filtered = filtered[filtered["Date"] <= end]
        except Exception:
            pass

    filtered = filtered.sort_values("Date", ascending=False).reset_index(drop=True)
    return filtered


def _sanitize_text(value, limit: int = 160) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\t", " ").replace("\n", " ").strip()
    return text[:limit]


def build_statement_context(df: pd.DataFrame, keyword: str = "") -> str:
    if df is None or df.empty:
        return "No statement data available."

    working = df.copy()
    if "Date" in working.columns:
        working["Date"] = pd.to_datetime(working["Date"], errors="coerce")

    columns = list(working.columns)
    date_min = ""
    date_max = ""
    if "Date" in working.columns and not working["Date"].dropna().empty:
        date_min_ts = working["Date"].min()
        date_max_ts = working["Date"].max()
        date_min = date_min_ts.strftime("%Y-%m-%d") if isinstance(date_min_ts, pd.Timestamp) else ""
        date_max = date_max_ts.strftime("%Y-%m-%d") if isinstance(date_max_ts, pd.Timestamp) else ""

    total_debit = float(working["Debit"].sum()) if "Debit" in working.columns else None
    total_credit = float(working["Credit"].sum()) if "Credit" in working.columns else None

    def _top_transactions(kind: str):
        col = "Debit" if kind == "debit" else "Credit"
        if col not in working.columns:
            return []
        subset = working.copy()
        subset[col] = pd.to_numeric(subset[col], errors="coerce").fillna(0)
        subset = subset[subset[col] > 0]
        subset = subset.sort_values(col, ascending=False).head(5)
        results = []
        for _, row in subset.iterrows():
            results.append({
                "Date": row["Date"].strftime("%Y-%m-%d") if isinstance(row.get("Date"), pd.Timestamp) else _sanitize_text(row.get("Date", "")),
                "Details": _sanitize_text(row.get("Details", ""), 120),
                "Amount": float(row.get(col, 0) or 0),
                "Balance": row.get("Balance", ""),
            })
        return results

    top_debits = _top_transactions("debit")
    top_credits = _top_transactions("credit")

    selected_columns = [col for col in ["Date", "Details", "Debit", "Credit", "Balance", "Transaction Type", "Payment Category", "Merchant", "Bank"] if col in working.columns]
    rows = working.tail(40)
    if "Date" in rows.columns:
        rows["Date"] = rows["Date"].dt.strftime("%Y-%m-%d")

    def _format_row(row):
        return "\t".join(_sanitize_text(row.get(col, "")) for col in selected_columns)

    keyword_rows = []
    if keyword:
        keyword_lower = keyword.lower()
        def _match(val):
            return keyword_lower in str(val).lower()
        mask = False
        for col in ["Details", "Merchant", "Payment Category", "Transaction Type"]:
            if col in working.columns:
                col_mask = working[col].astype(str).str.lower().str.contains(keyword_lower, na=False)
                mask = col_mask if isinstance(mask, bool) else (mask | col_mask)
        keyword_rows = working[mask].tail(10) if not isinstance(mask, bool) else working.head(0)
        if "Date" in keyword_rows.columns:
            keyword_rows["Date"] = keyword_rows["Date"].dt.strftime("%Y-%m-%d")

    lines = []
    lines.append(f"COLUMNS: {', '.join(columns)}")
    if date_min or date_max:
        lines.append(f"DATE_RANGE: {date_min} to {date_max}")
    if total_debit is not None or total_credit is not None:
        lines.append(f"TOTALS: debit={total_debit or 0:.2f}, credit={total_credit or 0:.2f}")
    lines.append("TOP_DEBITS (max 5):")
    for item in top_debits:
        bal = f" | Balance={item['Balance']}" if "Balance" in working.columns else ""
        lines.append(f"{item['Date']} | {item['Details']} | {item['Amount']:.2f}{bal}")
    lines.append("TOP_CREDITS (max 5):")
    for item in top_credits:
        bal = f" | Balance={item['Balance']}" if "Balance" in working.columns else ""
        lines.append(f"{item['Date']} | {item['Details']} | {item['Amount']:.2f}{bal}")
    lines.append("LAST_ROWS (most recent 40):")
    lines.append("\t".join(selected_columns))
    for _, row in rows.iterrows():
        lines.append(_format_row(row))
    if keyword_rows is not None and len(keyword_rows) > 0:
        lines.append(f"KEYWORD_MATCHES (keyword='{keyword}') latest 10:")
        lines.append("\t".join(selected_columns))
        for _, row in keyword_rows.tail(10).iterrows():
            lines.append(_format_row(row))
    return "\n".join(lines)


def call_ollama_cloud_chat(system_prompt: str, statement_context: str, question: str) -> str:
    final_url = f"{OLLAMA_HOST}/api/chat"
    headers = {
        "Authorization": f"Bearer {OLLAMA_API_KEY}",
        "Content-Type": "application/json",
    }
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"STATEMENT_CONTEXT:\n{statement_context}\n\nQUESTION:\n{question}"},
    ]
    payload = {
        "model": LLM_MODEL,
        "messages": messages,
        "stream": False
    }


    try:
        resp = requests.post(final_url, json=payload, headers=headers, timeout=LLM_TIMEOUT)
    except Exception as exc:
        raise RuntimeError(f"LLM call failed: {exc}") from exc
    if not resp.ok:
        raise RuntimeError(f"LLM call failed: HTTP {resp.status_code} {resp.text}")
    data = resp.json()
    message = data.get("message") or {}
    content = message.get("content") or data.get("response") or ""
    if not isinstance(content, str):
        raise RuntimeError("LLM call failed: Empty response body.")
    return content.strip()


@app.route('/chat/ledger', methods=['POST'])
@login_required
def chat_ledger():
    auth_block = _require_login_json()
    if auth_block:
        return auth_block

    try:
        data = request.get_json(force=True) or {}
        question = (data.get("question") or "").strip()
        keyword = (data.get("keyword") or "").strip()

        if not question:
            return jsonify(ok=False, error="Missing question"), 400

        df = _load_session_dataframe()
        if df is None or df.empty:
            return jsonify(ok=False, error="No transactions available"), 400

        statement_context = build_statement_context(df, keyword=keyword)

        system_prompt = (
            "You are LedgerBot. Answer ONLY using STATEMENT_CONTEXT. "
            "If the question cannot be answered from the statement, reply exactly: This information is not available in the provided statement.\n"
            "OUTPUT FORMAT RULES (MANDATORY):\n"
            "1) Plain text only. Do NOT use markdown, code blocks, bullets, or decorative characters.\n"
            "2) Do NOT expose internal or debug phrases such as: Rows used, filtered total, context length, tokens, analysis, based on the data above, according to the rows.\n"
            "3) Use clean, human-friendly financial language with short sentences; avoid technical terms unless required.\n"
            "4) Currency formatting: use the currency symbol exactly as shown in the statement (e.g., ₹) with comma separators for thousands (₹25,000).\n"
            "5) When listing transactions (max 5 rows unless asked for more), use this format exactly:\n"
            "   Date | Description | Debit | Credit | Balance\n"
            "   Leave a field blank if not available. Do not add extra columns.\n"
            "6) When summarizing numbers, start with a short title line, then list key values on separate lines, e.g.:\n"
            "   Monthly Summary\n"
            "   Total debit: ₹12,450\n"
            "   Total credit: ₹18,000\n"
            "   Net difference: ₹5,550 credit surplus\n"
            "7) If there are no matching transactions, say exactly: No transactions matching this query were found in the statement.\n"
            "8) Tone: neutral, professional, calm, no opinions.\n"
            "9) End the response cleanly. Do NOT add explanations or follow-up questions unless clarification is required by ambiguity."
        )

        answer = call_ollama_cloud_chat(system_prompt, statement_context, question)

        return jsonify(ok=True, answer=answer)

    except Exception as e:
        err_msg = str(e)
        if not err_msg.startswith("LLM call failed:"):
            err_msg = f"LLM call failed: {err_msg}"
        return jsonify(ok=False, error=err_msg), 500


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
    data = request.get_json(silent=True) or {}
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

    entry_label = (data.get("label") or "").strip()
    entry = {
        "columns": new_columns,
        "rows": normalize_rows(new_columns, new_rows),
        "row_count": len(new_rows),
        "saved_at": datetime.utcnow(),
        "saved_at_ist": saved_at_ist,
    }
    if entry_label:
        entry["label"] = entry_label

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
