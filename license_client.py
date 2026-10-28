"""Client for the BankStatementConverter backend, used by app.py.

Handles sign-up/sign-in against Supabase Auth and the per-conversion usage
check. See backend/README.md for how to stand up the actual Supabase
project this talks to.

Configure SUPABASE_URL / SUPABASE_ANON_KEY below once the Supabase project
exists; both are safe to ship inside the desktop app since the anon key
only grants what the row-level-security policies and
check_and_increment_usage()'s own auth.uid() scoping allow.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
from pathlib import Path
from urllib.parse import quote_plus

import requests

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://ijuoslevahlojwjmjuco.supabase.co")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "sb_publishable_xOrXnGIppwXlLgBkyBzo_A_U5LQN8zU")
FREE_TIER_LIMIT = 5  # conversions per calendar month; keep in sync with your pricing page

CONFIG_PATH = Path.home() / ".bankstatementconverter" / "session.json"
TIMEOUT = 15

# --- PayFast checkout ---
# These two are PayFast SANDBOX test credentials (no real money moves) --
# swap for your live Merchant ID/Key (Settings in your PayFast dashboard)
# before actually selling anything, and set PAYFAST_SANDBOX=false then too.
PAYFAST_MERCHANT_ID = os.environ.get("PAYFAST_MERCHANT_ID", "10056596")
PAYFAST_MERCHANT_KEY = os.environ.get("PAYFAST_MERCHANT_KEY", "q3bya8kvbrh3n")
PAYFAST_PASSPHRASE = os.environ.get("PAYFAST_PASSPHRASE", "")  # optional; must match your PayFast dashboard exactly
PAYFAST_SANDBOX = os.environ.get("PAYFAST_SANDBOX", "true").lower() != "false"
PAYFAST_PROCESS_URL = (
    "https://sandbox.payfast.co.za/eng/process" if PAYFAST_SANDBOX else "https://www.payfast.co.za/eng/process"
)
PRO_MONTHLY_PRICE = os.environ.get("PRO_MONTHLY_PRICE", "99.00")  # ZAR; tune to your pricing

# Derived from SUPABASE_URL once you've deployed the payfast-webhook and
# payfast-return Edge Functions under those exact names (see backend/README.md).
PAYFAST_NOTIFY_URL = f"{SUPABASE_URL}/functions/v1/payfast-webhook"
PAYFAST_RETURN_URL = f"{SUPABASE_URL}/functions/v1/payfast-return?status=success"
PAYFAST_CANCEL_URL = f"{SUPABASE_URL}/functions/v1/payfast-return?status=cancelled"


class LicenseError(Exception):
    """Sign-in failed, or the usage check couldn't be completed."""


def is_configured() -> bool:
    """False until SUPABASE_URL/SUPABASE_ANON_KEY are set to real values."""
    return "YOUR-PROJECT" not in SUPABASE_URL and "YOUR-ANON-KEY" not in SUPABASE_ANON_KEY


def _save_session(data: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(data))


def _load_session() -> dict | None:
    if not CONFIG_PATH.exists():
        return None
    try:
        return json.loads(CONFIG_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _post(url: str, **kwargs) -> requests.Response:
    """requests.post, with network failures (no internet, DNS, timeout, ...)
    turned into a LicenseError instead of an uncaught requests exception."""
    try:
        return requests.post(url, timeout=TIMEOUT, **kwargs)
    except requests.exceptions.RequestException as exc:
        raise LicenseError(f"Couldn't reach the server ({exc.__class__.__name__}). Check your internet connection.") from exc


def _error_message(resp: requests.Response, *keys: str, default: str) -> str:
    try:
        data = resp.json()
    except ValueError:
        return default
    for key in keys:
        if data.get(key):
            return data[key]
    return default


def is_signed_in() -> bool:
    return current_access_token() is not None


def current_access_token() -> str | None:
    session = _load_session()
    return session.get("access_token") if session else None


def current_email() -> str | None:
    session = _load_session()
    if not session:
        return None
    return (session.get("user") or {}).get("email")


def current_user_id() -> str | None:
    session = _load_session()
    if not session:
        return None
    return (session.get("user") or {}).get("id")


def sign_up(email: str, password: str) -> dict:
    resp = _post(
        f"{SUPABASE_URL}/auth/v1/signup",
        headers={"apikey": SUPABASE_ANON_KEY, "Content-Type": "application/json"},
        json={"email": email, "password": password},
    )
    if resp.status_code >= 400:
        raise LicenseError(_error_message(resp, "msg", "error_description", default="Sign up failed"))
    data = resp.json()
    if data.get("access_token"):
        _save_session(data)
    return data


def sign_in(email: str, password: str) -> dict:
    resp = _post(
        f"{SUPABASE_URL}/auth/v1/token?grant_type=password",
        headers={"apikey": SUPABASE_ANON_KEY, "Content-Type": "application/json"},
        json={"email": email, "password": password},
    )
    if resp.status_code >= 400:
        raise LicenseError(_error_message(resp, "error_description", "msg", default="Sign in failed"))
    data = resp.json()
    _save_session(data)
    return data


def sign_out() -> None:
    if CONFIG_PATH.exists():
        CONFIG_PATH.unlink()


def check_and_increment_usage(limit: int = FREE_TIER_LIMIT) -> dict:
    """Call once per conversion, before doing the work.

    Returns {"allowed": bool, "tier": "free"|"pro", "remaining": int|None}.
    Raises LicenseError if not signed in, or if the request fails outright
    (e.g. no internet) -- the caller decides whether that should block the
    conversion or be treated as a grace fallback.
    """
    token = current_access_token()
    if not token:
        raise LicenseError("Not signed in")

    resp = _post(
        f"{SUPABASE_URL}/rest/v1/rpc/check_and_increment_usage",
        headers={
            "apikey": SUPABASE_ANON_KEY,
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json={"p_limit": limit},
    )
    if resp.status_code >= 400:
        raise LicenseError(_error_message(resp, "message", "error_description", default="Usage check failed"))
    return resp.json()


def submit_bug_report(
    bank_name: str,
    details: str,
    app_version: str | None = None,
    used_ocr: bool | None = None,
    sample_rows: list[dict] | None = None,
) -> None:
    """Opt-in only -- call this ONLY after the user explicitly asks to send
    a report (e.g. clicks "Report an Issue" and confirms). Never call this
    automatically; nothing is collected silently. `sample_rows`, if passed,
    should already be whatever the user agreed to include -- this function
    does no redaction itself. Write-only on the server (see
    backend/supabase/migrations/0002_bug_reports.sql): not even the
    submitting user can read reports back with the anon key.
    """
    _post(
        f"{SUPABASE_URL}/rest/v1/bug_reports",
        headers={"apikey": SUPABASE_ANON_KEY, "Content-Type": "application/json"},
        json={
            "user_id": current_user_id(),
            "bank_name": bank_name,
            "details": details,
            "app_version": app_version,
            "used_ocr": used_ocr,
            "sample_rows": sample_rows,
        },
    )


def _php_urlencode(value: str) -> str:
    """Match PHP's urlencode() exactly (spaces -> '+', uppercase hex, and --
    unlike Python's quote_plus, which treats it as always-safe -- '~' percent
    -encoded). PayFast's signature is computed with PHP's urlencode, so this
    has to match byte for byte or every signature silently comes out wrong.
    """
    return quote_plus(str(value), safe="").replace("~", "%7E")


def _payfast_signature(data: dict) -> str:
    """Field order is insertion order (PayFast's own SDK does the same, not
    alphabetical) -- see backend/README.md for where this was confirmed.
    """
    parts = []
    for key, value in data.items():
        if value in (None, ""):
            continue
        parts.append(f"{key}={_php_urlencode(str(value).strip())}")
    if PAYFAST_PASSPHRASE and "passphrase" not in data:
        parts.append(f"passphrase={_php_urlencode(PAYFAST_PASSPHRASE.strip())}")
    query = "&".join(parts)
    return hashlib.md5(query.encode("utf-8")).hexdigest()


def build_upgrade_checkout_html() -> str:
    """A self-submitting HTML page that POSTs a signed PayFast payment
    request for the Pro subscription for the currently signed-in user.

    This is a one-time payment, not yet a true recurring subscription (which
    needs subscription_type/frequency/cycles fields and a PayFast passphrase)
    -- see backend/README.md for upgrading to real recurring billing once
    this simpler flow is confirmed working end to end.
    """
    user_id = current_user_id()
    if not user_id:
        raise LicenseError("Not signed in")

    data = {
        "merchant_id": PAYFAST_MERCHANT_ID,
        "merchant_key": PAYFAST_MERCHANT_KEY,
        "return_url": PAYFAST_RETURN_URL,
        "cancel_url": PAYFAST_CANCEL_URL,
        "notify_url": PAYFAST_NOTIFY_URL,
        "email_address": current_email() or "",
        "m_payment_id": user_id,
        "amount": f"{float(PRO_MONTHLY_PRICE):.2f}",
        "item_name": "BankStatementConverter Pro",
        "custom_str1": user_id,
    }
    data["signature"] = _payfast_signature(data)

    inputs = "\n".join(
        f'<input type="hidden" name="{html.escape(k)}" value="{html.escape(str(v))}">' for k, v in data.items()
    )
    return f"""<!doctype html>
<html><body onload="document.forms[0].submit()">
<form action="{html.escape(PAYFAST_PROCESS_URL)}" method="post">
{inputs}
</form>
<p>Redirecting to PayFast...</p>
</body></html>"""
