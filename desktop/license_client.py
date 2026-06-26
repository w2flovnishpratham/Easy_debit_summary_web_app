"""
Desktop license management.
- Validates license key against VPS at first activation.
- Caches a signed token locally for offline grace period (48 h).
- Exposes verify_gateway_token() so the VPS gateway can check desktop tokens.
"""
import hashlib
import hmac
import json
import os
import time
from pathlib import Path

import requests

from desktop.device_id import get_device_id

VPS_URL      = os.environ.get("VPS_URL", "https://easydebitsummary.com").rstrip("/")
GATEWAY_SECRET = os.environ.get("LICENSE_GATEWAY_SECRET", "change-me-in-env")  # shared VPS secret
GRACE_HOURS  = 48
TOKEN_FILE   = Path(os.environ.get("APPDATA") or Path.home() / ".eds") / "license_token.json"


def _token_path() -> Path:
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    return TOKEN_FILE


def _load_token() -> dict:
    try:
        return json.loads(_token_path().read_text())
    except Exception:
        return {}


def _save_token(data: dict):
    _token_path().write_text(json.dumps(data))


def _sign(payload: str) -> str:
    return hmac.new(GATEWAY_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()


def is_license_valid() -> bool:
    token = _load_token()
    if not token:
        return False
    expires_at = token.get("expires_at", 0)
    if time.time() < expires_at:
        return True
    # grace period: allow GRACE_HOURS past expiry before blocking
    grace_until = token.get("grace_until", 0)
    if time.time() < grace_until:
        refresh_license()  # background attempt
        return True
    return False


def license_status() -> dict:
    token = _load_token()
    if not token:
        return {"ok": False, "active": False, "reason": "no_license"}
    expires_at = token.get("expires_at", 0)
    grace_until = token.get("grace_until", 0)
    now = time.time()
    if now < expires_at:
        return {"ok": True, "active": True, "expires_at": expires_at}
    if now < grace_until:
        return {"ok": True, "active": True, "grace": True, "grace_until": grace_until}
    return {"ok": False, "active": False, "reason": "expired"}


def activate_license(email: str, license_key: str) -> dict:
    device_id = get_device_id()
    try:
        resp = requests.post(
            f"{VPS_URL}/api/license/activate",
            json={"email": email, "license_key": license_key, "device_id": device_id},
            timeout=15,
        )
        data = resp.json()
    except Exception as exc:
        return {"ok": False, "error": f"Cannot reach activation server: {exc}"}

    if data.get("ok") and data.get("token"):
        _save_token({
            "email": email,
            "token": data["token"],
            "expires_at": data.get("expires_at", time.time() + 30 * 86400),
            "grace_until": data.get("expires_at", time.time() + 30 * 86400) + GRACE_HOURS * 3600,
        })
        return {"ok": True, "message": "License activated successfully."}
    return {"ok": False, "error": data.get("error", "Activation failed.")}


def refresh_license() -> dict:
    token = _load_token()
    if not token:
        return {"ok": False, "error": "No token to refresh"}
    try:
        resp = requests.post(
            f"{VPS_URL}/api/license/refresh",
            json={"token": token.get("token"), "device_id": get_device_id()},
            timeout=10,
        )
        data = resp.json()
        if data.get("ok") and data.get("token"):
            token.update({
                "token": data["token"],
                "expires_at": data.get("expires_at", time.time() + 30 * 86400),
                "grace_until": data.get("expires_at", time.time() + 30 * 86400) + GRACE_HOURS * 3600,
            })
            _save_token(token)
            return {"ok": True}
        return {"ok": False, "error": data.get("error", "Refresh failed")}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def get_gateway_token() -> str:
    """Return the stored bearer token for calling the VPS LLM gateway."""
    return _load_token().get("token", "")


def verify_gateway_token(token: str) -> bool:
    """
    VPS-side check: verify a token submitted by a desktop app.
    Simple HMAC check — in production, use a real JWT or DB lookup.
    """
    if not token or len(token) < 10:
        return False
    # Accept tokens signed with our gateway secret
    parts = token.split(".", 1)
    if len(parts) != 2:
        return False
    payload, sig = parts
    expected = _sign(payload)
    return hmac.compare_digest(sig, expected)
