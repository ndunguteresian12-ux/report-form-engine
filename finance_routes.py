"""
finance_routes.py — the student fees module, extracted out of main.py,
following the exact same pattern as timetable_routes.py.

Owns its own two tables (fee_structures, fee_payments) and every
/finance* and /api/v1/finance* route. main.py just does:

    from finance_routes import router as finance_router, bootstrap_finance_schema
    bootstrap_finance_schema()
    app.include_router(finance_router)

All shared plumbing (DB pool, auth checks, esc, full names) comes from
shared.py rather than from main.py, to avoid a circular import — same
reasoning as timetable_routes.py.

Payments are recorded manually by an admin/staff (cash, bank, M-Pesa
reference typed in, or other) — there is no live payment gateway here.
A student's balance for a given term is simply:
    fee_structures.amount (for their grade + term + year)
    minus SUM(fee_payments.amount) for that student + term + year.
"""

import os
import urllib.parse
from datetime import datetime
from fastapi import APIRouter, Request, HTTPException, Form
from fastapi.responses import HTMLResponse, RedirectResponse

from shared import (
    esc,
    get_db_connection,
    RealDictCursor,
    require_school_session,
    require_admin_session,
    get_dashboard_url,
    full_student_name,
)

router = APIRouter()

# Off by default — the module is fully built and deployed, but every route
# shows a friendly "not yet available" page instead of doing anything,
# until this is explicitly turned on. Flip it by setting the environment
# variable FINANCE_MODULE_ENABLED=true in Render — no code change or
# redeploy needed to activate it later.
FINANCE_MODULE_ENABLED = (os.getenv("FINANCE_MODULE_ENABLED", "false").strip().lower() == "true")

def _coming_soon_page(school_id: int, request: Request) -> HTMLResponse:
    return HTMLResponse(f"""
    <!DOCTYPE html>
    <html>
    <head><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Elimu Hub | Finance — Coming Soon</title><script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script></head>
    <body class="bg-[#F7F9F8] min-h-screen flex items-center justify-center p-4">
        <div class="bg-white p-8 rounded-2xl border shadow-md max-w-md w-full text-center">
            <p class="text-4xl mb-3">💰</p>
            <h1 class="text-lg font-black text-slate-800 mb-2">Finance — Coming Soon</h1>
            <p class="text-sm text-slate-500 mb-6">The Finance module is still being finished and isn't available yet. We'll let you know as soon as it's ready.</p>
            <a href="{get_dashboard_url(request, school_id)}" class="bg-indigo-800 hover:bg-indigo-900 text-white font-bold py-2.5 px-6 rounded-xl text-sm transition inline-block">← Back to Dashboard</a>
        </div>
    </body>
    </html>
    """)


def bootstrap_finance_schema():
    """Creates this module's tables if they don't exist yet. Purely
    additive — never touches students, classes, or any other existing
    table, so it can't affect or lose any live school's existing data."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS fee_structures (
                    id SERIAL PRIMARY KEY,
                    school_id INTEGER REFERENCES schools(id) ON DELETE CASCADE,
                    grade_name VARCHAR(100) NOT NULL,
                    education_level VARCHAR(100) NOT NULL,
                    term VARCHAR(20) NOT NULL,
                    year INTEGER NOT NULL,
                    amount NUMERIC(10, 2) NOT NULL,
                    UNIQUE(school_id, grade_name, term, year)
                );

                CREATE TABLE IF NOT EXISTS fee_payments (
                    id SERIAL PRIMARY KEY,
                    student_id INTEGER REFERENCES students(id) ON DELETE CASCADE,
                    school_id INTEGER REFERENCES schools(id) ON DELETE CASCADE,
                    amount NUMERIC(10, 2) NOT NULL,
                    payment_method VARCHAR(30) NOT NULL DEFAULT 'cash',
                    reference_note VARCHAR(255),
                    term VARCHAR(20) NOT NULL,
                    year INTEGER NOT NULL,
                    recorded_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    paid_at TIMESTAMP DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_fee_payments_student_term ON fee_payments (student_id, term, year);
            """)
            conn.commit()


def _get_school_and_settings(cur, school_id):
    cur.execute("SELECT * FROM schools WHERE id = %s;", (school_id,))
    school = cur.fetchone()
    cur.execute("SELECT active_term, active_year FROM school_settings WHERE school_id = %s;", (school_id,))
    settings = cur.fetchone()
    active_term = settings['active_term'] if settings else "Term 1"
    active_year = settings['active_year'] if settings else datetime.now().year
    return school, active_term, active_year


@router.get("/finance/dashboard/{school_id}", response_class=HTMLResponse)
def finance_dashboard(school_id: int, request: Request, term: str = None, year: int = None):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error
    if not FINANCE_MODULE_ENABLED:
        return _coming_soon_page(school_id, request)

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            school, active_term, active_year = _get_school_and_settings(cur, school_id)
            if not school:
                raise HTTPException(status_code=404, detail="School not found.")
            term = term or active_term
            year = year or active_year

            # One aggregate query per side (expected vs collected) rather
            # than looping per student — stays fast even for a large school.
            cur.execute("""
                SELECT c.grade_name, c.education_level, s.stream,
                       COUNT(DISTINCT s.id) AS student_count,
                       COALESCE(fs.amount, 0) AS fee_amount
                FROM students s
                JOIN classes c ON s.class_id = c.id
                LEFT JOIN fee_structures fs ON fs.school_id = s.school_id AND fs.grade_name = c.grade_name AND fs.term = %s AND fs.year = %s
                WHERE s.school_id = %s AND (s.status IS NULL OR s.status != 'GRADUATED')
                GROUP BY c.grade_name, c.education_level, s.stream, fs.amount
                ORDER BY c.grade_name, s.stream;
            """, (term, year, school_id))
            class_rows = cur.fetchall()

            cur.execute("""
                SELECT c.grade_name, COALESCE(SUM(fp.amount), 0) AS collected
                FROM fee_payments fp
                JOIN students s ON fp.student_id = s.id
                JOIN classes c ON s.class_id = c.id
                WHERE fp.school_id = %s AND fp.term = %s AND fp.year = %s
                GROUP BY c.grade_name;
            """, (school_id, term, year))
            collected_by_grade = {r['grade_name']: float(r['collected']) for r in cur.fetchall()}

    total_expected = sum(float(r['fee_amount']) * r['student_count'] for r in class_rows)
    total_collected = sum(collected_by_grade.values())
    total_outstanding = max(0, total_expected - total_collected)

    class_rows_html = ""
    for r in class_rows:
        expected = float(r['fee_amount']) * r['student_count']
        collected = collected_by_grade.get(r['grade_name'], 0)
        # Rough per-class share when multiple streams share a grade's total —
        # good enough for an at-a-glance dashboard; the real per-student
        # figures live on the class fee list page.
        section_label = r['grade_name'] if (not r['stream'] or r['stream'] == 'SINGLE STREAM') else f"{r['grade_name']} — {r['stream']}"
        encoded_grade = urllib.parse.quote(r['grade_name'])
        encoded_stream = urllib.parse.quote(r['stream'] or 'SINGLE STREAM')
        encoded_level = urllib.parse.quote(r['education_level'])
        class_rows_html += f"""
        <tr class="border-b border-slate-50">
            <td class="p-3 font-semibold text-slate-700">{esc(section_label)}</td>
            <td class="p-3 text-center">{r['student_count']}</td>
            <td class="p-3 text-right">KSh {expected:,.0f}</td>
            <td class="p-3 text-right text-emerald-700 font-semibold">KSh {collected:,.0f}</td>
            <td class="p-3 text-right">
                <a href="/finance/class/{school_id}?grade_name={encoded_grade}&stream={encoded_stream}&education_level={encoded_level}&term={urllib.parse.quote(term)}&year={year}" class="text-indigo-700 hover:underline text-xs font-bold">View →</a>
            </td>
        </tr>
        """

    return f"""
    <!DOCTYPE html>
    <html>
    <head><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Elimu Hub | Finance</title><script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script></head>
    <body class="bg-[#F7F9F8] min-h-screen">
        <header class="bg-white border-b px-6 sm:px-8 py-4 flex flex-col sm:flex-row sm:justify-between sm:items-center gap-2">
            <div>
                <h1 class="text-base font-bold text-slate-900">💰 Finance — {esc(school['name'])}</h1>
                <p class="text-xs text-slate-400">{esc(term)} {year} — student fees are tracked and recorded manually.</p>
            </div>
            <div class="flex items-center gap-2">
                <a href="/finance/fee-structure/{school_id}?term={urllib.parse.quote(term)}&year={year}" class="bg-white hover:bg-slate-50 text-slate-600 border border-slate-200 px-4 py-2 rounded-xl text-xs font-bold transition">⚙️ Fee Structure</a>
                <a href="{get_dashboard_url(request, school_id)}" class="bg-slate-800 hover:bg-slate-900 text-white px-4 py-2 rounded-xl text-xs font-bold transition">← Back to Dashboard</a>
            </div>
        </header>

        <div class="p-4 sm:p-8 max-w-5xl mx-auto space-y-6">
            <div class="grid grid-cols-1 sm:grid-cols-3 gap-4">
                <div class="bg-white rounded-2xl border shadow-xs p-5 border-l-4" style="border-left-color:#4f46e5;">
                    <p class="text-[11px] font-bold uppercase tracking-wider text-slate-400">Expected This Term</p>
                    <p class="text-2xl font-black text-slate-900 mt-1">KSh {total_expected:,.0f}</p>
                </div>
                <div class="bg-white rounded-2xl border shadow-xs p-5 border-l-4" style="border-left-color:#059669;">
                    <p class="text-[11px] font-bold uppercase tracking-wider text-slate-400">Collected</p>
                    <p class="text-2xl font-black text-slate-900 mt-1">KSh {total_collected:,.0f}</p>
                </div>
                <div class="bg-white rounded-2xl border shadow-xs p-5 border-l-4" style="border-left-color:#dc2626;">
                    <p class="text-[11px] font-bold uppercase tracking-wider text-slate-400">Outstanding</p>
                    <p class="text-2xl font-black text-slate-900 mt-1">KSh {total_outstanding:,.0f}</p>
                </div>
            </div>

            <div class="bg-white rounded-2xl border shadow-xs overflow-hidden">
                <div class="px-5 py-3 border-b bg-slate-50/60">
                    <h2 class="text-sm font-bold text-slate-800">Fee Status by Class</h2>
                </div>
                <table class="w-full text-sm">
                    <thead><tr class="bg-slate-50 text-slate-500 text-xs border-b"><th class="p-3 text-left">Class</th><th class="p-3 text-center">Students</th><th class="p-3 text-right">Expected</th><th class="p-3 text-right">Collected</th><th class="p-3"></th></tr></thead>
                    <tbody>{class_rows_html or "<tr><td colspan='5' class='p-8 text-center text-slate-400 text-xs italic'>No classes with students yet.</td></tr>"}</tbody>
                </table>
            </div>

            {"<div class='bg-amber-50 border border-amber-200 text-amber-800 text-xs px-4 py-3 rounded-xl'>⚠️ No fee amounts have been set for this term yet — <a href='/finance/fee-structure/" + str(school_id) + "?term=" + urllib.parse.quote(term) + "&year=" + str(year) + "' class='underline font-bold'>set up your Fee Structure</a> first so balances can be calculated.</div>" if total_expected == 0 and class_rows else ""}
        </div>
    </body>
    </html>
    """


@router.get("/finance/fee-structure/{school_id}", response_class=HTMLResponse)
def fee_structure_view(school_id: int, request: Request, term: str = None, year: int = None):
    auth_error = require_admin_session(request, school_id)
    if auth_error:
        return auth_error
    if not FINANCE_MODULE_ENABLED:
        return _coming_soon_page(school_id, request)

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            school, active_term, active_year = _get_school_and_settings(cur, school_id)
            if not school:
                raise HTTPException(status_code=404, detail="School not found.")
            term = term or active_term
            year = year or active_year

            cur.execute("SELECT DISTINCT grade_name, education_level FROM classes ORDER BY id ASC;")
            all_grades = cur.fetchall()

            cur.execute("SELECT grade_name, amount FROM fee_structures WHERE school_id = %s AND term = %s AND year = %s;", (school_id, term, year))
            existing = {r['grade_name']: float(r['amount']) for r in cur.fetchall()}

    rows_html = "".join(f"""
        <div class="flex items-center justify-between gap-3 py-2.5 border-b border-slate-50 last:border-0">
            <span class="text-sm font-semibold text-slate-700">{esc(g['grade_name'])} <span class="text-slate-400 font-normal">({esc(g['education_level'])})</span></span>
            <div class="flex items-center gap-1.5">
                <span class="text-slate-400 text-sm">KSh</span>
                <input type="number" name="amount_{esc(g['grade_name'])}" value="{existing.get(g['grade_name'], '')}" min="0" step="0.01" placeholder="0" class="border p-2 rounded-lg w-32 text-right text-sm">
            </div>
        </div>
    """ for g in all_grades)

    return f"""
    <!DOCTYPE html>
    <html>
    <head><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Elimu Hub | Fee Structure</title><script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script></head>
    <body class="bg-[#F7F9F8] min-h-screen p-4 sm:p-8">
        <div class="max-w-xl mx-auto space-y-4">
            <div class="bg-white p-6 rounded-2xl border shadow-xs">
                <h2 class="text-lg font-black text-slate-800">⚙️ Fee Structure — {esc(term)} {year}</h2>
                <p class="text-xs text-slate-400 mt-1">Set the expected fee amount per grade for this term. Leave blank for grades that don't charge fees.</p>
            </div>
            <form action="/api/v1/finance/fee-structure/save/{school_id}" method="post" class="bg-white p-6 rounded-2xl border shadow-xs">
                <input type="hidden" name="term" value="{esc(term)}">
                <input type="hidden" name="year" value="{year}">
                {rows_html or "<p class='text-slate-400 text-xs italic py-4'>No classes set up yet.</p>"}
                <button type="submit" class="w-full mt-4 bg-indigo-800 hover:bg-indigo-900 text-white font-bold py-3 rounded-xl text-sm transition">Save Fee Structure</button>
            </form>
            <a href="/finance/dashboard/{school_id}" class="bg-slate-200 hover:bg-slate-300 text-slate-700 font-bold py-2.5 px-5 rounded-xl text-sm transition inline-block">← Back to Finance</a>
        </div>
    </body>
    </html>
    """


@router.post("/api/v1/finance/fee-structure/save/{school_id}")
async def save_fee_structure(school_id: int, request: Request):
    auth_error = require_admin_session(request, school_id)
    if auth_error:
        return auth_error
    if not FINANCE_MODULE_ENABLED:
        return _coming_soon_page(school_id, request)

    form_data = await request.form()
    term = (form_data.get("term") or "").strip()
    try:
        year = int(form_data.get("year"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid year.")
    if not term:
        raise HTTPException(status_code=400, detail="Term is required.")

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT DISTINCT grade_name, education_level FROM classes ORDER BY id ASC;")
            all_grades = cur.fetchall()

            for g in all_grades:
                field_name = f"amount_{g['grade_name']}"
                raw_val = form_data.get(field_name, "")
                if str(raw_val).strip() == "":
                    continue
                try:
                    amount = float(raw_val)
                except ValueError:
                    continue
                if amount < 0:
                    continue
                cur.execute("""
                    INSERT INTO fee_structures (school_id, grade_name, education_level, term, year, amount)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (school_id, grade_name, term, year) DO UPDATE SET amount = EXCLUDED.amount;
                """, (school_id, g['grade_name'], g['education_level'], term, year, amount))
            conn.commit()

    return RedirectResponse(url=f"/finance/dashboard/{school_id}?term={urllib.parse.quote(term)}&year={year}", status_code=303)


@router.get("/finance/class/{school_id}", response_class=HTMLResponse)
def finance_class_list(school_id: int, request: Request, grade_name: str, education_level: str, stream: str, term: str = None, year: int = None):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error
    if not FINANCE_MODULE_ENABLED:
        return _coming_soon_page(school_id, request)

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            school, active_term, active_year = _get_school_and_settings(cur, school_id)
            term = term or active_term
            year = year or active_year

            cur.execute("""
                SELECT amount FROM fee_structures
                WHERE school_id = %s AND grade_name = %s AND term = %s AND year = %s;
            """, (school_id, grade_name, term, year))
            fee_row = cur.fetchone()
            fee_amount = float(fee_row['amount']) if fee_row else 0

            cur.execute("""
                SELECT s.id, s.first_name, s.middle_name, s.last_name, s.admission_number,
                       COALESCE((SELECT SUM(fp.amount) FROM fee_payments fp WHERE fp.student_id = s.id AND fp.term = %s AND fp.year = %s), 0) AS paid
                FROM students s
                JOIN classes c ON s.class_id = c.id
                WHERE s.school_id = %s AND c.grade_name = %s AND c.education_level = %s AND s.stream = %s
                  AND (s.status IS NULL OR s.status != 'GRADUATED')
                ORDER BY s.admission_number ASC;
            """, (term, year, school_id, grade_name, education_level, stream))
            students = cur.fetchall()

    section_label = grade_name if stream == 'SINGLE STREAM' else f"{grade_name} — {stream}"

    rows_html = ""
    for s in students:
        paid = float(s['paid'])
        balance = fee_amount - paid
        status_badge = (
            "<span class='text-[10px] font-bold px-2 py-0.5 rounded-full bg-emerald-50 text-emerald-700 border border-emerald-200'>Paid in full</span>" if balance <= 0 and fee_amount > 0 else
            "<span class='text-[10px] font-bold px-2 py-0.5 rounded-full bg-amber-50 text-amber-700 border border-amber-200'>Balance owing</span>" if balance > 0 else
            "<span class='text-[10px] font-bold px-2 py-0.5 rounded-full bg-slate-100 text-slate-500 border border-slate-200'>No fee set</span>"
        )
        rows_html += f"""
        <tr class="border-b border-slate-50">
            <td class="p-3 font-mono text-xs text-slate-400">{esc(s['admission_number'])}</td>
            <td class="p-3 font-semibold text-slate-700">{esc(full_student_name(s))}</td>
            <td class="p-3 text-right">KSh {paid:,.0f}</td>
            <td class="p-3 text-right font-bold {'text-rose-700' if balance > 0 else 'text-emerald-700'}">KSh {balance:,.0f}</td>
            <td class="p-3 text-center">{status_badge}</td>
            <td class="p-3 text-right"><a href="/finance/student/{school_id}/{s['id']}?term={urllib.parse.quote(term)}&year={year}" class="text-indigo-700 hover:underline text-xs font-bold">Statement →</a></td>
        </tr>
        """

    return f"""
    <!DOCTYPE html>
    <html>
    <head><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Elimu Hub | Fees — {esc(section_label)}</title><script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script></head>
    <body class="bg-[#F7F9F8] min-h-screen">
        <header class="bg-white border-b px-6 sm:px-8 py-4 flex flex-col sm:flex-row sm:justify-between sm:items-center gap-2">
            <div>
                <h1 class="text-base font-bold text-slate-900">💰 {esc(section_label)} — Fees</h1>
                <p class="text-xs text-slate-400">{esc(term)} {year} — fee amount: KSh {fee_amount:,.0f} per student</p>
            </div>
            <a href="/finance/dashboard/{school_id}" class="bg-slate-800 hover:bg-slate-900 text-white px-4 py-2 rounded-xl text-xs font-bold transition">← Back to Finance</a>
        </header>
        <div class="p-4 sm:p-8 max-w-4xl mx-auto">
            <div class="bg-white rounded-2xl border shadow-xs overflow-hidden">
                <table class="w-full text-sm">
                    <thead><tr class="bg-slate-50 text-slate-500 text-xs border-b"><th class="p-3 text-left">Adm No.</th><th class="p-3 text-left">Name</th><th class="p-3 text-right">Paid</th><th class="p-3 text-right">Balance</th><th class="p-3 text-center">Status</th><th class="p-3"></th></tr></thead>
                    <tbody>{rows_html or "<tr><td colspan='6' class='p-8 text-center text-slate-400 text-xs italic'>No students in this class.</td></tr>"}</tbody>
                </table>
            </div>
        </div>
    </body>
    </html>
    """


@router.get("/finance/student/{school_id}/{student_id}", response_class=HTMLResponse)
def student_fee_statement(school_id: int, student_id: int, request: Request, term: str = None, year: int = None, saved: str = None):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error
    if not FINANCE_MODULE_ENABLED:
        return _coming_soon_page(school_id, request)

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            school, active_term, active_year = _get_school_and_settings(cur, school_id)
            term = term or active_term
            year = year or active_year

            cur.execute("""
                SELECT s.*, c.grade_name FROM students s JOIN classes c ON s.class_id = c.id
                WHERE s.id = %s AND s.school_id = %s;
            """, (student_id, school_id))
            student = cur.fetchone()
            if not student:
                raise HTTPException(status_code=404, detail="Student not found.")

            cur.execute("""
                SELECT amount FROM fee_structures
                WHERE school_id = %s AND grade_name = %s AND term = %s AND year = %s;
            """, (school_id, student['grade_name'], term, year))
            fee_row = cur.fetchone()
            fee_amount = float(fee_row['amount']) if fee_row else 0

            cur.execute("""
                SELECT * FROM fee_payments WHERE student_id = %s AND term = %s AND year = %s ORDER BY paid_at DESC;
            """, (student_id, term, year))
            payments = cur.fetchall()

    total_paid = sum(float(p['amount']) for p in payments)
    balance = fee_amount - total_paid

    payments_html = "".join(f"""
        <tr class="border-b border-slate-50">
            <td class="p-3 text-xs text-slate-400">{p['paid_at'].strftime('%d %b %Y') if p['paid_at'] else ''}</td>
            <td class="p-3 text-sm font-semibold text-slate-700 capitalize">{esc(p['payment_method'])}</td>
            <td class="p-3 text-xs text-slate-500">{esc(p['reference_note'] or '')}</td>
            <td class="p-3 text-right font-bold text-emerald-700">KSh {float(p['amount']):,.0f}</td>
            <td class="p-3 text-right">
                <form action="/api/v1/finance/payment/delete/{school_id}/{p['id']}" method="post" onsubmit="return confirm('Remove this payment record? This cannot be undone.');">
                    <input type="hidden" name="term" value="{esc(term)}"><input type="hidden" name="year" value="{year}"><input type="hidden" name="student_id" value="{student_id}">
                    <button type="submit" class="text-rose-600 hover:text-rose-800 text-xs font-bold">Remove</button>
                </form>
            </td>
        </tr>
    """ for p in payments)

    return f"""
    <!DOCTYPE html>
    <html>
    <head><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Elimu Hub | Fee Statement — {esc(full_student_name(student))}</title><script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script></head>
    <body class="bg-[#F7F9F8] min-h-screen p-4 sm:p-8">
        <div class="max-w-2xl mx-auto space-y-4">
            <div class="bg-white p-6 rounded-2xl border shadow-xs">
                <h2 class="text-lg font-black text-slate-800">{esc(full_student_name(student))}</h2>
                <p class="text-xs text-slate-400">#{esc(student['admission_number'])} — {esc(student['grade_name'])} — {esc(term)} {year}</p>
                <div class="grid grid-cols-3 gap-3 mt-4">
                    <div class="bg-slate-50 rounded-xl p-3 text-center"><p class="text-[10px] font-bold text-slate-400 uppercase">Fee</p><p class="font-black text-slate-800">KSh {fee_amount:,.0f}</p></div>
                    <div class="bg-emerald-50 rounded-xl p-3 text-center"><p class="text-[10px] font-bold text-emerald-600 uppercase">Paid</p><p class="font-black text-emerald-800">KSh {total_paid:,.0f}</p></div>
                    <div class="bg-rose-50 rounded-xl p-3 text-center"><p class="text-[10px] font-bold text-rose-600 uppercase">Balance</p><p class="font-black text-rose-800">KSh {balance:,.0f}</p></div>
                </div>
            </div>

            {"<div class='bg-emerald-50 border border-emerald-200 text-emerald-800 text-sm px-4 py-3 rounded-xl'>✅ Payment recorded successfully.</div>" if saved else ""}

            <div class="bg-white p-6 rounded-2xl border shadow-xs">
                <h3 class="text-sm font-bold text-slate-800 mb-3">Record a Payment</h3>
                <form action="/api/v1/finance/payment/add/{school_id}/{student_id}" method="post" class="grid grid-cols-2 gap-3">
                    <input type="hidden" name="term" value="{esc(term)}"><input type="hidden" name="year" value="{year}">
                    <div>
                        <label class="text-xs font-bold text-slate-600 block mb-1">Amount (KSh)</label>
                        <input type="number" name="amount" min="0.01" step="0.01" class="w-full border p-2.5 rounded-xl text-sm" required>
                    </div>
                    <div>
                        <label class="text-xs font-bold text-slate-600 block mb-1">Method</label>
                        <select name="payment_method" class="w-full border p-2.5 rounded-xl text-sm bg-white">
                            <option value="cash">Cash</option>
                            <option value="bank">Bank Deposit</option>
                            <option value="mpesa">M-Pesa</option>
                            <option value="other">Other</option>
                        </select>
                    </div>
                    <div class="col-span-2">
                        <label class="text-xs font-bold text-slate-600 block mb-1">Reference / Note (optional)</label>
                        <input type="text" name="reference_note" placeholder="e.g. M-Pesa code, receipt number" class="w-full border p-2.5 rounded-xl text-sm">
                    </div>
                    <button type="submit" class="col-span-2 bg-emerald-700 hover:bg-emerald-800 text-white font-bold py-3 rounded-xl text-sm transition">+ Record Payment</button>
                </form>
            </div>

            <div class="bg-white rounded-2xl border shadow-xs overflow-hidden">
                <div class="px-5 py-3 border-b bg-slate-50/60"><h3 class="text-sm font-bold text-slate-800">Payment History</h3></div>
                <table class="w-full text-sm">
                    <thead><tr class="bg-slate-50 text-slate-500 text-xs border-b"><th class="p-3 text-left">Date</th><th class="p-3 text-left">Method</th><th class="p-3 text-left">Note</th><th class="p-3 text-right">Amount</th><th class="p-3"></th></tr></thead>
                    <tbody>{payments_html or "<tr><td colspan='5' class='p-6 text-center text-slate-400 text-xs italic'>No payments recorded yet.</td></tr>"}</tbody>
                </table>
            </div>

            <a href="/finance/dashboard/{school_id}" class="bg-slate-200 hover:bg-slate-300 text-slate-700 font-bold py-2.5 px-5 rounded-xl text-sm transition inline-block">← Back to Finance</a>
        </div>
    </body>
    </html>
    """


@router.post("/api/v1/finance/payment/add/{school_id}/{student_id}")
def add_fee_payment(
    school_id: int, student_id: int, request: Request,
    amount: float = Form(...), payment_method: str = Form("cash"),
    reference_note: str = Form(""), term: str = Form(...), year: int = Form(...),
):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error
    if not FINANCE_MODULE_ENABLED:
        return _coming_soon_page(school_id, request)

    if amount <= 0:
        raise HTTPException(status_code=400, detail="Payment amount must be greater than zero.")
    if payment_method not in ("cash", "bank", "mpesa", "other"):
        payment_method = "other"

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM students WHERE id = %s AND school_id = %s;", (student_id, school_id))
            if not cur.fetchone():
                raise HTTPException(status_code=404, detail="Student not found.")

            recorded_by = request.cookies.get("session_user_id")
            cur.execute("""
                INSERT INTO fee_payments (student_id, school_id, amount, payment_method, reference_note, term, year, recorded_by_user_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s);
            """, (student_id, school_id, amount, payment_method, reference_note.strip() or None, term, year, recorded_by))
            conn.commit()

    return RedirectResponse(url=f"/finance/student/{school_id}/{student_id}?term={urllib.parse.quote(term)}&year={year}&saved=1", status_code=303)


@router.post("/api/v1/finance/payment/delete/{school_id}/{payment_id}")
def delete_fee_payment(school_id: int, payment_id: int, request: Request, term: str = Form(...), year: int = Form(...), student_id: int = Form(...)):
    auth_error = require_admin_session(request, school_id)
    if auth_error:
        return auth_error
    if not FINANCE_MODULE_ENABLED:
        return _coming_soon_page(school_id, request)

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM fee_payments WHERE id = %s AND school_id = %s;", (payment_id, school_id))
            conn.commit()

    return RedirectResponse(url=f"/finance/student/{school_id}/{student_id}?term={urllib.parse.quote(term)}&year={year}", status_code=303)
