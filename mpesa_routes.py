"""
Daraja (M-Pesa) STK Push integration for Elimu Hub.

Scope is deliberately narrow — this module only ever COLLECTS money, never
sends it out, so it only implements Lipa Na M-Pesa Online (STK Push):

  1. School subscription — a flat fee, paid by a school admin, that unlocks
     the whole school for a period (termly or yearly). Stored as
     schools.subscription_expires_at.
  2. Scheme of work print/download — a small fee, paid by an individual
     teacher, that unlocks printing/downloading one specific scheme copy
     for the rest of that scheme's year. Stored as rows in
     scheme_print_unlocks, keyed by (copy_id, user_id, year) — a teacher
     who has already paid for e.g. Grade 4 Term 2 Mathematics this year
     can print/download it as many times as they like without paying
     again, but a future year's version of that same subject/grade is a
     new scheme_copies row (new upload → new master → new copy) and gets
     billed again on its own.

All prices (subscription termly/yearly amounts, scheme print price) are
set by the Super Admin at /superadmin/billing/settings and stored in the
billing_settings table — nothing money-related is hardcoded here.

Environment variables (set these on Render):
    MPESA_ENV                 "sandbox" or "production"        (default: sandbox)
    MPESA_CONSUMER_KEY        from your Daraja app
    MPESA_CONSUMER_SECRET     from your Daraja app
    MPESA_SHORTCODE           your Paybill/Till (sandbox default: 174379)
    MPESA_PASSKEY             your Lipa Na M-Pesa Online passkey
    MPESA_CALLBACK_URL        full public HTTPS URL Safaricom should POST
                               results to, e.g.
                               https://elimuhub.onrender.com/api/v1/mpesa/callback
"""

import os
import re
import base64
import logging
import datetime
import requests
from fastapi import APIRouter, Request, HTTPException, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from shared import (
    esc,
    get_db_connection,
    RealDictCursor,
    require_school_session,
    require_admin_session,
    require_superadmin_session,
    get_current_session_user,
    get_dashboard_url,
)

router = APIRouter()
logger = logging.getLogger(__name__)

# Subscription plan durations — how long each plan lasts once paid. Not
# super-admin-editable (only the amounts are); these are fixed to match
# the Kenyan school calendar with a small buffer for late renewal.
SUBSCRIPTION_PLAN_DAYS = {
    "termly": 120,
    "yearly": 380,
}
SUBSCRIPTION_PLAN_LABELS = {
    "termly": "Per Term",
    "yearly": "Per Year",
}

# Master switch for actually charging anyone. Defaults to OFF, so pushing
# this doesn't suddenly block real teachers/admins at live schools from
# printing schemes or losing dashboard access before production M-Pesa
# credentials are ready. Flip MPESA_BILLING_ENFORCED=true on Render only
# once real Daraja production credentials are in place — until then,
# every paywall in this module is bypassed and everything stays free.
BILLING_ENFORCED = (os.getenv("MPESA_BILLING_ENFORCED") or "false").strip().lower() == "true"

# --- Daraja config ---
MPESA_ENV = (os.getenv("MPESA_ENV") or "sandbox").strip().lower()
MPESA_CONSUMER_KEY = (os.getenv("MPESA_CONSUMER_KEY") or "").strip()
MPESA_CONSUMER_SECRET = (os.getenv("MPESA_CONSUMER_SECRET") or "").strip()
MPESA_SHORTCODE = (os.getenv("MPESA_SHORTCODE") or "174379").strip()
MPESA_PASSKEY = (os.getenv("MPESA_PASSKEY") or "").strip()
MPESA_CALLBACK_URL = (os.getenv("MPESA_CALLBACK_URL") or "").strip()

MPESA_BASE_URL = (
    "https://api.safaricom.co.ke" if MPESA_ENV == "production"
    else "https://sandbox.safaricom.co.ke"
)


def bootstrap_mpesa_schema():
    """Creates/upgrades every table this module owns. Purely additive."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE schools ADD COLUMN IF NOT EXISTS subscription_expires_at TIMESTAMP;")

            # One-time backfill for schools that existed before this trial
            # feature was added — they have subscription_expires_at = NULL,
            # which would otherwise mean an abrupt cutoff to "no active
            # subscription" the instant billing is enforced, unlike new
            # signups which get a one-term trial automatically at creation
            # (see the registration route in main.py). This grants the same
            # one-term grace period retroactively, but the WHERE clause
            # makes it safe to re-run on every deploy — it only ever
            # touches a school that has NEVER had any subscription/trial
            # value set, so it never overwrites a real paid subscription
            # or a trial that's already been granted (including by this
            # same backfill running again on the next deploy).
            cur.execute("""
                UPDATE schools
                SET subscription_expires_at = NOW() + (%s || ' days')::INTERVAL
                WHERE subscription_expires_at IS NULL;
            """, (SUBSCRIPTION_PLAN_DAYS["termly"],))
            # Distinguishes "free trial" from "actually paid" purely for
            # display — both use subscription_expires_at as the single
            # source of truth for whether printing/subscription features
            # are unlocked, so is_school_subscription_active() and the
            # print-paywall gating never need to know or care which kind
            # of active period a school is in.
            cur.execute("ALTER TABLE schools ADD COLUMN IF NOT EXISTS subscription_is_trial BOOLEAN NOT NULL DEFAULT FALSE;")

            # Single-row settings table — Super Admin edits these amounts,
            # nothing about pricing lives in code.
            cur.execute("""
                CREATE TABLE IF NOT EXISTS billing_settings (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    subscription_termly_amount NUMERIC(10, 2) NOT NULL DEFAULT 4000,
                    subscription_yearly_amount NUMERIC(10, 2) NOT NULL DEFAULT 11000,
                    scheme_print_amount NUMERIC(10, 2) NOT NULL DEFAULT 20,
                    updated_at TIMESTAMP DEFAULT NOW(),
                    CONSTRAINT billing_settings_single_row CHECK (id = 1)
                );
            """)
            cur.execute("INSERT INTO billing_settings (id) VALUES (1) ON CONFLICT (id) DO NOTHING;")

            cur.execute("""
                CREATE TABLE IF NOT EXISTS mpesa_transactions (
                    id SERIAL PRIMARY KEY,
                    school_id INTEGER NOT NULL REFERENCES schools(id) ON DELETE CASCADE,
                    initiated_by_user_id INTEGER,
                    purpose VARCHAR(30) NOT NULL,          -- 'subscription' or 'scheme_print'
                    reference_id INTEGER,                   -- copy_id for scheme_print, plan days for subscription
                    amount NUMERIC(10, 2) NOT NULL,
                    phone_number VARCHAR(20) NOT NULL,
                    account_reference VARCHAR(50),
                    merchant_request_id VARCHAR(100),
                    checkout_request_id VARCHAR(100) UNIQUE,
                    status VARCHAR(20) NOT NULL DEFAULT 'pending',  -- pending, completed, failed
                    mpesa_receipt_number VARCHAR(50),
                    result_desc TEXT,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                );
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS scheme_print_unlocks (
                    id SERIAL PRIMARY KEY,
                    copy_id INTEGER NOT NULL REFERENCES scheme_copies(id) ON DELETE CASCADE,
                    user_id INTEGER NOT NULL,
                    school_id INTEGER NOT NULL REFERENCES schools(id) ON DELETE CASCADE,
                    year INTEGER NOT NULL,
                    unlocked_at TIMESTAMP DEFAULT NOW(),
                    UNIQUE (copy_id, user_id, year)
                );
            """)

            # Each teacher tops this up via M-Pesa whenever they like, then
            # unlocking a scheme's print/download just deducts from it
            # instantly — no separate M-Pesa prompt per scheme.
            cur.execute("""
                CREATE TABLE IF NOT EXISTS staff_wallets (
                    user_id INTEGER PRIMARY KEY,
                    school_id INTEGER NOT NULL REFERENCES schools(id) ON DELETE CASCADE,
                    balance NUMERIC(10, 2) NOT NULL DEFAULT 0,
                    updated_at TIMESTAMP DEFAULT NOW()
                );
            """)
            conn.commit()


def get_billing_settings() -> dict:
    """Reads the Super Admin's current prices. Always returns a row —
    bootstrap_mpesa_schema guarantees one exists."""
    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM billing_settings WHERE id = 1;")
            return cur.fetchone()


def _normalize_msisdn(phone: str) -> str:
    """Daraja wants 2547XXXXXXXX / 2541XXXXXXXX with no plus, no spaces.
    Accepts however a Kenyan number is typically typed (07..., 01...,
    +254..., 254...) and normalizes it."""
    digits = re.sub(r"\D", "", phone or "")
    if digits.startswith("0") and len(digits) == 10:
        return "254" + digits[1:]
    if digits.startswith("254") and len(digits) == 12:
        return digits
    if digits.startswith("7") or digits.startswith("1"):
        if len(digits) == 9:
            return "254" + digits
    return digits


def _get_access_token() -> str:
    if not MPESA_CONSUMER_KEY or not MPESA_CONSUMER_SECRET:
        raise RuntimeError("MPESA_CONSUMER_KEY / MPESA_CONSUMER_SECRET are not set.")

    resp = requests.get(
        f"{MPESA_BASE_URL}/oauth/v1/generate?grant_type=client_credentials",
        auth=(MPESA_CONSUMER_KEY, MPESA_CONSUMER_SECRET),
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def _initiate_stk_push(phone: str, amount: int, account_reference: str, transaction_desc: str) -> dict:
    """Calls Daraja's STK Push endpoint. Returns the raw JSON response,
    which on success contains MerchantRequestID and CheckoutRequestID —
    the callback later arrives keyed by CheckoutRequestID."""
    if not MPESA_SHORTCODE or not MPESA_PASSKEY:
        raise RuntimeError("MPESA_SHORTCODE / MPESA_PASSKEY are not set.")
    if not MPESA_CALLBACK_URL:
        raise RuntimeError("MPESA_CALLBACK_URL is not set.")

    token = _get_access_token()
    timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    password = base64.b64encode(
        f"{MPESA_SHORTCODE}{MPESA_PASSKEY}{timestamp}".encode()
    ).decode()

    payload = {
        "BusinessShortCode": MPESA_SHORTCODE,
        "Password": password,
        "Timestamp": timestamp,
        "TransactionType": "CustomerPayBillOnline",
        "Amount": int(amount),
        "PartyA": phone,
        "PartyB": MPESA_SHORTCODE,
        "PhoneNumber": phone,
        "CallBackURL": MPESA_CALLBACK_URL,
        "AccountReference": account_reference[:12],
        "TransactionDesc": transaction_desc[:20],
    }

    resp = requests.post(
        f"{MPESA_BASE_URL}/mpesa/stkpush/v1/processrequest",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


def _page_shell(title: str, body: str) -> str:
    return f"""
    <!DOCTYPE html>
    <html>
    <head><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Elimu Hub | {esc(title)}</title><script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script></head>
    <body class="bg-slate-100 min-h-screen p-4 sm:p-8">
        <div class="max-w-md mx-auto space-y-4">
            {body}
        </div>
    </body>
    </html>
    """


# ---------------------------------------------------------------------
# Subscription — paid by a school admin, unlocks the whole school.
# ---------------------------------------------------------------------

@router.get("/billing/subscription/{school_id}", response_class=HTMLResponse)
def subscription_billing_page(school_id: int, request: Request):
    auth_error = require_admin_session(request, school_id)
    if auth_error:
        return auth_error

    viewer = get_current_session_user(request)
    settings = get_billing_settings()
    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT subscription_expires_at FROM schools WHERE id = %s;", (school_id,))
            school = cur.fetchone()

    expires_at = school["subscription_expires_at"] if school else None
    is_active = bool(expires_at and expires_at > datetime.datetime.now())
    status_html = (
        f"<div class='bg-emerald-50 border border-emerald-200 text-emerald-800 text-xs px-4 py-2 rounded-xl'>✅ Active until {expires_at.strftime('%d %b %Y')}</div>"
        if is_active else
        "<div class='bg-amber-50 border border-amber-200 text-amber-800 text-xs px-4 py-2 rounded-xl'>⚠️ No active subscription</div>"
    )

    plan_amounts = {
        "termly": settings["subscription_termly_amount"],
        "yearly": settings["subscription_yearly_amount"],
    }
    plan_options = "".join(
        f"""<label class="flex items-center justify-between p-3 border rounded-xl cursor-pointer hover:bg-slate-50">
                <span><input type="radio" name="plan" value="{key}" {"checked" if key == "termly" else ""} class="mr-2">{esc(SUBSCRIPTION_PLAN_LABELS[key])}</span>
                <span class="font-bold text-slate-700">KES {amount:,.0f}</span>
            </label>"""
        for key, amount in plan_amounts.items()
    )

    if not BILLING_ENFORCED:
        # Not live yet — show the plans for reference, but no working
        # payment form, since a real M-Pesa number can't complete an STK
        # push against sandbox credentials.
        payment_section = f"""
            <div class="bg-slate-50 border border-slate-200 text-slate-600 text-xs px-4 py-3 rounded-xl">
                💳 Subscriptions aren't live yet — check back soon. Here's a preview of the plans:
            </div>
            <div class="space-y-2 opacity-60 pointer-events-none">{plan_options}</div>
        """
    else:
        payment_section = f"""
            <form method="post" action="/billing/subscription/{school_id}/pay" class="space-y-3">
                <div class="space-y-2">{plan_options}</div>
                <div>
                    <label class="text-xs font-bold text-slate-500">M-Pesa phone number</label>
                    <input type="tel" name="phone_number" placeholder="07XXXXXXXX" value="{esc(viewer.get('phone_number') or '') if viewer else ''}" required class="w-full p-2.5 border rounded-lg mt-1 bg-white">
                </div>
                <button type="submit" class="w-full bg-emerald-600 hover:bg-emerald-700 text-white py-2.5 rounded-xl text-sm font-bold transition">Pay with M-Pesa</button>
            </form>
        """

    body = f"""
        <a href="{get_dashboard_url(request, school_id)}" class="text-slate-500 hover:text-slate-700 text-xs font-bold inline-block">← Back to Dashboard</a>
        <div class="bg-white p-6 rounded-2xl border shadow-xs space-y-4">
            <h2 class="text-lg font-black text-slate-800">Elimu Hub Subscription</h2>
            {status_html if BILLING_ENFORCED else ""}
            {payment_section}
        </div>
    """
    return _page_shell("Subscription", body)


@router.post("/billing/subscription/{school_id}/pay")
async def subscription_billing_pay(school_id: int, request: Request, plan: str = Form(...), phone_number: str = Form(...)):
    auth_error = require_admin_session(request, school_id)
    if auth_error:
        return auth_error

    if not BILLING_ENFORCED:
        raise HTTPException(status_code=403, detail="Subscriptions aren't live yet.")

    if plan not in SUBSCRIPTION_PLAN_DAYS:
        raise HTTPException(status_code=400, detail="Unknown plan.")

    viewer = get_current_session_user(request)
    settings = get_billing_settings()
    plan_amount = settings["subscription_termly_amount"] if plan == "termly" else settings["subscription_yearly_amount"]
    plan_days = SUBSCRIPTION_PLAN_DAYS[plan]
    msisdn = _normalize_msisdn(phone_number)
    if not msisdn or len(msisdn) != 12:
        raise HTTPException(status_code=400, detail="Enter a valid Safaricom number, e.g. 07XXXXXXXX.")

    try:
        stk_response = await run_in_threadpool(
            _initiate_stk_push,
            msisdn,
            plan_amount,
            f"EH-{school_id}",
            "Elimu Hub Subscription",
        )
    except Exception as e:
        logger.exception("STK push failed for school %s subscription", school_id)
        return _page_shell("Payment Error", f"""
            <div class="bg-white p-6 rounded-2xl border shadow-xs space-y-3">
                <h2 class="text-lg font-black text-slate-800">Couldn't start the payment</h2>
                <p class="text-sm text-slate-500">{esc(str(e))}</p>
                <a href="/billing/subscription/{school_id}" class="text-emerald-700 text-xs font-bold">← Try again</a>
            </div>
        """)

    checkout_request_id = stk_response.get("CheckoutRequestID")
    merchant_request_id = stk_response.get("MerchantRequestID")
    if not checkout_request_id:
        return _page_shell("Payment Error", f"""
            <div class="bg-white p-6 rounded-2xl border shadow-xs space-y-3">
                <h2 class="text-lg font-black text-slate-800">Couldn't start the payment</h2>
                <p class="text-sm text-slate-500">{esc(stk_response.get('errorMessage') or stk_response.get('ResponseDescription') or 'Safaricom did not accept this request.')}</p>
                <a href="/billing/subscription/{school_id}" class="text-emerald-700 text-xs font-bold">← Try again</a>
            </div>
        """)

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO mpesa_transactions (school_id, initiated_by_user_id, purpose, reference_id, amount, phone_number, account_reference, merchant_request_id, checkout_request_id)
                VALUES (%s, %s, 'subscription', %s, %s, %s, %s, %s, %s);
            """, (school_id, viewer['id'] if viewer else None, plan_days, plan_amount, msisdn, f"EH-{school_id}", merchant_request_id, checkout_request_id))
            conn.commit()

    return RedirectResponse(url=f"/billing/status/{checkout_request_id}", status_code=303)


# ---------------------------------------------------------------------
# Staff wallet — teachers top this up via M-Pesa whenever they like;
# unlocking a scheme's print/download deducts from it instantly.
# ---------------------------------------------------------------------

def get_wallet_balance(user_id: int) -> float:
    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT balance FROM staff_wallets WHERE user_id = %s;", (user_id,))
            row = cur.fetchone()
    return float(row["balance"]) if row else 0.0


@router.get("/staff/wallet/{school_id}", response_class=HTMLResponse)
def staff_wallet_page(school_id: int, request: Request):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    viewer = get_current_session_user(request)
    if not viewer:
        raise HTTPException(status_code=401, detail="Not logged in.")

    balance = get_wallet_balance(viewer['id'])
    price = get_billing_settings()["scheme_print_amount"]

    if not BILLING_ENFORCED:
        body = f"""
            <a href="/staff/dashboard/{school_id}" class="text-slate-500 hover:text-slate-700 text-xs font-bold inline-block">← Back to Dashboard</a>
            <div class="bg-white p-6 rounded-2xl border shadow-xs space-y-4">
                <h2 class="text-lg font-black text-slate-800">My Wallet</h2>
                <div class="bg-slate-50 border border-slate-200 text-slate-600 text-xs px-4 py-3 rounded-xl">
                    💳 Scheme print/download charges aren't live yet — every scheme is free to print and download for now. Check back soon.
                </div>
            </div>
        """
        return _page_shell("My Wallet", body)

    body = f"""
        <a href="/staff/dashboard/{school_id}" class="text-slate-500 hover:text-slate-700 text-xs font-bold inline-block">← Back to Dashboard</a>
        <div class="bg-white p-6 rounded-2xl border shadow-xs space-y-4">
            <h2 class="text-lg font-black text-slate-800">My Wallet</h2>
            <div class="bg-gradient-to-br from-emerald-50 to-white border border-emerald-100 rounded-xl p-4">
                <p class="text-[10px] text-emerald-700 font-bold uppercase tracking-wide">Current Balance</p>
                <p class="text-2xl font-black text-slate-900">KES {balance:,.2f}</p>
            </div>
            <p class="text-xs text-slate-400">Each scheme of work costs KES {price:,.0f} to print/download, deducted from this balance the moment you unlock it — no extra M-Pesa prompt needed once you've topped up.</p>
            <form method="post" action="/staff/wallet/{school_id}/topup" class="space-y-3">
                <div>
                    <label class="text-xs font-bold text-slate-500">M-Pesa phone number</label>
                    <input type="tel" name="phone_number" placeholder="07XXXXXXXX" value="{esc(viewer.get('phone_number') or '')}" required class="w-full p-2.5 border rounded-lg mt-1 bg-white">
                </div>
                <div>
                    <label class="text-xs font-bold text-slate-500">Top-up amount (KES)</label>
                    <input type="number" name="amount" value="{max(price * 5, 100):.0f}" min="{price:.0f}" step="1" required class="w-full p-2.5 border rounded-lg mt-1 bg-white">
                </div>
                <button type="submit" class="w-full bg-emerald-600 hover:bg-emerald-700 text-white py-2.5 rounded-xl text-sm font-bold transition">🚀 Top Up with M-Pesa</button>
            </form>
        </div>
    """
    return _page_shell("My Wallet", body)


@router.post("/staff/wallet/{school_id}/topup")
async def staff_wallet_topup(school_id: int, request: Request, phone_number: str = Form(...), amount: float = Form(...)):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    if not BILLING_ENFORCED:
        raise HTTPException(status_code=403, detail="Wallet top-ups aren't live yet.")

    viewer = get_current_session_user(request)
    if not viewer:
        raise HTTPException(status_code=401, detail="Not logged in.")

    if amount <= 0:
        raise HTTPException(status_code=400, detail="Top-up amount must be greater than zero.")

    msisdn = _normalize_msisdn(phone_number)
    if not msisdn or len(msisdn) != 12:
        raise HTTPException(status_code=400, detail="Enter a valid Safaricom number, e.g. 07XXXXXXXX.")

    try:
        stk_response = await run_in_threadpool(
            _initiate_stk_push,
            msisdn,
            amount,
            f"EH-W{viewer['id']}",
            "Elimu Hub Wallet Top-up",
        )
    except Exception as e:
        logger.exception("STK push failed for wallet top-up, user %s", viewer['id'])
        return _page_shell("Payment Error", f"""
            <div class="bg-white p-6 rounded-2xl border shadow-xs space-y-3">
                <h2 class="text-lg font-black text-slate-800">Couldn't start the payment</h2>
                <p class="text-sm text-slate-500">{esc(str(e))}</p>
                <a href="/staff/wallet/{school_id}" class="text-emerald-700 text-xs font-bold">← Try again</a>
            </div>
        """)

    checkout_request_id = stk_response.get("CheckoutRequestID")
    merchant_request_id = stk_response.get("MerchantRequestID")
    if not checkout_request_id:
        return _page_shell("Payment Error", f"""
            <div class="bg-white p-6 rounded-2xl border shadow-xs space-y-3">
                <h2 class="text-lg font-black text-slate-800">Couldn't start the payment</h2>
                <p class="text-sm text-slate-500">{esc(stk_response.get('errorMessage') or stk_response.get('ResponseDescription') or 'Safaricom did not accept this request.')}</p>
                <a href="/staff/wallet/{school_id}" class="text-emerald-700 text-xs font-bold">← Try again</a>
            </div>
        """)

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO mpesa_transactions (school_id, initiated_by_user_id, purpose, reference_id, amount, phone_number, account_reference, merchant_request_id, checkout_request_id)
                VALUES (%s, %s, 'wallet_topup', %s, %s, %s, %s, %s, %s);
            """, (school_id, viewer['id'], viewer['id'], amount, msisdn, f"EH-W{viewer['id']}", merchant_request_id, checkout_request_id))
            conn.commit()

    return RedirectResponse(url=f"/billing/status/{checkout_request_id}", status_code=303)


# ---------------------------------------------------------------------
# Scheme of work print/download — paid individually by each teacher out
# of their wallet balance, once per scheme copy per year.
# ---------------------------------------------------------------------

@router.get("/schemes/pay/{school_id}/{copy_id}", response_class=HTMLResponse)
def scheme_print_billing_page(school_id: int, copy_id: int, request: Request):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    viewer = get_current_session_user(request)
    if not viewer:
        raise HTTPException(status_code=401, detail="Not logged in.")

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT subject_name, grade_name, term, year FROM scheme_copies WHERE id = %s AND school_id = %s;", (copy_id, school_id))
            copy = cur.fetchone()
    if not copy:
        raise HTTPException(status_code=404, detail="Scheme not found.")

    # Already paid for this scheme's year? Don't make them pay again —
    # send them straight to print.
    if has_scheme_print_unlock(copy_id, viewer['id'], copy['year']):
        return RedirectResponse(url=f"/schemes/print/{school_id}/{copy_id}", status_code=303)

    price = get_billing_settings()["scheme_print_amount"]
    balance = get_wallet_balance(viewer['id'])
    can_afford = balance >= price

    if can_afford:
        action_html = f"""
            <form method="post" action="/schemes/unlock/{school_id}/{copy_id}">
                <button type="submit" class="w-full bg-emerald-600 hover:bg-emerald-700 text-white py-2.5 rounded-xl text-sm font-bold transition">Unlock for KES {price:,.0f} from Wallet</button>
            </form>
        """
    else:
        shortfall = price - balance
        action_html = f"""
            <div class="bg-amber-50 border border-amber-200 text-amber-800 text-xs px-4 py-2.5 rounded-xl">
                Your wallet balance (KES {balance:,.2f}) isn't enough. You need KES {shortfall:,.2f} more.
            </div>
            <a href="/staff/wallet/{school_id}" class="block text-center w-full bg-emerald-600 hover:bg-emerald-700 text-white py-2.5 rounded-xl text-sm font-bold transition">Top Up Wallet</a>
        """

    body = f"""
        <a href="/schemes/edit/{school_id}/{copy_id}" class="text-slate-500 hover:text-slate-700 text-xs font-bold inline-block">← Back to Scheme</a>
        <div class="bg-white p-6 rounded-2xl border shadow-xs space-y-4">
            <h2 class="text-lg font-black text-slate-800">Unlock Printing/Download</h2>
            <p class="text-sm text-slate-500">{esc(copy['subject_name'])} — {esc(copy['grade_name'])} ({esc(copy['term'])} {copy['year']})</p>
            <p class="text-xs text-slate-400">KES {price:,.0f} for {copy['year']}. Once unlocked, you can print or download this specific scheme as many times as you like for the rest of {copy['year']} — you won't be charged again for it until next year's version is uploaded. Your wallet balance: <span class="font-bold text-slate-600">KES {balance:,.2f}</span>.</p>
            {action_html}
        </div>
    """
    return _page_shell("Unlock Scheme", body)


@router.post("/schemes/unlock/{school_id}/{copy_id}")
def scheme_print_unlock_from_wallet(school_id: int, copy_id: int, request: Request):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    viewer = get_current_session_user(request)
    if not viewer:
        raise HTTPException(status_code=401, detail="Not logged in.")

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT id, year FROM scheme_copies WHERE id = %s AND school_id = %s;", (copy_id, school_id))
            copy = cur.fetchone()
            if not copy:
                raise HTTPException(status_code=404, detail="Scheme not found.")

            if has_scheme_print_unlock(copy_id, viewer['id'], copy['year']):
                return RedirectResponse(url=f"/schemes/print/{school_id}/{copy_id}", status_code=303)

            price = get_billing_settings()["scheme_print_amount"]

            # Atomic, race-safe deduction: only succeeds if the balance is
            # still sufficient at the moment this runs, so a teacher can't
            # unlock two schemes at once with money they only have once.
            cur.execute("""
                UPDATE staff_wallets SET balance = balance - %s, updated_at = NOW()
                WHERE user_id = %s AND balance >= %s
                RETURNING balance;
            """, (price, viewer['id'], price))
            updated = cur.fetchone()

            if not updated:
                conn.commit()
                return RedirectResponse(url=f"/schemes/pay/{school_id}/{copy_id}", status_code=303)

            cur.execute("""
                INSERT INTO scheme_print_unlocks (copy_id, user_id, school_id, year)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (copy_id, user_id, year) DO NOTHING;
            """, (copy_id, viewer['id'], school_id, copy['year']))
            conn.commit()

    return RedirectResponse(url=f"/schemes/print/{school_id}/{copy_id}", status_code=303)


# ---------------------------------------------------------------------
# Shared: status polling page + Daraja callback.
# ---------------------------------------------------------------------

@router.get("/billing/status/{checkout_request_id}", response_class=HTMLResponse)
def billing_status_page(checkout_request_id: str, request: Request):
    """A small polling page — checks /api/v1/mpesa/status every 3s until
    Safaricom's callback has landed, then redirects to the right place."""
    body = f"""
        <div class="bg-white p-6 rounded-2xl border shadow-xs space-y-3 text-center" id="statusBox">
            <h2 class="text-lg font-black text-slate-800">Check your phone</h2>
            <p class="text-sm text-slate-500">Enter your M-Pesa PIN on the prompt sent to your phone to complete this payment.</p>
            <div class="text-xs text-slate-400 animate-pulse">Waiting for confirmation…</div>
        </div>
        <script>
        const checkoutId = {checkout_request_id!r};
        async function poll() {{
            try {{
                const res = await fetch(`/api/v1/mpesa/status/${{checkoutId}}`);
                const data = await res.json();
                if (data.status === 'completed') {{
                    document.getElementById('statusBox').innerHTML = '<h2 class="text-lg font-black text-emerald-700">✅ Payment received</h2><p class="text-sm text-slate-500">Redirecting…</p>';
                    setTimeout(() => {{ window.location.href = data.redirect_url || '/'; }}, 1200);
                    return;
                }}
                if (data.status === 'failed') {{
                    document.getElementById('statusBox').innerHTML = '<h2 class="text-lg font-black text-rose-700">Payment not completed</h2><p class="text-sm text-slate-500">' + (data.result_desc || 'The payment was cancelled or timed out.') + '</p>';
                    return;
                }}
                setTimeout(poll, 3000);
            }} catch (e) {{
                setTimeout(poll, 3000);
            }}
        }}
        poll();
        </script>
    """
    return _page_shell("Payment Status", body)


@router.get("/api/v1/mpesa/status/{checkout_request_id}")
def mpesa_status_check(checkout_request_id: str):
    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM mpesa_transactions WHERE checkout_request_id = %s;", (checkout_request_id,))
            txn = cur.fetchone()

    if not txn:
        return JSONResponse({"status": "pending"})

    redirect_url = None
    if txn["status"] == "completed":
        if txn["purpose"] == "subscription":
            redirect_url = f"/billing/subscription/{txn['school_id']}"
        elif txn["purpose"] == "wallet_topup":
            redirect_url = f"/staff/wallet/{txn['school_id']}"

    return JSONResponse({
        "status": txn["status"],
        "result_desc": txn["result_desc"],
        "redirect_url": redirect_url,
    })


@router.post("/api/v1/mpesa/callback")
async def mpesa_callback(request: Request):
    """Safaricom POSTs here after the customer completes or cancels the
    STK prompt. No session auth here — this call comes from Safaricom's
    servers, not a logged-in browser. Must always return 200 quickly or
    Daraja will retry."""
    try:
        payload = await request.json()
    except Exception:
        logger.warning("M-Pesa callback received non-JSON body")
        return JSONResponse({"ResultCode": 0, "ResultDesc": "Accepted"})

    stk_callback = (payload.get("Body") or {}).get("stkCallback") or {}
    checkout_request_id = stk_callback.get("CheckoutRequestID")
    result_code = stk_callback.get("ResultCode")
    result_desc = stk_callback.get("ResultDesc")

    if not checkout_request_id:
        logger.warning("M-Pesa callback missing CheckoutRequestID: %s", payload)
        return JSONResponse({"ResultCode": 0, "ResultDesc": "Accepted"})

    mpesa_receipt_number = None
    if result_code == 0:
        items = ((stk_callback.get("CallbackMetadata") or {}).get("Item")) or []
        for item in items:
            if item.get("Name") == "MpesaReceiptNumber":
                mpesa_receipt_number = item.get("Value")

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM mpesa_transactions WHERE checkout_request_id = %s;", (checkout_request_id,))
            txn = cur.fetchone()

            if not txn:
                logger.warning("M-Pesa callback for unknown CheckoutRequestID: %s", checkout_request_id)
                return JSONResponse({"ResultCode": 0, "ResultDesc": "Accepted"})

            new_status = "completed" if result_code == 0 else "failed"
            cur.execute("""
                UPDATE mpesa_transactions
                SET status = %s, result_desc = %s, mpesa_receipt_number = %s, updated_at = NOW()
                WHERE checkout_request_id = %s;
            """, (new_status, result_desc, mpesa_receipt_number, checkout_request_id))

            if new_status == "completed":
                if txn["purpose"] == "subscription":
                    days = txn["reference_id"] or 30
                    cur.execute("""
                        UPDATE schools
                        SET subscription_expires_at = GREATEST(COALESCE(subscription_expires_at, NOW()), NOW()) + (%s || ' days')::INTERVAL,
                            subscription_is_trial = FALSE
                        WHERE id = %s;
                    """, (days, txn["school_id"]))
                elif txn["purpose"] == "wallet_topup":
                    # reference_id holds the user_id for a wallet top-up.
                    cur.execute("""
                        INSERT INTO staff_wallets (user_id, school_id, balance, updated_at)
                        VALUES (%s, %s, %s, NOW())
                        ON CONFLICT (user_id) DO UPDATE SET balance = staff_wallets.balance + EXCLUDED.balance, updated_at = NOW();
                    """, (txn["reference_id"], txn["school_id"], txn["amount"]))

            conn.commit()

    return JSONResponse({"ResultCode": 0, "ResultDesc": "Accepted"})


@router.get("/superadmin/billing/mpesa-transactions", response_class=HTMLResponse)
def superadmin_mpesa_transactions(request: Request):
    """Shows the last 30 mpesa_transactions rows exactly as stored —
    this is the ground truth for what actually happened to a payment,
    since it tells us whether Safaricom's callback ever reached us at
    all (status stuck on 'pending' = callback never arrived; status
    'completed' but the wallet/subscription still looks wrong = the
    callback arrived but something after that failed)."""
    auth_error = require_superadmin_session(request)
    if auth_error:
        return auth_error

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT t.*, s.name AS school_name
                FROM mpesa_transactions t
                LEFT JOIN schools s ON t.school_id = s.id
                ORDER BY t.created_at DESC
                LIMIT 30;
            """)
            txns = cur.fetchall()

    rows = ""
    for t in txns:
        status_color = {"completed": "text-emerald-700", "failed": "text-rose-600", "pending": "text-amber-600"}.get(t["status"], "text-slate-500")
        rows += f"""
        <tr class="border-b text-xs">
            <td class="p-2">{t['id']}</td>
            <td class="p-2">{esc(t['school_name'] or '—')}</td>
            <td class="p-2">{esc(t['purpose'])}</td>
            <td class="p-2">{t['reference_id'] if t['reference_id'] is not None else '—'}</td>
            <td class="p-2">KES {t['amount']:,.0f}</td>
            <td class="p-2 font-mono">{esc(t['phone_number'])}</td>
            <td class="p-2 font-bold {status_color}">{esc(t['status'])}</td>
            <td class="p-2">{esc(t['mpesa_receipt_number'] or '—')}</td>
            <td class="p-2 text-slate-400">{esc((t['result_desc'] or '—')[:60])}</td>
            <td class="p-2 text-slate-400">{t['created_at'].strftime('%d %b %H:%M')}</td>
        </tr>
        """

    body = f"""
        <a href="/superadmin/billing/settings" class="text-slate-500 hover:text-slate-700 text-xs font-bold inline-block">← Back to Billing Settings</a>
        <div class="bg-white p-6 rounded-2xl border shadow-xs space-y-4 overflow-x-auto">
            <h2 class="text-lg font-black text-slate-800">Recent M-Pesa Transactions</h2>
            <p class="text-xs text-slate-400">If a payment shows status "pending" here even though the customer got a real M-Pesa confirmation SMS, Safaricom's callback never reached this server — almost always a wrong/unreachable MPESA_CALLBACK_URL. If it shows "completed" but a wallet/subscription still looks unpaid, the callback arrived but something in processing it failed.</p>
            <table class="w-full min-w-[900px]">
                <thead><tr class="border-b-2 text-xs text-left"><th class="p-2">ID</th><th class="p-2">School</th><th class="p-2">Purpose</th><th class="p-2">Ref</th><th class="p-2">Amount</th><th class="p-2">Phone</th><th class="p-2">Status</th><th class="p-2">Receipt</th><th class="p-2">Result</th><th class="p-2">Created</th></tr></thead>
                <tbody>{rows if rows else '<tr><td colspan="10" class="p-4 text-center text-slate-400">No transactions yet.</td></tr>'}</tbody>
            </table>
        </div>
    """
    return _page_shell("M-Pesa Transactions", body)


@router.get("/superadmin/billing/mpesa-diagnostic", response_class=HTMLResponse)
def superadmin_mpesa_diagnostic(request: Request):
    """Shows whether the RUNNING process actually sees each MPESA_*
    environment variable as set — never the values themselves. This
    exists because 'I set it on Render' and 'this process can see it'
    are two different claims, and the only way to tell them apart for
    sure is to ask the process directly rather than guess from
    screenshots of the dashboard."""
    auth_error = require_superadmin_session(request)
    if auth_error:
        return auth_error

    # These aren't secrets — a public URL and a public test shortcode —
    # so show them in full rather than masked, since seeing the exact
    # value is the whole point of checking a callback delivery failure.
    NON_SENSITIVE = {"MPESA_ENV", "MPESA_SHORTCODE", "MPESA_CALLBACK_URL"}

    checks = [
        ("MPESA_ENV", MPESA_ENV, "sandbox' or 'production"),
        ("MPESA_CONSUMER_KEY", MPESA_CONSUMER_KEY, None),
        ("MPESA_CONSUMER_SECRET", MPESA_CONSUMER_SECRET, None),
        ("MPESA_SHORTCODE", MPESA_SHORTCODE, "174379 in sandbox"),
        ("MPESA_PASSKEY", MPESA_PASSKEY, None),
        ("MPESA_CALLBACK_URL", MPESA_CALLBACK_URL, None),
    ]

    rows = ""
    for name, value, hint in checks:
        is_set = bool(value)
        status = f"<span class='text-emerald-700 font-bold'>✅ Set ({len(value)} characters)</span>" if is_set else "<span class='text-rose-600 font-bold'>❌ Not set / empty</span>"
        if not is_set:
            preview = ""
        elif name in NON_SENSITIVE:
            preview = f"<span class='text-slate-600 text-xs font-mono'>{esc(value)}</span>"
        elif len(value) > 4:
            preview = f"<span class='text-slate-400 text-xs'>starts with: {esc(value[:4])}…</span>"
        else:
            preview = ""
        rows += f"""
        <tr class="border-b">
            <td class="p-3 font-mono text-xs font-bold">{name}</td>
            <td class="p-3">{status}</td>
            <td class="p-3">{preview}</td>
        </tr>
        """

    body = f"""
        <a href="/superadmin/billing/settings" class="text-slate-500 hover:text-slate-700 text-xs font-bold inline-block">← Back to Billing Settings</a>
        <div class="bg-white p-6 rounded-2xl border shadow-xs space-y-4">
            <h2 class="text-lg font-black text-slate-800">M-Pesa Environment Variable Check</h2>
            <p class="text-xs text-slate-400">This checks what the CURRENTLY RUNNING process actually sees — not what's saved on Render's dashboard. If a variable shows "Not set" here after you've saved it and redeployed, the name almost certainly doesn't match exactly (case, spelling, a stray space) between what's on Render and what the code reads.</p>
            <table class="w-full text-sm">
                <thead><tr class="border-b-2"><th class="p-3 text-left">Variable</th><th class="p-3 text-left">Status</th><th class="p-3 text-left"></th></tr></thead>
                <tbody>{rows}</tbody>
            </table>
            <a href="/superadmin/billing/mpesa-transactions" class="block text-center w-full bg-indigo-600 hover:bg-indigo-700 text-white text-xs py-2.5 rounded-xl font-semibold transition">📋 View Recent Transactions</a>
        </div>
    """
    return _page_shell("M-Pesa Diagnostic", body)


@router.get("/superadmin/billing/settings", response_class=HTMLResponse)
def superadmin_billing_settings_page(request: Request, saved: str = None):
    auth_error = require_superadmin_session(request)
    if auth_error:
        return auth_error

    settings = get_billing_settings()

    # Revenue actually collected (completed transactions only — pending
    # or failed ones never moved real money). Subscriptions and staff
    # wallet top-ups are the only two channels money ever comes in
    # through; 'scheme_print' is a legacy purpose from before the wallet
    # model and is included in the staff total for historical accuracy,
    # in case any old direct per-scheme payments exist.
    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT purpose, COUNT(*) AS cnt, COALESCE(SUM(amount), 0) AS total
                FROM mpesa_transactions
                WHERE status = 'completed'
                GROUP BY purpose;
            """)
            by_purpose = {r['purpose']: r for r in cur.fetchall()}

    subscription_total = float(by_purpose.get('subscription', {}).get('total', 0) or 0)
    subscription_count = by_purpose.get('subscription', {}).get('cnt', 0) or 0
    staff_total = float(by_purpose.get('wallet_topup', {}).get('total', 0) or 0) + float(by_purpose.get('scheme_print', {}).get('total', 0) or 0)
    staff_count = (by_purpose.get('wallet_topup', {}).get('cnt', 0) or 0) + (by_purpose.get('scheme_print', {}).get('cnt', 0) or 0)

    revenue_cards = f"""
        <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <div class="bg-gradient-to-br from-indigo-50 to-white border border-indigo-100 rounded-xl p-4">
                <p class="text-[10px] text-indigo-700 font-bold uppercase tracking-wide">Collected from Schools (Subscriptions)</p>
                <p class="text-2xl font-black text-slate-900">KES {subscription_total:,.0f}</p>
                <p class="text-xs text-slate-400">{subscription_count} completed payment(s)</p>
            </div>
            <div class="bg-gradient-to-br from-emerald-50 to-white border border-emerald-100 rounded-xl p-4">
                <p class="text-[10px] text-emerald-700 font-bold uppercase tracking-wide">Collected from Staff (Wallet Top-ups)</p>
                <p class="text-2xl font-black text-slate-900">KES {staff_total:,.0f}</p>
                <p class="text-xs text-slate-400">{staff_count} completed payment(s)</p>
            </div>
        </div>
    """

    body = f"""
        <a href="/superadmin/dashboard" class="text-slate-500 hover:text-slate-700 text-xs font-bold inline-block">← Back to Super Admin Portal</a>
        <div class="bg-white p-6 rounded-2xl border shadow-xs space-y-4">
            <h2 class="text-lg font-black text-slate-800">Billing Settings</h2>
            {revenue_cards}
            <p class="text-xs text-slate-400">These are the only place prices are set in the whole system — every school subscription payment and every teacher's scheme print/download payment uses these amounts.</p>
            <a href="/superadmin/billing/mpesa-diagnostic" class="text-xs font-bold text-indigo-700 hover:text-indigo-900 inline-block">🔍 Check M-Pesa environment variables</a>
            {"<div class='bg-emerald-50 border border-emerald-200 text-emerald-800 text-xs px-4 py-2 rounded-xl'>✅ Saved.</div>" if saved else ""}
            <form method="post" action="/superadmin/billing/settings" class="space-y-4">
                <div>
                    <label class="text-xs font-bold text-slate-500">School subscription — Per Term (KES)</label>
                    <input type="number" step="1" min="0" name="subscription_termly_amount" value="{settings['subscription_termly_amount']:.0f}" required class="w-full p-2.5 border rounded-lg mt-1 bg-white">
                </div>
                <div>
                    <label class="text-xs font-bold text-slate-500">School subscription — Per Year (KES)</label>
                    <input type="number" step="1" min="0" name="subscription_yearly_amount" value="{settings['subscription_yearly_amount']:.0f}" required class="w-full p-2.5 border rounded-lg mt-1 bg-white">
                </div>
                <div>
                    <label class="text-xs font-bold text-slate-500">Scheme of work print/download — per teacher, per scheme, per year (KES)</label>
                    <input type="number" step="1" min="0" name="scheme_print_amount" value="{settings['scheme_print_amount']:.0f}" required class="w-full p-2.5 border rounded-lg mt-1 bg-white">
                </div>
                <button type="submit" class="w-full bg-emerald-600 hover:bg-emerald-700 text-white py-2.5 rounded-xl text-sm font-bold transition">Save Prices</button>
            </form>
        </div>
    """
    return _page_shell("Billing Settings", body)


@router.post("/superadmin/billing/settings")
def superadmin_billing_settings_save(
    request: Request,
    subscription_termly_amount: float = Form(...),
    subscription_yearly_amount: float = Form(...),
    scheme_print_amount: float = Form(...),
):
    auth_error = require_superadmin_session(request)
    if auth_error:
        return auth_error

    for label, value in [
        ("Per Term subscription", subscription_termly_amount),
        ("Per Year subscription", subscription_yearly_amount),
        ("Scheme print/download", scheme_print_amount),
    ]:
        if value < 0:
            raise HTTPException(status_code=400, detail=f"{label} price cannot be negative.")

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE billing_settings
                SET subscription_termly_amount = %s,
                    subscription_yearly_amount = %s,
                    scheme_print_amount = %s,
                    updated_at = NOW()
                WHERE id = 1;
            """, (subscription_termly_amount, subscription_yearly_amount, scheme_print_amount))
            conn.commit()

    return RedirectResponse(url="/superadmin/billing/settings?saved=1", status_code=303)


def is_school_subscription_active(school_id: int) -> bool:
    """Call this from anywhere you need to gate a feature behind an
    active school subscription."""
    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT subscription_expires_at FROM schools WHERE id = %s;", (school_id,))
            row = cur.fetchone()
    return bool(row and row["subscription_expires_at"] and row["subscription_expires_at"] > datetime.datetime.now())


def subscription_status_label(school: dict) -> tuple:
    """Given a school row (must include subscription_expires_at and
    subscription_is_trial — both present on any 'SELECT * FROM schools'
    row), returns (label_text, color_class) for consistent display
    across the admin header badge, sidebar widget, and super admin
    table. A free trial and a real paid subscription both unlock the
    exact same features (is_school_subscription_active doesn't
    distinguish them at all) — this label exists purely so an admin
    understands *why* they currently have access, since a trial ending
    soon needs a different next action (pay) than an active paid period
    does (nothing)."""
    expires_at = school.get('subscription_expires_at')
    is_trial = school.get('subscription_is_trial')
    if expires_at and expires_at > datetime.datetime.now():
        if is_trial:
            return (f"🎓 Free Trial — ends {expires_at.strftime('%d %b %Y')}", "bg-gradient-to-r from-amber-500 to-amber-600")
        return (f"✅ Active until {expires_at.strftime('%d %b %Y')}", "bg-gradient-to-r from-emerald-500 to-emerald-600")
    return ("⚠️ No active subscription", "bg-gradient-to-r from-rose-500 to-rose-600")


def render_admin_print_toolbar_and_content(school_id: int, document_content_html: str, doc_label: str, button_color: str = "#4f46e5", max_height_px: int = 480):
    """Every printable admin-portal document (rosters, merit lists, top
    10 lists, grade distribution, subject analysis, class statements,
    receipts, bulk report card batches) routes its toolbar + content
    through this one function, so the "requires a subscription to
    print" behavior only has to be changed in one place rather than
    across nine separate route bodies.

    Returns (toolbar_button_html, content_html, extra_style_html):
      - toolbar_button_html: goes inside the existing .no-print div —
        the real print button when subscribed, a "Subscribe to Print"
        link instead when not.
      - content_html: the actual document body — unchanged when
        subscribed; when not, wrapped in a height-limited, overflow-
        hidden container (default max_height_px=480, adjustable per
        call — a dense multi-page batch like report cards can pass a
        taller value so the preview shows a meaningful chunk rather
        than a sliver of the first page) with a "Subscribe to unlock"
        card underneath. This wrapping never parses or splits the
        original HTML, so it's safe regardless of the document's
        internal structure.
      - extra_style_html: goes in <head> — empty when subscribed; when
        not, a print-stylesheet rule that blanks the entire physical
        printout except a lock message, so bypassing the disabled
        button with Ctrl+P still doesn't produce the real document on
        paper or as a browser-native PDF.

    Honest limitation: this is a strong deterrent for the ordinary
    "click print" / "Ctrl+P" path, not a cryptographic guarantee —
    someone determined enough with browser dev tools could still strip
    this out of the page's own HTML/CSS. There's no real PDF file or
    server-side PDF library involved anywhere in this codebase; every
    one of these pages has always been a browser-rendered HTML page
    that "Save as PDF" captures via the browser's own print dialog."""
    if is_school_subscription_active(school_id):
        toolbar_button = f'<button onclick="window.print()" style="background:{button_color};color:white;border:none;padding:10px 18px;border-radius:8px;font-weight:bold;cursor:pointer;">🖨 Print / Save as PDF</button><p style="font-size:10px;color:#94a3b8;margin:6px 0 0;">Tip: in the print dialog, choose "Save as PDF" as the destination to download a file instead of printing on paper.</p>'
        return toolbar_button, document_content_html, ""

    toolbar_button = f'<a href="/billing/subscription/{school_id}" style="background:#f59e0b;color:white;padding:10px 18px;border-radius:8px;font-weight:bold;text-decoration:none;display:inline-block;">🔒 Subscribe to Print</a><p style="font-size:10px;color:#94a3b8;margin:6px 0 0;">This school\'s Elimu Hub subscription is inactive, so printing/saving and part of this document are locked.</p>'

    extra_style_html = """
    <style>
        @media print {
            body * { visibility: hidden !important; }
            .eh-print-lock, .eh-print-lock * { visibility: visible !important; }
            .eh-print-lock {
                position: fixed !important; top: 0; left: 0; width: 100%; height: 100%;
                display: flex !important; align-items: center; justify-content: center;
                background: white !important; z-index: 999999;
            }
        }
        .eh-print-lock { display: none; }
    </style>
    """

    print_lock_div = f"""
    <div class="eh-print-lock">
        <div style="text-align:center; font-family: sans-serif; padding: 40px;">
            <p style="font-size:26px; font-weight:900; margin:0 0 12px;">🔒 Subscription Required</p>
            <p style="font-size:14px; color:#475569; margin:0;">This school does not have an active Elimu Hub subscription.<br>Visit /billing/subscription/{school_id} to unlock printing.</p>
        </div>
    </div>
    """

    content_html = f"""
    <div style="position:relative; max-height:{max_height_px}px; overflow:hidden;">
        {document_content_html}
        <div style="position:absolute; bottom:0; left:0; right:0; height:240px; background:linear-gradient(to bottom, rgba(255,255,255,0), white 65%); pointer-events:none;"></div>
    </div>
    <div style="text-align:center; margin-top:16px; padding:22px; background:#fffbeb; border:2px dashed #f59e0b; border-radius:16px;">
        <p style="font-weight:900; font-size:16px; margin:0 0 8px; color:#92400e;">🔒 Subscribe to unlock the rest of this {esc(doc_label)}</p>
        <p style="font-size:12px; color:#94a3b8; margin:0 0 14px;">Your school doesn't currently have an active Elimu Hub subscription, so part of this document is hidden and printing/saving is disabled.</p>
        <a href="/billing/subscription/{school_id}" style="background:#f59e0b;color:white;padding:10px 22px;border-radius:10px;font-weight:900;text-decoration:none;display:inline-block;">Subscribe Now</a>
    </div>
    {print_lock_div}
    """

    return toolbar_button, content_html, extra_style_html


def has_scheme_print_unlock(copy_id: int, user_id: int, year: int) -> bool:
    """Call this before letting a teacher print/download a scheme copy.
    Scoped to the scheme's own year — paying for Grade 4 Term 2 Maths this
    year never carries over to a future year's re-upload of that same
    subject/grade, since that re-upload is a new scheme_copies row with
    its own id and year."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM scheme_print_unlocks WHERE copy_id = %s AND user_id = %s AND year = %s;", (copy_id, user_id, year))
            return cur.fetchone() is not None
