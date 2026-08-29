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
import csv
import io
import logging
import urllib.parse
from datetime import datetime
from fastapi import APIRouter, Request, HTTPException, Form, File, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

logger = logging.getLogger("cbe_engine")

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
    """Creates/upgrades this module's tables. Purely additive to the rest of
    the app — never touches students, classes, or any other existing table.
    Uses ALTER TABLE ... ADD COLUMN IF NOT EXISTS throughout so it's safe to
    run against a fresh install or one that already has these tables."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS fee_categories (
                    id SERIAL PRIMARY KEY,
                    school_id INTEGER REFERENCES schools(id) ON DELETE CASCADE,
                    name VARCHAR(100) NOT NULL,
                    is_default BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP DEFAULT NOW(),
                    UNIQUE(school_id, name)
                );

                CREATE TABLE IF NOT EXISTS fee_structures (
                    id SERIAL PRIMARY KEY,
                    school_id INTEGER REFERENCES schools(id) ON DELETE CASCADE,
                    grade_name VARCHAR(100) NOT NULL,
                    education_level VARCHAR(100) NOT NULL,
                    term VARCHAR(20) NOT NULL,
                    year INTEGER NOT NULL,
                    amount NUMERIC(10, 2) NOT NULL
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

            # Category-aware columns — additive, nullable so this is safe
            # even if rows already exist from earlier testing.
            cur.execute("ALTER TABLE fee_structures ADD COLUMN IF NOT EXISTS fee_category_id INTEGER REFERENCES fee_categories(id) ON DELETE CASCADE;")
            cur.execute("ALTER TABLE fee_payments ADD COLUMN IF NOT EXISTS fee_category_id INTEGER REFERENCES fee_categories(id) ON DELETE SET NULL;")
            cur.execute("ALTER TABLE fee_payments ADD COLUMN IF NOT EXISTS receipt_number VARCHAR(50);")

            # Any old rows (from before fee categories existed) get backfilled
            # onto a "School Fees" default category per school, so nothing
            # silently becomes orphaned or invisible.
            cur.execute("""
                INSERT INTO fee_categories (school_id, name, is_default)
                SELECT DISTINCT school_id, 'School Fees', TRUE FROM fee_structures WHERE fee_category_id IS NULL
                UNION
                SELECT DISTINCT school_id, 'School Fees', TRUE FROM fee_payments WHERE fee_category_id IS NULL
                ON CONFLICT (school_id, name) DO NOTHING;
            """)
            cur.execute("""
                UPDATE fee_structures fs SET fee_category_id = fc.id
                FROM fee_categories fc
                WHERE fs.fee_category_id IS NULL AND fc.school_id = fs.school_id AND fc.name = 'School Fees';
            """)
            cur.execute("""
                UPDATE fee_payments fp SET fee_category_id = fc.id
                FROM fee_categories fc
                WHERE fp.fee_category_id IS NULL AND fc.school_id = fp.school_id AND fc.name = 'School Fees';
            """)

            # Re-apply the uniqueness rule now that category is part of the
            # key (a grade can have one amount per category, per term).
            # Wrapped defensively: if the constraint is already in this shape,
            # or under a different name than expected, this simply no-ops
            # rather than raising — never blocks the rest of the bootstrap.
            # (The actual save logic no longer depends on this constraint
            # existing — see save_fee_structure — but we still attempt it
            # here since a real uniqueness guarantee at the DB level is
            # good practice regardless.)
            try:
                cur.execute("ALTER TABLE fee_structures DROP CONSTRAINT IF EXISTS fee_structures_school_id_grade_name_term_year_key;")
                cur.execute("ALTER TABLE fee_structures ADD CONSTRAINT fee_structures_category_grade_term_year_key UNIQUE (school_id, fee_category_id, grade_name, term, year);")
            except Exception as e:
                conn.rollback()
                logger.warning(f"Could not add fee_structures uniqueness constraint (non-fatal, save logic doesn't depend on it): {e}")
            else:
                conn.commit()

            cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_fee_payments_receipt_number ON fee_payments (receipt_number) WHERE receipt_number IS NOT NULL;")

            # Historical backlog import tracking — every bulk-imported batch
            # of past payments (e.g. a school's old Excel sheet) is tagged,
            # so it can be reviewed or reversed as a whole if something in
            # the file turns out to be wrong, without touching any payment
            # entered normally through the app.
            cur.execute("""
                CREATE TABLE IF NOT EXISTS fee_payment_imports (
                    id SERIAL PRIMARY KEY,
                    school_id INTEGER REFERENCES schools(id) ON DELETE CASCADE,
                    filename VARCHAR(255),
                    imported_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    row_count INTEGER NOT NULL DEFAULT 0,
                    skipped_count INTEGER NOT NULL DEFAULT 0,
                    imported_at TIMESTAMP DEFAULT NOW()
                );
            """)
            cur.execute("ALTER TABLE fee_payments ADD COLUMN IF NOT EXISTS import_batch_id INTEGER REFERENCES fee_payment_imports(id) ON DELETE CASCADE;")
            conn.commit()

            # Manually-entered "brought forward" balance — for arrears that
            # predate the school's use of this system (e.g. a student who
            # already owed money before Elimu Hub's finance module existed,
            # tracked on paper or in an old spreadsheet). This amount is
            # ADDED to what the student owes for that category/term, on top
            # of the normal fee structure amount — it is not a payment, it's
            # additional debt.
            cur.execute("""
                CREATE TABLE IF NOT EXISTS fee_opening_balances (
                    id SERIAL PRIMARY KEY,
                    school_id INTEGER REFERENCES schools(id) ON DELETE CASCADE,
                    student_id INTEGER REFERENCES students(id) ON DELETE CASCADE,
                    fee_category_id INTEGER REFERENCES fee_categories(id) ON DELETE CASCADE,
                    term VARCHAR(20) NOT NULL,
                    year INTEGER NOT NULL,
                    amount NUMERIC(10, 2) NOT NULL DEFAULT 0,
                    note VARCHAR(255),
                    recorded_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    updated_at TIMESTAMP DEFAULT NOW(),
                    UNIQUE(school_id, student_id, fee_category_id, term, year)
                );
            """)
            conn.commit()


def _next_term_year(term: str, year: int):
    """Standard Kenyan school-calendar progression: Term 1 -> Term 2 ->
    Term 3 -> Term 1 of the following year. Used to pre-fill sensible
    defaults on the Carry Forward Balances form — the admin can still
    override either side manually."""
    order = ["Term 1", "Term 2", "Term 3"]
    try:
        idx = order.index(term)
    except ValueError:
        return "Term 1", year + 1
    if idx == len(order) - 1:
        return order[0], year + 1
    return order[idx + 1], year


def _ensure_default_category(cur, school_id: int) -> int:
    """Every school always has a 'School Fees' category, created lazily the
    first time it's needed. Returns its id."""
    cur.execute("SELECT id FROM fee_categories WHERE school_id = %s AND name = 'School Fees';", (school_id,))
    row = cur.fetchone()
    if row:
        return row[0] if not isinstance(row, dict) else row['id']
    cur.execute("INSERT INTO fee_categories (school_id, name, is_default) VALUES (%s, 'School Fees', TRUE) RETURNING id;", (school_id,))
    new_id = cur.fetchone()
    return new_id[0] if not isinstance(new_id, dict) else new_id['id']


def _receipt_number_from_id(school_id: int, payment_id: int) -> str:
    """Builds a human-friendly receipt number from a payment's own row id —
    e.g. RCT-005-000042. Unlike a COUNT(*)-based scheme, this can never
    collide: id is a real auto-incrementing primary key that Postgres
    guarantees is unique and never reused, even after a payment is later
    deleted, and even when many rows are inserted in the same transaction
    (as a CSV import does)."""
    return f"RCT-{school_id:03d}-{payment_id:06d}"


def _get_school_and_settings(cur, school_id):
    cur.execute("SELECT * FROM schools WHERE id = %s;", (school_id,))
    school = cur.fetchone()
    cur.execute("SELECT active_term, active_year FROM school_settings WHERE school_id = %s;", (school_id,))
    settings = cur.fetchone()
    active_term = settings['active_term'] if settings else "Term 1"
    active_year = settings['active_year'] if settings else datetime.now().year
    return school, active_term, active_year


# ============================================================
# Fee Categories — "School Fees" always exists by default;
# admins can add their own (Exam Fee, Uniform Fee, Trip Fee, etc.)
# ============================================================

@router.get("/finance/categories/{school_id}", response_class=HTMLResponse)
def fee_categories_view(school_id: int, request: Request):
    auth_error = require_admin_session(request, school_id)
    if auth_error:
        return auth_error
    if not FINANCE_MODULE_ENABLED:
        return _coming_soon_page(school_id, request)

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            school, _, _ = _get_school_and_settings(cur, school_id)
            _ensure_default_category(cur, school_id)
            conn.commit()
            cur.execute("SELECT * FROM fee_categories WHERE school_id = %s ORDER BY is_default DESC, name ASC;", (school_id,))
            categories = cur.fetchall()

    rows_html = "".join(f"""
        <div class="flex items-center justify-between py-2.5 border-b border-slate-50 last:border-0">
            <div>
                <span class="text-sm font-semibold text-slate-700">{esc(c['name'])}</span>
                {"<span class='ml-2 text-[10px] font-bold px-2 py-0.5 rounded-full bg-indigo-50 text-indigo-700 border border-indigo-200'>Default</span>" if c['is_default'] else ""}
            </div>
            {"" if c['is_default'] else f'''
            <form action="/api/v1/finance/categories/delete/{school_id}/{c['id']}" method="post" onsubmit="return confirm('Delete the \\'{esc(c['name'])}\\' fee category? Any fee amounts or payments already recorded under it will remain, but you will not be able to set new amounts for it.');">
                <button type="submit" class="text-rose-600 hover:text-rose-800 text-xs font-bold">Delete</button>
            </form>
            '''}
        </div>
    """ for c in categories)

    return f"""
    <!DOCTYPE html>
    <html>
    <head><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Elimu Hub | Fee Categories</title><script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script></head>
    <body class="bg-[#F7F9F8] min-h-screen p-4 sm:p-8">
        <div class="max-w-xl mx-auto space-y-4">
            <div class="bg-white p-6 rounded-2xl border shadow-xs">
                <h2 class="text-lg font-black text-slate-800">🏷️ Fee Categories — {esc(school['name'])}</h2>
                <p class="text-xs text-slate-400 mt-1">"School Fees" is always available. Add special fees like Exam Fee, Uniform Fee, or Trip Fee here — each gets its own amount and balance tracking.</p>
            </div>
            <div class="bg-white p-6 rounded-2xl border shadow-xs">
                <div class="mb-4">{rows_html}</div>
                <form action="/api/v1/finance/categories/add/{school_id}" method="post" class="flex gap-2">
                    <input type="text" name="name" placeholder="e.g. Exam Fee" maxlength="100" class="flex-1 border p-2.5 rounded-xl text-sm" required>
                    <button type="submit" class="bg-indigo-800 hover:bg-indigo-900 text-white font-bold px-5 py-2.5 rounded-xl text-sm transition">+ Add</button>
                </form>
            </div>
            <a href="/finance/dashboard/{school_id}" class="bg-slate-200 hover:bg-slate-300 text-slate-700 font-bold py-2.5 px-5 rounded-xl text-sm transition inline-block">← Back to Finance</a>
        </div>
    </body>
    </html>
    """


@router.post("/api/v1/finance/categories/add/{school_id}")
def add_fee_category(school_id: int, request: Request, name: str = Form(...)):
    auth_error = require_admin_session(request, school_id)
    if auth_error:
        return auth_error
    if not FINANCE_MODULE_ENABLED:
        return _coming_soon_page(school_id, request)

    name = name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="A category name is required.")

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO fee_categories (school_id, name, is_default) VALUES (%s, %s, FALSE)
                ON CONFLICT (school_id, name) DO NOTHING;
            """, (school_id, name))
            conn.commit()

    return RedirectResponse(url=f"/finance/categories/{school_id}", status_code=303)


@router.post("/api/v1/finance/categories/delete/{school_id}/{category_id}")
def delete_fee_category(school_id: int, category_id: int, request: Request):
    auth_error = require_admin_session(request, school_id)
    if auth_error:
        return auth_error
    if not FINANCE_MODULE_ENABLED:
        return _coming_soon_page(school_id, request)

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            # The default "School Fees" category can never be deleted —
            # every school must always have at least one fee category.
            cur.execute("DELETE FROM fee_categories WHERE id = %s AND school_id = %s AND is_default = FALSE;", (category_id, school_id))
            conn.commit()

    return RedirectResponse(url=f"/finance/categories/{school_id}", status_code=303)


# ============================================================
# Finance Dashboard — overview across every fee category
# ============================================================

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
            _ensure_default_category(cur, school_id)
            conn.commit()
            term = term or active_term
            year = year or active_year

            cur.execute("SELECT * FROM fee_categories WHERE school_id = %s ORDER BY is_default DESC, name ASC;", (school_id,))
            categories = cur.fetchall()

            # Expected, per category: sum(fee amount x students in that grade)
            cur.execute("""
                SELECT fs.fee_category_id, COALESCE(SUM(fs.amount * sub.student_count), 0) AS expected
                FROM fee_structures fs
                JOIN (
                    SELECT c.grade_name, COUNT(DISTINCT s.id) AS student_count
                    FROM students s JOIN classes c ON s.class_id = c.id
                    WHERE s.school_id = %s AND (s.status IS NULL OR s.status != 'GRADUATED')
                    GROUP BY c.grade_name
                ) sub ON sub.grade_name = fs.grade_name
                WHERE fs.school_id = %s AND fs.term = %s AND fs.year = %s
                GROUP BY fs.fee_category_id;
            """, (school_id, school_id, term, year))
            expected_by_category = {r['fee_category_id']: float(r['expected']) for r in cur.fetchall()}

            cur.execute("""
                SELECT fee_category_id, COALESCE(SUM(amount), 0) AS collected
                FROM fee_payments WHERE school_id = %s AND term = %s AND year = %s
                GROUP BY fee_category_id;
            """, (school_id, term, year))
            collected_by_category = {r['fee_category_id']: float(r['collected']) for r in cur.fetchall()}

            cur.execute("""
                SELECT c.grade_name, c.education_level, s.stream, COUNT(DISTINCT s.id) AS student_count
                FROM students s JOIN classes c ON s.class_id = c.id
                WHERE s.school_id = %s AND (s.status IS NULL OR s.status != 'GRADUATED')
                GROUP BY c.grade_name, c.education_level, s.stream
                ORDER BY c.grade_name, s.stream;
            """, (school_id,))
            class_rows = cur.fetchall()

    total_expected = sum(expected_by_category.values())
    total_collected = sum(collected_by_category.values())
    total_outstanding = max(0, total_expected - total_collected)

    category_cards_html = "".join(f"""
        <div class="bg-white rounded-2xl border shadow-xs p-4">
            <p class="text-xs font-bold text-slate-600 mb-2">{esc(c['name'])}</p>
            <div class="flex justify-between text-xs">
                <span class="text-slate-400">Expected</span><span class="font-semibold">KSh {expected_by_category.get(c['id'], 0):,.0f}</span>
            </div>
            <div class="flex justify-between text-xs mt-1">
                <span class="text-slate-400">Collected</span><span class="font-semibold text-emerald-700">KSh {collected_by_category.get(c['id'], 0):,.0f}</span>
            </div>
            <a href="/finance/fee-structure/{school_id}?category_id={c['id']}&term={urllib.parse.quote(term)}&year={year}" class="text-indigo-700 hover:underline text-xs font-bold block mt-2">Set amounts →</a>
        </div>
    """ for c in categories)

    class_rows_html = ""
    for r in class_rows:
        section_label = r['grade_name'] if (not r['stream'] or r['stream'] == 'SINGLE STREAM') else f"{r['grade_name']} — {r['stream']}"
        encoded_grade = urllib.parse.quote(r['grade_name'])
        encoded_stream = urllib.parse.quote(r['stream'] or 'SINGLE STREAM')
        encoded_level = urllib.parse.quote(r['education_level'])
        class_rows_html += f"""
        <tr class="border-b border-slate-50">
            <td class="p-3 font-semibold text-slate-700">{esc(section_label)}</td>
            <td class="p-3 text-center">{r['student_count']}</td>
            <td class="p-3 text-right"><a href="/finance/class/{school_id}?grade_name={encoded_grade}&stream={encoded_stream}&education_level={encoded_level}&term={urllib.parse.quote(term)}&year={year}" class="text-indigo-700 hover:underline text-xs font-bold">View Fees →</a></td>
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
                <p class="text-xs text-slate-400">{esc(term)} {year} — fees are tracked and recorded manually.</p>
            </div>
            <div class="flex items-center gap-2 flex-wrap">
                <a href="/finance/categories/{school_id}" class="bg-white hover:bg-slate-50 text-slate-600 border border-slate-200 px-4 py-2 rounded-xl text-xs font-bold transition">🏷️ Fee Categories</a>
                <a href="/finance/import/{school_id}" class="bg-white hover:bg-slate-50 text-slate-600 border border-slate-200 px-4 py-2 rounded-xl text-xs font-bold transition">📥 Import History</a>
                <a href="/finance/carry-forward/{school_id}" class="bg-amber-50 hover:bg-amber-100 text-amber-700 border border-amber-200 px-4 py-2 rounded-xl text-xs font-bold transition">🔁 Carry Forward Balances</a>
                <a href="{get_dashboard_url(request, school_id)}" class="bg-slate-800 hover:bg-slate-900 text-white px-4 py-2 rounded-xl text-xs font-bold transition">← Back to Dashboard</a>
            </div>
        </header>

        <div class="p-4 sm:p-8 max-w-5xl mx-auto space-y-6">
            <div class="grid grid-cols-1 sm:grid-cols-3 gap-4">
                <div class="bg-white rounded-2xl border shadow-xs p-5 border-l-4" style="border-left-color:#4f46e5;">
                    <p class="text-[11px] font-bold uppercase tracking-wider text-slate-400">Expected This Term (All Fees)</p>
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

            <div>
                <h2 class="text-xs font-bold uppercase tracking-wider text-slate-400 mb-3">By Fee Category</h2>
                <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">{category_cards_html}</div>
            </div>

            <div class="bg-white rounded-2xl border shadow-xs overflow-hidden">
                <div class="px-5 py-3 border-b bg-slate-50/60"><h2 class="text-sm font-bold text-slate-800">Classes</h2></div>
                <table class="w-full text-sm">
                    <thead><tr class="bg-slate-50 text-slate-500 text-xs border-b"><th class="p-3 text-left">Class</th><th class="p-3 text-center">Students</th><th class="p-3"></th></tr></thead>
                    <tbody>{class_rows_html or "<tr><td colspan='3' class='p-8 text-center text-slate-400 text-xs italic'>No classes with students yet.</td></tr>"}</tbody>
                </table>
            </div>
        </div>
    </body>
    </html>
    """


# ============================================================
# Carry Forward Balances — an overpayment or underpayment from one
# term becomes the opening balance for the next, automatically.
# ============================================================

@router.get("/finance/carry-forward/{school_id}", response_class=HTMLResponse)
def carry_forward_view(school_id: int, request: Request, done: str = None):
    auth_error = require_admin_session(request, school_id)
    if auth_error:
        return auth_error
    if not FINANCE_MODULE_ENABLED:
        return _coming_soon_page(school_id, request)

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            school, active_term, active_year = _get_school_and_settings(cur, school_id)

    default_to_term, default_to_year = _next_term_year(active_term, active_year)
    term_choices = ["Term 1", "Term 2", "Term 3"]

    from_term_options = "".join(f"<option value='{t}' {'selected' if t == active_term else ''}>{t}</option>" for t in term_choices)
    to_term_options = "".join(f"<option value='{t}' {'selected' if t == default_to_term else ''}>{t}</option>" for t in term_choices)

    return f"""
    <!DOCTYPE html>
    <html>
    <head><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Elimu Hub | Carry Forward Balances</title><script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script></head>
    <body class="bg-slate-100 min-h-screen p-4 sm:p-8">
        <div class="max-w-xl mx-auto space-y-4">
            <div class="bg-white p-6 rounded-2xl border shadow-xs space-y-4">
                <h2 class="text-lg font-black text-slate-800">🔁 Carry Forward Balances</h2>
                <p class="text-xs text-slate-500">
                    For every student and every fee category, this computes each student's actual closing balance for the "From" term —
                    <span class="font-mono bg-slate-100 px-1 rounded">fee amount + old opening balance − amount paid</span> —
                    and sets that as their opening balance for the "To" term. If they <b>underpaid</b>, they'll start the new term already owing that amount.
                    If they <b>overpaid</b>, that credit will reduce (or fully cover) what they owe next.
                </p>
                {"<div class='bg-emerald-50 border border-emerald-200 text-emerald-800 text-xs px-4 py-2.5 rounded-xl'>✅ Balances carried forward for " + esc(done) + " student/category combination(s).</div>" if done else ""}
                <div class="bg-amber-50 border border-amber-200 text-amber-800 text-xs px-4 py-3 rounded-xl">
                    ⚠️ <b>Run this before "Advance All Classes"</b>, not after — fee amounts are looked up by each student's <i>current</i> class. If you promote classes first, this will use their new grade's fee amount instead of the grade they were actually in during the "From" term, producing the wrong closing balance for anyone who was promoted.
                </div>
                <form action="/api/v1/finance/carry-forward/{school_id}" method="post" class="space-y-3" onsubmit="return confirm('Carry forward balances from the From term into the To term? This overwrites any existing opening balance already set for the To term, for every student and every fee category. This cannot be undone automatically (though you can re-run it, or edit individual balances afterward).');">
                    <div class="grid grid-cols-2 gap-3">
                        <div class="space-y-2">
                            <p class="text-[11px] font-bold uppercase text-slate-400">From</p>
                            <select name="from_term" class="w-full border border-slate-200 p-2 rounded-lg text-sm">{from_term_options}</select>
                            <input type="number" name="from_year" value="{active_year}" class="w-full border border-slate-200 p-2 rounded-lg text-sm">
                        </div>
                        <div class="space-y-2">
                            <p class="text-[11px] font-bold uppercase text-slate-400">To</p>
                            <select name="to_term" class="w-full border border-slate-200 p-2 rounded-lg text-sm">{to_term_options}</select>
                            <input type="number" name="to_year" value="{default_to_year}" class="w-full border border-slate-200 p-2 rounded-lg text-sm">
                        </div>
                    </div>
                    <button type="submit" class="w-full bg-amber-500 hover:bg-amber-600 text-white py-2.5 rounded-xl text-sm font-bold transition">Carry Forward Balances</button>
                </form>
            </div>
            <a href="/finance/dashboard/{school_id}" class="text-slate-500 hover:text-slate-700 text-xs font-bold inline-block">← Back to Finance</a>
        </div>
    </body>
    </html>
    """


@router.post("/api/v1/finance/carry-forward/{school_id}")
def run_carry_forward_balances(cur, school_id: int, from_term: str, from_year: int, to_term: str, to_year: int, recorded_by=None, note_prefix: str = "Carried forward"):
    """For every active student and every fee category, computes the
    student's real closing balance for the From term (fee amount + old
    opening balance - amount paid) and writes that as their opening
    balance for the To term — positive if they underpaid (they now owe
    that much more), negative if they overpaid (a credit that reduces
    what they owe next). Reuses fee_opening_balances, the same table
    the manual "brought forward" entry already uses, so every existing
    balance display in the app picks this up automatically with no
    other changes needed.

    Takes an already-open cursor rather than opening its own connection,
    so callers elsewhere (e.g. main.py's Settings save and Advance All
    Classes routes) can run this as part of their own transaction —
    important for Advance All Classes specifically, since fee amounts
    are looked up via each student's CURRENT class, and this must run
    with the OLD class assignments still in place, before promotion
    changes them.

    Returns the number of student/category combinations written."""
    if (from_term, from_year) == (to_term, to_year):
        return 0

    note = f"{note_prefix} from {from_term} {from_year}"
    combinations_written = 0

    cur.execute("SELECT id FROM fee_categories WHERE school_id = %s;", (school_id,))
    category_ids = [r[0] if not isinstance(r, dict) else r['id'] for r in cur.fetchall()]

    for cat_id in category_ids:
        cur.execute("""
            SELECT s.id AS student_id,
                   COALESCE(fs.amount, 0) AS fee_amount,
                   COALESCE((SELECT amount FROM fee_opening_balances fob WHERE fob.student_id = s.id AND fob.fee_category_id = %s AND fob.term = %s AND fob.year = %s), 0) AS old_opening,
                   COALESCE((SELECT SUM(fp.amount) FROM fee_payments fp WHERE fp.student_id = s.id AND fp.fee_category_id = %s AND fp.term = %s AND fp.year = %s), 0) AS paid
            FROM students s
            JOIN classes c ON s.class_id = c.id
            LEFT JOIN fee_structures fs ON fs.school_id = s.school_id AND fs.fee_category_id = %s AND fs.grade_name = c.grade_name AND fs.term = %s AND fs.year = %s
            WHERE s.school_id = %s AND (s.status IS NULL OR s.status != 'GRADUATED');
        """, (cat_id, from_term, from_year, cat_id, from_term, from_year, cat_id, from_term, from_year, school_id))
        rows = cur.fetchall()

        for row in rows:
            # Works whether the caller's cursor is a plain tuple cursor
            # (main.py sometimes uses one) or a RealDictCursor.
            student_id = row['student_id'] if isinstance(row, dict) else row[0]
            fee_amount = row['fee_amount'] if isinstance(row, dict) else row[1]
            old_opening = row['old_opening'] if isinstance(row, dict) else row[2]
            paid = row['paid'] if isinstance(row, dict) else row[3]

            closing = float(fee_amount) + float(old_opening) - float(paid)

            if abs(closing) < 0.005:
                # Fully settled, no credit either — remove any stale
                # destination row rather than leave a 0 behind.
                cur.execute("""
                    DELETE FROM fee_opening_balances
                    WHERE school_id = %s AND student_id = %s AND fee_category_id = %s AND term = %s AND year = %s;
                """, (school_id, student_id, cat_id, to_term, to_year))
                continue

            cur.execute("""
                SELECT id FROM fee_opening_balances
                WHERE school_id = %s AND student_id = %s AND fee_category_id = %s AND term = %s AND year = %s;
            """, (school_id, student_id, cat_id, to_term, to_year))
            existing_row = cur.fetchone()
            if existing_row:
                existing_id = existing_row['id'] if isinstance(existing_row, dict) else existing_row[0]
                cur.execute("""
                    UPDATE fee_opening_balances SET amount = %s, note = %s, recorded_by_user_id = %s, updated_at = NOW()
                    WHERE id = %s;
                """, (closing, note, recorded_by, existing_id))
            else:
                cur.execute("""
                    INSERT INTO fee_opening_balances (school_id, student_id, fee_category_id, term, year, amount, note, recorded_by_user_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s);
                """, (school_id, student_id, cat_id, to_term, to_year, closing, note, recorded_by))
            combinations_written += 1

    return combinations_written


@router.post("/api/v1/finance/carry-forward/{school_id}")
async def carry_forward_balances(school_id: int, request: Request, from_term: str = Form(...), from_year: int = Form(...), to_term: str = Form(...), to_year: int = Form(...)):
    """Manual trigger for run_carry_forward_balances — kept available for
    ad-hoc re-runs and corrections, even though the common cases (a new
    term or a new year via Advance All Classes) now trigger this
    automatically from main.py."""
    auth_error = require_admin_session(request, school_id)
    if auth_error:
        return auth_error
    if not FINANCE_MODULE_ENABLED:
        return _coming_soon_page(school_id, request)

    if (from_term, from_year) == (to_term, to_year):
        raise HTTPException(status_code=400, detail="From and To must be different terms.")

    recorded_by = request.cookies.get("session_user_id")

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            combinations_written = run_carry_forward_balances(cur, school_id, from_term, from_year, to_term, to_year, recorded_by)
            conn.commit()

    return RedirectResponse(url=f"/finance/carry-forward/{school_id}?done={combinations_written}", status_code=303)


# ============================================================
# Fee Structure — set the amount per grade, per category, per term
# ============================================================

@router.get("/finance/fee-structure/{school_id}", response_class=HTMLResponse)
def fee_structure_view(school_id: int, request: Request, category_id: int = None, term: str = None, year: int = None):
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
            default_category_id = _ensure_default_category(cur, school_id)
            conn.commit()
            term = term or active_term
            year = year or active_year
            category_id = category_id or default_category_id

            cur.execute("SELECT * FROM fee_categories WHERE school_id = %s ORDER BY is_default DESC, name ASC;", (school_id,))
            categories = cur.fetchall()
            current_category = next((c for c in categories if c['id'] == category_id), categories[0] if categories else None)

            cur.execute("SELECT grade_name, education_level FROM classes GROUP BY grade_name, education_level ORDER BY MIN(id) ASC;")
            all_grades = cur.fetchall()

            cur.execute("SELECT grade_name, amount FROM fee_structures WHERE school_id = %s AND fee_category_id = %s AND term = %s AND year = %s;", (school_id, category_id, term, year))
            existing = {r['grade_name']: float(r['amount']) for r in cur.fetchall()}

    category_tabs = "".join(
        f"""<a href="/finance/fee-structure/{school_id}?category_id={c['id']}&term={urllib.parse.quote(term)}&year={year}"
               class="px-3 py-1.5 rounded-lg text-xs font-bold transition {'bg-indigo-800 text-white' if c['id'] == category_id else 'bg-white border border-slate-200 text-slate-600 hover:bg-slate-50'}">{esc(c['name'])}</a>"""
        for c in categories
    )

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
                <p class="text-xs text-slate-400 mt-1">Set the expected amount per grade for the selected fee category. Leave blank for grades that don't pay this fee.</p>
            </div>
            <div class="flex gap-2 flex-wrap">{category_tabs}</div>
            <form action="/api/v1/finance/fee-structure/save/{school_id}" method="post" class="bg-white p-6 rounded-2xl border shadow-xs">
                <input type="hidden" name="category_id" value="{category_id}">
                <input type="hidden" name="term" value="{esc(term)}">
                <input type="hidden" name="year" value="{year}">
                <h3 class="text-sm font-bold text-indigo-700 mb-2">{esc(current_category['name']) if current_category else ''}</h3>
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
        category_id = int(form_data.get("category_id"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid year or category.")
    if not term:
        raise HTTPException(status_code=400, detail="Term is required.")

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT id FROM fee_categories WHERE id = %s AND school_id = %s;", (category_id, school_id))
            if not cur.fetchone():
                raise HTTPException(status_code=400, detail="Invalid fee category.")

            cur.execute("SELECT grade_name, education_level FROM classes GROUP BY grade_name, education_level ORDER BY MIN(id) ASC;")
            all_grades = cur.fetchall()

            for g in all_grades:
                raw_val = form_data.get(f"amount_{g['grade_name']}", "")
                if str(raw_val).strip() == "":
                    continue
                try:
                    amount = float(raw_val)
                except ValueError:
                    continue
                if amount < 0:
                    continue

                # Deliberately not using ON CONFLICT here — that requires a
                # specific named unique constraint to exist on the table,
                # and relying on that turned out to be fragile (a schema
                # migration adding it could silently fail on some databases,
                # which would make every single save here error out with
                # "no unique or exclusion constraint matching ON CONFLICT").
                # This check-then-update-or-insert works regardless of
                # whether that constraint actually exists.
                try:
                    cur.execute("""
                        SELECT id FROM fee_structures
                        WHERE school_id = %s AND fee_category_id = %s AND grade_name = %s AND term = %s AND year = %s;
                    """, (school_id, category_id, g['grade_name'], term, year))
                    existing_row = cur.fetchone()
                    if existing_row:
                        cur.execute("UPDATE fee_structures SET amount = %s WHERE id = %s;", (amount, existing_row['id']))
                    else:
                        cur.execute("""
                            INSERT INTO fee_structures (school_id, fee_category_id, grade_name, education_level, term, year, amount)
                            VALUES (%s, %s, %s, %s, %s, %s, %s);
                        """, (school_id, category_id, g['grade_name'], g['education_level'], term, year, amount))
                    conn.commit()
                except Exception as e:
                    # One bad grade shouldn't stop every other grade in this
                    # same save from going through.
                    conn.rollback()
                    logger.warning(f"Failed to save fee structure for school {school_id}, grade {g['grade_name']}: {e}")

    return RedirectResponse(url=f"/finance/dashboard/{school_id}?term={urllib.parse.quote(term)}&year={year}", status_code=303)


# ============================================================
# Class fee list — every student in a class, balance for one category
# ============================================================

@router.get("/finance/class/{school_id}", response_class=HTMLResponse)
def finance_class_list(school_id: int, request: Request, grade_name: str, education_level: str, stream: str, category_id: int = None, term: str = None, year: int = None):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error
    if not FINANCE_MODULE_ENABLED:
        return _coming_soon_page(school_id, request)

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            school, active_term, active_year = _get_school_and_settings(cur, school_id)
            default_category_id = _ensure_default_category(cur, school_id)
            conn.commit()
            term = term or active_term
            year = year or active_year
            category_id = category_id or default_category_id

            cur.execute("SELECT * FROM fee_categories WHERE school_id = %s ORDER BY is_default DESC, name ASC;", (school_id,))
            categories = cur.fetchall()
            current_category = next((c for c in categories if c['id'] == category_id), categories[0] if categories else None)

            cur.execute("SELECT amount FROM fee_structures WHERE school_id = %s AND fee_category_id = %s AND grade_name = %s AND term = %s AND year = %s;",
                        (school_id, category_id, grade_name, term, year))
            fee_row = cur.fetchone()
            fee_amount = float(fee_row['amount']) if fee_row else 0

            cur.execute("""
                SELECT s.id, s.first_name, s.middle_name, s.last_name, s.admission_number,
                       COALESCE((SELECT SUM(fp.amount) FROM fee_payments fp WHERE fp.student_id = s.id AND fp.fee_category_id = %s AND fp.term = %s AND fp.year = %s), 0) AS paid,
                       COALESCE((SELECT amount FROM fee_opening_balances fob WHERE fob.student_id = s.id AND fob.fee_category_id = %s AND fob.term = %s AND fob.year = %s), 0) AS opening_balance
                FROM students s
                JOIN classes c ON s.class_id = c.id
                WHERE s.school_id = %s AND c.grade_name = %s AND c.education_level = %s AND s.stream = %s
                  AND (s.status IS NULL OR s.status != 'GRADUATED')
                ORDER BY s.admission_number ASC;
            """, (category_id, term, year, category_id, term, year, school_id, grade_name, education_level, stream))
            students = cur.fetchall()

    section_label = grade_name if stream == 'SINGLE STREAM' else f"{grade_name} — {stream}"
    encoded_grade, encoded_stream, encoded_level = urllib.parse.quote(grade_name), urllib.parse.quote(stream), urllib.parse.quote(education_level)

    category_tabs = "".join(
        f"""<a href="/finance/class/{school_id}?grade_name={encoded_grade}&stream={encoded_stream}&education_level={encoded_level}&category_id={c['id']}&term={urllib.parse.quote(term)}&year={year}"
               class="px-3 py-1.5 rounded-lg text-xs font-bold transition {'bg-indigo-800 text-white' if c['id'] == category_id else 'bg-white border border-slate-200 text-slate-600 hover:bg-slate-50'}">{esc(c['name'])}</a>"""
        for c in categories
    )

    rows_html = ""
    for s in students:
        paid = float(s['paid'])
        opening = float(s['opening_balance'])
        balance = fee_amount + opening - paid
        status_badge = (
            "<span class='text-[10px] font-bold px-2 py-0.5 rounded-full bg-sky-50 text-sky-700 border border-sky-200'>Credit — KSh " + f"{abs(balance):,.0f}" + "</span>" if balance < 0 else
            "<span class='text-[10px] font-bold px-2 py-0.5 rounded-full bg-emerald-50 text-emerald-700 border border-emerald-200'>Paid in full</span>" if balance == 0 and fee_amount > 0 else
            "<span class='text-[10px] font-bold px-2 py-0.5 rounded-full bg-amber-50 text-amber-700 border border-amber-200'>Balance owing</span>" if balance > 0 else
            "<span class='text-[10px] font-bold px-2 py-0.5 rounded-full bg-slate-100 text-slate-500 border border-slate-200'>No fee set</span>"
        )
        rows_html += f"""
        <tr class="border-b border-slate-50">
            <td class="p-3 font-mono text-xs text-slate-400">{esc(s['admission_number'])}</td>
            <td class="p-3 font-semibold text-slate-700">{esc(full_student_name(s))}</td>
            <td class="p-3 text-right">KSh {paid:,.0f}</td>
            <td class="p-3 text-right font-bold {'text-rose-700' if balance > 0 else 'text-sky-700' if balance < 0 else 'text-emerald-700'}">KSh {balance:,.0f}</td>
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
                <p class="text-xs text-slate-400">{esc(term)} {year} — {esc(current_category['name']) if current_category else ''}: KSh {fee_amount:,.0f} per student</p>
            </div>
            <div class="flex items-center gap-2">
                <a href="/finance/class-statement-print/{school_id}?grade_name={encoded_grade}&stream={encoded_stream}&education_level={encoded_level}&category_id={category_id}&term={urllib.parse.quote(term)}&year={year}" target="_blank" class="bg-white hover:bg-slate-50 text-slate-600 border border-slate-200 px-4 py-2 rounded-xl text-xs font-bold transition">🖨 Print Statement</a>
                <a href="/finance/dashboard/{school_id}" class="bg-slate-800 hover:bg-slate-900 text-white px-4 py-2 rounded-xl text-xs font-bold transition">← Back to Finance</a>
            </div>
        </header>
        <div class="p-4 sm:p-8 max-w-4xl mx-auto space-y-4">
            <div class="flex gap-2 flex-wrap">{category_tabs}</div>
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


@router.get("/finance/class-statement-print/{school_id}", response_class=HTMLResponse)
def class_statement_print(school_id: int, request: Request, grade_name: str, education_level: str, stream: str, category_id: int = None, term: str = None, year: int = None):
    """A printable fee statement for a whole class, for one fee category —
    lists every student's fee, brought-forward balance, paid, and balance,
    formatted for handing out or filing rather than for on-screen browsing.
    Purely read-only."""
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error
    if not FINANCE_MODULE_ENABLED:
        return _coming_soon_page(school_id, request)

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            school, active_term, active_year = _get_school_and_settings(cur, school_id)
            default_category_id = _ensure_default_category(cur, school_id)
            conn.commit()
            term = term or active_term
            year = year or active_year
            category_id = category_id or default_category_id

            cur.execute("SELECT * FROM fee_categories WHERE id = %s AND school_id = %s;", (category_id, school_id))
            category = cur.fetchone()

            cur.execute("""
                SELECT amount FROM fee_structures
                WHERE school_id = %s AND fee_category_id = %s AND grade_name = %s AND term = %s AND year = %s;
            """, (school_id, category_id, grade_name, term, year))
            fee_row = cur.fetchone()
            fee_amount = float(fee_row['amount']) if fee_row else 0

            cur.execute("""
                SELECT s.id, s.first_name, s.middle_name, s.last_name, s.admission_number,
                       COALESCE((SELECT SUM(fp.amount) FROM fee_payments fp WHERE fp.student_id = s.id AND fp.fee_category_id = %s AND fp.term = %s AND fp.year = %s), 0) AS paid,
                       COALESCE((SELECT amount FROM fee_opening_balances fob WHERE fob.student_id = s.id AND fob.fee_category_id = %s AND fob.term = %s AND fob.year = %s), 0) AS opening_balance
                FROM students s
                JOIN classes c ON s.class_id = c.id
                WHERE s.school_id = %s AND c.grade_name = %s AND c.education_level = %s AND s.stream = %s
                  AND (s.status IS NULL OR s.status != 'GRADUATED')
                ORDER BY s.admission_number ASC;
            """, (category_id, term, year, category_id, term, year, school_id, grade_name, education_level, stream))
            students = cur.fetchall()

    section_label = grade_name if stream == 'SINGLE STREAM' else f"{grade_name} — {stream}"
    logo_src = school.get('logo_url')
    logo_html = ""
    if logo_src:
        final_src = logo_src if logo_src.startswith("http") else f"/{logo_src.lstrip('/')}"
        logo_html = f"<img src='{final_src}' style='width:64px;height:64px;object-fit:contain;' />"

    total_expected = fee_amount * len(students)
    total_paid = sum(float(s['paid']) for s in students)
    total_balance = sum(fee_amount + float(s['opening_balance']) - float(s['paid']) for s in students)

    rows_html = ""
    for i, s in enumerate(students, start=1):
        paid = float(s['paid'])
        opening = float(s['opening_balance'])
        balance = fee_amount + opening - paid
        rows_html += f"""
        <tr>
            <td style="padding:6px 8px;border-bottom:1px solid #e2e8f0;text-align:center;">{i}</td>
            <td style="padding:6px 8px;border-bottom:1px solid #e2e8f0;font-family:monospace;">{esc(s['admission_number'])}</td>
            <td style="padding:6px 8px;border-bottom:1px solid #e2e8f0;">{esc(full_student_name(s))}</td>
            <td style="padding:6px 8px;border-bottom:1px solid #e2e8f0;text-align:right;">{fee_amount:,.0f}</td>
            <td style="padding:6px 8px;border-bottom:1px solid #e2e8f0;text-align:right;">{opening:,.0f}</td>
            <td style="padding:6px 8px;border-bottom:1px solid #e2e8f0;text-align:right;">{paid:,.0f}</td>
            <td style="padding:6px 8px;border-bottom:1px solid #e2e8f0;text-align:right;font-weight:bold;color:{'#be123c' if balance > 0 else '#047857'};">{balance:,.0f}</td>
        </tr>
        """

    from mpesa_routes import render_admin_print_toolbar_and_content
    document_content_html = f"""
        <div style="display:flex;align-items:center;gap:16px;border-bottom:3px double #4f46e5;padding-bottom:12px;">
            {logo_html}
            <div>
                <h1 style="margin:0;font-size:18px;">{esc(school['name'])}</h1>
                <p style="margin:2px 0 0;font-size:12px;color:#64748b;">Fee Statement — {esc(section_label)} — {esc(category['name']) if category else 'School Fees'} — {esc(term)} {year}</p>
            </div>
        </div>
        <table style="margin-top:16px;">
            <thead>
                <tr><th style="text-align:center;">#</th><th>Adm No.</th><th>Full Name</th><th style="text-align:right;">Fee (KSh)</th><th style="text-align:right;">Brought Fwd</th><th style="text-align:right;">Paid</th><th style="text-align:right;">Balance</th></tr>
            </thead>
            <tbody>{rows_html or "<tr><td colspan='7' style='padding:20px;text-align:center;color:#94a3b8;'>No students in this class.</td></tr>"}</tbody>
            <tfoot>
                <tr style="font-weight:bold;background:#f8fafc;">
                    <td colspan="5" style="padding:8px;text-align:right;border-top:2px solid #cbd5e1;">Totals</td>
                    <td style="padding:8px;text-align:right;border-top:2px solid #cbd5e1;">{total_paid:,.0f}</td>
                    <td style="padding:8px;text-align:right;border-top:2px solid #cbd5e1;color:{'#be123c' if total_balance > 0 else '#047857'};">{total_balance:,.0f}</td>
                </tr>
            </tfoot>
        </table>
        <p style="margin-top:8px;font-size:10px;color:#94a3b8;">Expected total for this class/category: KSh {total_expected:,.0f}</p>
        <p style="margin-top:24px;font-size:10px;color:#94a3b8;text-align:center;">Generated by Elimu Hub Finance — {esc(school['name'])}</p>
    """
    toolbar_button, content_html, extra_style_html = render_admin_print_toolbar_and_content(school_id, document_content_html, "fee statement", "#4f46e5")

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Elimu Hub | Fee Statement — {esc(section_label)}</title>
        <style>
            @page {{ size: A4 portrait; margin: 14mm; }}
            body {{ font-family: Arial, sans-serif; padding: 20px; color: #1e293b; }}
            @media print {{ .no-print {{ display: none !important; }} body {{ padding: 0; }} }}
            table {{ width: 100%; border-collapse: collapse; font-size: 11px; }}
            th {{ text-align: left; background: #f8fafc; border-bottom: 2px solid #cbd5e1; padding: 6px 8px; font-size: 10px; text-transform: uppercase; color: #64748b; }}
        </style>
        {extra_style_html}
    </head>
    <body>
        <div class="no-print" style="text-align:right; margin-bottom:16px;">
            {toolbar_button}
        </div>
        {content_html}
    </body>
    </html>
    """


# ============================================================
# Student fee statement — every category at once, record payments,
# link to a printable receipt for each one
# ============================================================

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
            _ensure_default_category(cur, school_id)
            conn.commit()
            term = term or active_term
            year = year or active_year

            cur.execute("""
                SELECT s.*, c.grade_name FROM students s JOIN classes c ON s.class_id = c.id
                WHERE s.id = %s AND s.school_id = %s;
            """, (student_id, school_id))
            student = cur.fetchone()
            if not student:
                raise HTTPException(status_code=404, detail="Student not found.")

            cur.execute("SELECT * FROM fee_categories WHERE school_id = %s ORDER BY is_default DESC, name ASC;", (school_id,))
            categories = cur.fetchall()

            cur.execute("""
                SELECT fee_category_id, amount FROM fee_structures
                WHERE school_id = %s AND grade_name = %s AND term = %s AND year = %s;
            """, (school_id, student['grade_name'], term, year))
            fee_by_category = {r['fee_category_id']: float(r['amount']) for r in cur.fetchall()}

            cur.execute("""
                SELECT fee_category_id, amount, note FROM fee_opening_balances
                WHERE school_id = %s AND student_id = %s AND term = %s AND year = %s;
            """, (school_id, student_id, term, year))
            opening_by_category = {r['fee_category_id']: {'amount': float(r['amount']), 'note': r['note']} for r in cur.fetchall()}

            cur.execute("""
                SELECT fee_category_id, COALESCE(SUM(amount), 0) AS paid FROM fee_payments
                WHERE student_id = %s AND term = %s AND year = %s GROUP BY fee_category_id;
            """, (student_id, term, year))
            paid_by_category = {r['fee_category_id']: float(r['paid']) for r in cur.fetchall()}

            # The balance/fee summary above is deliberately for ONE specific
            # period (whichever term is selected) — but the payment history
            # below shows EVERY payment ever recorded for this student,
            # regardless of term. This matters specifically for imported
            # backlog data: a payment from Term 1 2024 should always be
            # visible here, not just when someone happens to be viewing
            # that exact historical term.
            cur.execute("""
                SELECT fp.*, fc.name AS category_name FROM fee_payments fp
                LEFT JOIN fee_categories fc ON fp.fee_category_id = fc.id
                WHERE fp.student_id = %s ORDER BY fp.paid_at DESC;
            """, (student_id,))
            payments = cur.fetchall()

            # Every term/year that either has a configured fee or has any
            # payment recorded for this student, so the period selector
            # covers historical terms too, not just currently-active ones.
            cur.execute("""
                SELECT DISTINCT term, year FROM fee_structures WHERE school_id = %s
                UNION
                SELECT DISTINCT term, year FROM fee_payments WHERE student_id = %s
                ORDER BY year DESC, term DESC;
            """, (school_id, student_id))
            available_periods = cur.fetchall()

    total_fee = sum(fee_by_category.values())
    total_opening = sum(v['amount'] for v in opening_by_category.values())
    total_paid = sum(paid_by_category.values())
    total_balance = total_fee + total_opening - total_paid

    category_options = "".join(f"<option value='{c['id']}'>{esc(c['name'])}</option>" for c in categories)

    period_options = "".join(
        f"<option value='{esc(p['term'])}|{p['year']}' {'selected' if p['term'] == term and p['year'] == year else ''}>{esc(p['term'])} {p['year']}</option>"
        for p in available_periods
    )

    breakdown_html = ""
    for c in categories:
        fee = fee_by_category.get(c['id'], 0)
        opening = opening_by_category.get(c['id'], {}).get('amount', 0)
        paid = paid_by_category.get(c['id'], 0)
        bal = fee + opening - paid
        if fee == 0 and paid == 0 and opening == 0:
            continue  # skip categories entirely irrelevant to this student
        breakdown_html += f"""
        <div class="flex items-center justify-between py-2 border-b border-slate-50 last:border-0 text-sm flex-wrap gap-1">
            <span class="font-semibold text-slate-700">{esc(c['name'])}</span>
            <span class="text-slate-400">Fee: KSh {fee:,.0f}</span>
            {f"<span class='text-amber-600'>+ Brought forward: KSh {opening:,.0f}</span>" if opening else ""}
            <span class="text-emerald-700">Paid: KSh {paid:,.0f}</span>
            <span class="font-bold {'text-rose-700' if bal > 0 else 'text-emerald-700'}">Bal: KSh {bal:,.0f}</span>
        </div>
        """

    payments_html = "".join(f"""
        <tr class="border-b border-slate-50">
            <td class="p-3 text-xs text-slate-400">{p['paid_at'].strftime('%d %b %Y') if p['paid_at'] else ''}</td>
            <td class="p-3 text-xs text-slate-500">{esc(p['term'])} {p['year']}</td>
            <td class="p-3 text-xs font-semibold text-indigo-700">{esc(p['category_name'] or 'School Fees')}</td>
            <td class="p-3 text-sm font-semibold text-slate-700 capitalize">{esc(p['payment_method'])}</td>
            <td class="p-3 text-right font-bold text-emerald-700">KSh {float(p['amount']):,.0f}</td>
            <td class="p-3 text-right">
                <a href="/finance/receipt/{school_id}/{p['id']}" target="_blank" class="text-indigo-700 hover:underline text-xs font-bold mr-3">Receipt</a>
                <form action="/api/v1/finance/payment/delete/{school_id}/{p['id']}" method="post" class="inline" onsubmit="return confirm('Remove this payment record? This cannot be undone.');">
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
                <div class="flex items-center justify-between gap-3 flex-wrap">
                    <div>
                        <h2 class="text-lg font-black text-slate-800">{esc(full_student_name(student))}</h2>
                        <p class="text-xs text-slate-400">#{esc(student['admission_number'])} — {esc(student['grade_name'])}</p>
                    </div>
                    <form method="get" class="flex items-center gap-2">
                        <label class="text-xs font-bold text-slate-500">Viewing period:</label>
                        <select onchange="const [t,y]=this.value.split('|'); window.location.href='/finance/student/{school_id}/{student_id}?term='+encodeURIComponent(t)+'&year='+y;" class="border p-2 rounded-lg text-xs bg-white font-semibold">{period_options}</select>
                    </form>
                </div>
                <div class="grid grid-cols-3 gap-3 mt-4">
                    <div class="bg-slate-50 rounded-xl p-3 text-center"><p class="text-[10px] font-bold text-slate-400 uppercase">Fees ({esc(term)} {year})</p><p class="font-black text-slate-800">KSh {(total_fee + total_opening):,.0f}</p></div>
                    <div class="bg-emerald-50 rounded-xl p-3 text-center"><p class="text-[10px] font-bold text-emerald-600 uppercase">Paid ({esc(term)} {year})</p><p class="font-black text-emerald-800">KSh {total_paid:,.0f}</p></div>
                    <div class="bg-rose-50 rounded-xl p-3 text-center"><p class="text-[10px] font-bold text-rose-600 uppercase">Balance</p><p class="font-black text-rose-800">KSh {total_balance:,.0f}</p></div>
                </div>
                {f"<div class='mt-3 pt-3 border-t'>{breakdown_html}</div>" if breakdown_html else ""}
            </div>

            {"<div class='bg-emerald-50 border border-emerald-200 text-emerald-800 text-sm px-4 py-3 rounded-xl'>✅ Payment recorded successfully.</div>" if saved else ""}
            {"<div class='bg-emerald-50 border border-emerald-200 text-emerald-800 text-sm px-4 py-3 rounded-xl'>✅ Brought-forward balance saved.</div>" if request.query_params.get('balance_saved') else ""}

            <details class="bg-white rounded-2xl border shadow-xs">
                <summary class="p-4 cursor-pointer text-sm font-bold text-slate-700 select-none">⚙️ Set Brought-Forward Balance (arrears from before this system)</summary>
                <div class="p-4 pt-0">
                    <p class="text-xs text-slate-400 mb-3">For a student who already owed money — or already had credit from overpaying — before this school started using Elimu Hub. Enter a positive number for arrears owed, or a negative number (e.g. <span class="font-mono">-500</span>) for a pre-existing credit that should reduce what they owe. This is added on top of the normal fee, not a payment.</p>
                    <form action="/api/v1/finance/opening-balance/save/{school_id}/{student_id}" method="post" class="space-y-2">
                        <input type="hidden" name="term" value="{esc(term)}"><input type="hidden" name="year" value="{year}">
                        {"".join(f'''
                        <div class="flex items-center gap-2 flex-wrap">
                            <span class="text-xs font-semibold text-slate-600 w-32 shrink-0">{esc(c["name"])}</span>
                            <span class="text-slate-400 text-xs">KSh</span>
                            <input type="number" name="opening_{c['id']}" value="{opening_by_category.get(c['id'], {}).get('amount', '') or ''}" step="0.01" placeholder="0" class="border p-2 rounded-lg w-32 text-right text-sm">
                            <input type="text" name="note_{c['id']}" value="{esc(opening_by_category.get(c['id'], {}).get('note') or '')}" placeholder="Note (optional)" class="border p-2 rounded-lg flex-1 min-w-[140px] text-xs">
                        </div>
                        ''' for c in categories)}
                        <button type="submit" class="bg-amber-600 hover:bg-amber-700 text-white font-bold py-2.5 px-5 rounded-xl text-sm transition mt-2">Save Brought-Forward Balance</button>
                    </form>
                </div>
            </details>

            <div class="bg-white p-6 rounded-2xl border shadow-xs">
                <h3 class="text-sm font-bold text-slate-800 mb-3">Record a Payment</h3>
                <form action="/api/v1/finance/payment/add/{school_id}/{student_id}" method="post" class="grid grid-cols-2 gap-3">
                    <input type="hidden" name="term" value="{esc(term)}"><input type="hidden" name="year" value="{year}">
                    <div class="col-span-2">
                        <label class="text-xs font-bold text-slate-600 block mb-1">Fee Category</label>
                        <select name="fee_category_id" class="w-full border p-2.5 rounded-xl text-sm bg-white" required>{category_options}</select>
                    </div>
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
                <div class="px-5 py-3 border-b bg-slate-50/60"><h3 class="text-sm font-bold text-slate-800">Payment History (All Terms)</h3></div>
                <table class="w-full text-sm">
                    <thead><tr class="bg-slate-50 text-slate-500 text-xs border-b"><th class="p-3 text-left">Date</th><th class="p-3 text-left">Term</th><th class="p-3 text-left">Category</th><th class="p-3 text-left">Method</th><th class="p-3 text-right">Amount</th><th class="p-3"></th></tr></thead>
                    <tbody>{payments_html or "<tr><td colspan='6' class='p-6 text-center text-slate-400 text-xs italic'>No payments recorded yet.</td></tr>"}</tbody>
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
    fee_category_id: int = Form(None),
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

            category_id = fee_category_id or _ensure_default_category(cur, school_id)
            recorded_by = request.cookies.get("session_user_id")

            cur.execute("""
                INSERT INTO fee_payments (student_id, school_id, fee_category_id, amount, payment_method, reference_note, term, year, recorded_by_user_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id;
            """, (student_id, school_id, category_id, amount, payment_method, reference_note.strip() or None, term, year, recorded_by))
            new_payment_id = cur.fetchone()[0]
            cur.execute("UPDATE fee_payments SET receipt_number = %s WHERE id = %s;", (_receipt_number_from_id(school_id, new_payment_id), new_payment_id))
            conn.commit()

    return RedirectResponse(url=f"/finance/student/{school_id}/{student_id}?term={urllib.parse.quote(term)}&year={year}&saved=1", status_code=303)


@router.post("/api/v1/finance/opening-balance/save/{school_id}/{student_id}")
async def save_opening_balance(school_id: int, student_id: int, request: Request):
    """Admin-only: records a student's brought-forward balance per fee
    category — arrears from before this system was in use. Deliberately
    admin-only, since this directly changes how much a student is
    considered to owe, unlike recording a payment which only reduces it."""
    auth_error = require_admin_session(request, school_id)
    if auth_error:
        return auth_error
    if not FINANCE_MODULE_ENABLED:
        return _coming_soon_page(school_id, request)

    form = await request.form()
    term = (form.get("term") or "").strip()
    try:
        year = int(form.get("year"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid year.")

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM students WHERE id = %s AND school_id = %s;", (student_id, school_id))
            if not cur.fetchone():
                raise HTTPException(status_code=404, detail="Student not found.")

            cur.execute("SELECT id FROM fee_categories WHERE school_id = %s;", (school_id,))
            category_ids = [r[0] for r in cur.fetchall()]

            recorded_by = request.cookies.get("session_user_id")
            for cat_id in category_ids:
                raw_amount = (form.get(f"opening_{cat_id}") or "").strip()
                note = (form.get(f"note_{cat_id}") or "").strip() or None
                if raw_amount == "":
                    # Blank means "no brought-forward balance for this
                    # category" — remove any existing row rather than
                    # leaving a stale amount behind.
                    cur.execute("""
                        DELETE FROM fee_opening_balances
                        WHERE school_id = %s AND student_id = %s AND fee_category_id = %s AND term = %s AND year = %s;
                    """, (school_id, student_id, cat_id, term, year))
                    continue
                try:
                    amount = float(raw_amount)
                except ValueError:
                    continue
                # Negative is allowed and meaningful here — it represents
                # a pre-existing credit (e.g. a student who overpaid at
                # their previous school, or before this school adopted
                # Elimu Hub), not just arrears. It's subtracted from what
                # the student owes exactly the same way an automatically
                # carried-forward credit is (see run_carry_forward_balances).

                # Check-then-update-or-insert rather than ON CONFLICT — same
                # lesson learned earlier with fee_structures: safer than
                # relying on a named constraint that might not exist.
                cur.execute("""
                    SELECT id FROM fee_opening_balances
                    WHERE school_id = %s AND student_id = %s AND fee_category_id = %s AND term = %s AND year = %s;
                """, (school_id, student_id, cat_id, term, year))
                existing_row = cur.fetchone()
                if existing_row:
                    cur.execute("""
                        UPDATE fee_opening_balances SET amount = %s, note = %s, recorded_by_user_id = %s, updated_at = NOW()
                        WHERE id = %s;
                    """, (amount, note, recorded_by, existing_row[0]))
                else:
                    cur.execute("""
                        INSERT INTO fee_opening_balances (school_id, student_id, fee_category_id, term, year, amount, note, recorded_by_user_id)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s);
                    """, (school_id, student_id, cat_id, term, year, amount, note, recorded_by))
            conn.commit()

    return RedirectResponse(url=f"/finance/student/{school_id}/{student_id}?term={urllib.parse.quote(term)}&year={year}&balance_saved=1", status_code=303)


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


# ============================================================
# Receipt generator
# ============================================================

@router.get("/finance/receipt/{school_id}/{payment_id}", response_class=HTMLResponse)
def view_receipt(school_id: int, payment_id: int, request: Request):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error
    if not FINANCE_MODULE_ENABLED:
        return _coming_soon_page(school_id, request)

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM schools WHERE id = %s;", (school_id,))
            school = cur.fetchone()

            cur.execute("""
                SELECT fp.*, fc.name AS category_name, s.first_name, s.middle_name, s.last_name, s.admission_number, c.grade_name,
                       u.full_name AS recorded_by_name
                FROM fee_payments fp
                JOIN students s ON fp.student_id = s.id
                JOIN classes c ON s.class_id = c.id
                LEFT JOIN fee_categories fc ON fp.fee_category_id = fc.id
                LEFT JOIN users u ON fp.recorded_by_user_id = u.id
                WHERE fp.id = %s AND fp.school_id = %s;
            """, (payment_id, school_id))
            payment = cur.fetchone()
            if not payment:
                raise HTTPException(status_code=404, detail="Receipt not found.")

            # Balance remaining AFTER this payment, for this category/term —
            # computed from the same fee + brought-forward - paid formula
            # used everywhere else in the module, so it always agrees with
            # the student's statement page.
            cur.execute("""
                SELECT amount FROM fee_structures
                WHERE school_id = %s AND grade_name = %s AND term = %s AND year = %s
                  AND fee_category_id = %s;
            """, (school_id, payment['grade_name'], payment['term'], payment['year'], payment['fee_category_id']))
            fee_row = cur.fetchone()
            fee_amount = float(fee_row['amount']) if fee_row else 0

            cur.execute("""
                SELECT amount FROM fee_opening_balances
                WHERE school_id = %s AND student_id = %s AND fee_category_id = %s AND term = %s AND year = %s;
            """, (school_id, payment['student_id'], payment['fee_category_id'], payment['term'], payment['year']))
            opening_row = cur.fetchone()
            opening_amount = float(opening_row['amount']) if opening_row else 0

            cur.execute("""
                SELECT COALESCE(SUM(amount), 0) AS total_paid FROM fee_payments
                WHERE student_id = %s AND fee_category_id = %s AND term = %s AND year = %s;
            """, (payment['student_id'], payment['fee_category_id'], payment['term'], payment['year']))
            total_paid_row = cur.fetchone()
            total_paid_to_date = float(total_paid_row['total_paid']) if total_paid_row else 0

            balance_after = fee_amount + opening_amount - total_paid_to_date

    logo_src = school.get('logo_url')
    logo_html = ""
    if logo_src:
        final_src = logo_src if logo_src.startswith("http") else f"/{logo_src.lstrip('/')}"
        logo_html = f"<img src='{final_src}' style='width:70px;height:70px;object-fit:contain;' />"

    from mpesa_routes import render_admin_print_toolbar_and_content
    document_content_html = f"""
        <div style="max-width:420px; margin:0 auto; background:white; border:2px solid #1e1b4b; border-radius:10px; padding:24px;">
            <div style="display:flex; align-items:center; gap:12px; border-bottom:2px double #1e1b4b; padding-bottom:12px; margin-bottom:16px;">
                {logo_html}
                <div>
                    <p style="margin:0; font-weight:900; font-size:16px;">{esc(school['name'])}</p>
                    <p style="margin:2px 0 0; font-size:11px; color:#64748b;">{esc(school.get('physical_address') or '')}</p>
                </div>
            </div>

            <p style="text-align:center; font-weight:900; font-size:14px; letter-spacing:1px; margin:0 0 16px;">OFFICIAL FEE PAYMENT RECEIPT</p>

            <table style="width:100%; font-size:12px; border-collapse:collapse;">
                <tr><td style="padding:4px 0; color:#64748b;">Receipt No.</td><td style="padding:4px 0; text-align:right; font-weight:bold; font-family:monospace;">{esc(payment['receipt_number'] or '—')}</td></tr>
                <tr><td style="padding:4px 0; color:#64748b;">Date</td><td style="padding:4px 0; text-align:right;">{payment['paid_at'].strftime('%d %B %Y, %H:%M') if payment['paid_at'] else ''}</td></tr>
                <tr><td style="padding:4px 0; color:#64748b;">Received From</td><td style="padding:4px 0; text-align:right; font-weight:bold;">{esc(full_student_name(payment))}</td></tr>
                <tr><td style="padding:4px 0; color:#64748b;">Admission No.</td><td style="padding:4px 0; text-align:right;">{esc(payment['admission_number'])}</td></tr>
                <tr><td style="padding:4px 0; color:#64748b;">Class</td><td style="padding:4px 0; text-align:right;">{esc(payment['grade_name'])}</td></tr>
                <tr><td style="padding:4px 0; color:#64748b;">Fee Category</td><td style="padding:4px 0; text-align:right;">{esc(payment['category_name'] or 'School Fees')}</td></tr>
                <tr><td style="padding:4px 0; color:#64748b;">Term</td><td style="padding:4px 0; text-align:right;">{esc(payment['term'])} {payment['year']}</td></tr>
                <tr><td style="padding:4px 0; color:#64748b;">Payment Method</td><td style="padding:4px 0; text-align:right; text-transform:capitalize;">{esc(payment['payment_method'])}</td></tr>
                {f"<tr><td style='padding:4px 0; color:#64748b;'>Reference</td><td style='padding:4px 0; text-align:right;'>{esc(payment['reference_note'])}</td></tr>" if payment['reference_note'] else ""}
            </table>

            <div style="margin-top:16px; padding-top:16px; border-top:2px dashed #cbd5e1; display:flex; justify-content:space-between; align-items:center;">
                <span style="font-weight:bold; font-size:13px;">Amount Paid</span>
                <span style="font-weight:900; font-size:22px; color:#047857;">KSh {float(payment['amount']):,.2f}</span>
            </div>
            <div style="margin-top:8px; display:flex; justify-content:space-between; align-items:center;">
                <span style="font-weight:bold; font-size:12px; color:#64748b;">Balance Remaining ({esc(payment['category_name'] or 'School Fees')})</span>
                <span style="font-weight:800; font-size:15px; color:{'#be123c' if balance_after > 0 else '#047857'};">KSh {balance_after:,.2f}</span>
            </div>

            <p style="margin-top:20px; font-size:10px; color:#94a3b8; text-align:center;">Recorded by {esc(school['name'])} Finance — Elimu Hub</p>
        </div>
    """
    # Deliberately a shorter preview window than other documents (220px vs
    # the usual 480px default) — for a receipt, "hide half" only matters
    # if it actually hides the Amount Paid / Balance figures, which is
    # what someone would actually want from an unpaid-for receipt. A
    # generic half-height crop on a short document like this could still
    # leave the payment amount visible, defeating the point.
    toolbar_button, content_html, extra_style_html = render_admin_print_toolbar_and_content(school_id, document_content_html, "receipt", "#4f46e5", max_height_px=220)

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Elimu Hub | Receipt {esc(payment['receipt_number'] or '')}</title>
        <style>
            @page {{ size: A5 portrait; margin: 12mm; }}
            body {{ font-family: Arial, sans-serif; padding: 24px; color: #1e293b; background:#f1f5f9; }}
            @media print {{ .no-print {{ display: none !important; }} body {{ background: white; padding: 0; }} }}
        </style>
        {extra_style_html}
    </head>
    <body>
        <div class="no-print" style="text-align:right; margin-bottom:16px; max-width:420px; margin-left:auto; margin-right:auto;">
            {toolbar_button}
        </div>
        {content_html}
    </body>
    </html>
    """


# ============================================================
# Staff fee collection — a deliberately SCOPED-DOWN view for teachers who
# collect fees. They can search for a student and record a payment, and
# see only what THEY personally have collected — never the full finance
# module (fee structure, categories, other staff's collections, or any
# delete/edit ability, which stays admin-only via require_admin_session
# on the existing delete and opening-balance routes).
# ============================================================

@router.get("/finance/staff/collect/{school_id}", response_class=HTMLResponse)
def staff_collect_search(school_id: int, request: Request, search: str = None, grade_name: str = None, education_level: str = None, stream: str = None, category_id: int = None):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error
    if not FINANCE_MODULE_ENABLED:
        return _coming_soon_page(school_id, request)

    students = []
    class_list_students = []
    fee_amount = 0
    current_category = None

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            school, active_term, active_year = _get_school_and_settings(cur, school_id)
            default_category_id = _ensure_default_category(cur, school_id)
            conn.commit()

            cur.execute("SELECT * FROM fee_categories WHERE school_id = %s ORDER BY is_default DESC, name ASC;", (school_id,))
            categories = cur.fetchall()

            cur.execute("""
                SELECT DISTINCT c.grade_name, c.education_level, s.stream
                FROM students s JOIN classes c ON s.class_id = c.id
                WHERE s.school_id = %s AND (s.status IS NULL OR s.status != 'GRADUATED')
                ORDER BY c.grade_name ASC, s.stream ASC;
            """, (school_id,))
            classes = cur.fetchall()

            if search and search.strip():
                like = f"%{search.strip()}%"
                cur.execute("""
                    SELECT s.id, s.first_name, s.middle_name, s.last_name, s.admission_number, c.grade_name, s.stream
                    FROM students s JOIN classes c ON s.class_id = c.id
                    WHERE s.school_id = %s AND (s.status IS NULL OR s.status != 'GRADUATED')
                      AND (s.first_name ILIKE %s OR s.last_name ILIKE %s OR s.admission_number ILIKE %s)
                    ORDER BY s.first_name ASC LIMIT 25;
                """, (school_id, like, like, like))
                students = cur.fetchall()

            # Browse mode — a full list of every learner in a class, with
            # their fee status for one category, exactly like the admin's
            # Class Fee List. This is what actually lets staff see who
            # still owes what per category, rather than only being able
            # to look up one student they already have in mind by name.
            if grade_name and education_level and stream:
                category_id = category_id or default_category_id
                current_category = next((c for c in categories if c['id'] == category_id), categories[0] if categories else None)

                cur.execute("""
                    SELECT amount FROM fee_structures
                    WHERE school_id = %s AND fee_category_id = %s AND grade_name = %s AND term = %s AND year = %s;
                """, (school_id, category_id, grade_name, active_term, active_year))
                fee_row = cur.fetchone()
                fee_amount = float(fee_row['amount']) if fee_row else 0

                cur.execute("""
                    SELECT s.id, s.first_name, s.middle_name, s.last_name, s.admission_number,
                           COALESCE((SELECT SUM(fp.amount) FROM fee_payments fp WHERE fp.student_id = s.id AND fp.fee_category_id = %s AND fp.term = %s AND fp.year = %s), 0) AS paid,
                           COALESCE((SELECT amount FROM fee_opening_balances fob WHERE fob.student_id = s.id AND fob.fee_category_id = %s AND fob.term = %s AND fob.year = %s), 0) AS opening_balance
                    FROM students s
                    JOIN classes c ON s.class_id = c.id
                    WHERE s.school_id = %s AND c.grade_name = %s AND c.education_level = %s AND s.stream = %s
                      AND (s.status IS NULL OR s.status != 'GRADUATED')
                    ORDER BY s.admission_number ASC;
                """, (category_id, active_term, active_year, category_id, active_term, active_year, school_id, grade_name, education_level, stream))
                class_list_students = cur.fetchall()

    results_html = "".join(f"""
        <a href="/finance/staff/collect/{school_id}/{s['id']}" class="flex items-center justify-between p-3 border-b last:border-0 hover:bg-slate-50 transition">
            <span class="text-sm font-semibold text-slate-800">{esc(full_student_name(s))} <span class="text-slate-400 font-normal text-xs">#{esc(s['admission_number'])} — {esc(s['grade_name'])} {esc(s['stream'])}</span></span>
            <span class="text-xs text-emerald-700 font-bold">Collect Fee →</span>
        </a>
    """ for s in students)

    class_options = "".join(
        f"""<option value="{esc(c['grade_name'])}|{esc(c['education_level'])}|{esc(c['stream'])}" {'selected' if grade_name == c['grade_name'] and stream == c['stream'] else ''}>{esc(c['grade_name'])}{' — ' + esc(c['stream']) if c['stream'] != 'SINGLE STREAM' else ''}</option>"""
        for c in classes
    )
    category_options = "".join(
        f"<option value='{c['id']}' {'selected' if c['id'] == category_id else ''}>{esc(c['name'])}</option>"
        for c in categories
    )

    class_list_rows_html = ""
    for s in class_list_students:
        paid = float(s['paid'])
        opening = float(s['opening_balance'])
        balance = fee_amount + opening - paid
        status_badge = (
            "<span class='text-[10px] font-bold px-2 py-0.5 rounded-full bg-sky-50 text-sky-700 border border-sky-200'>Credit — KSh " + f"{abs(balance):,.0f}" + "</span>" if balance < 0 else
            "<span class='text-[10px] font-bold px-2 py-0.5 rounded-full bg-emerald-50 text-emerald-700 border border-emerald-200'>Paid in full</span>" if balance == 0 and fee_amount > 0 else
            "<span class='text-[10px] font-bold px-2 py-0.5 rounded-full bg-amber-50 text-amber-700 border border-amber-200'>Balance owing</span>" if balance > 0 else
            "<span class='text-[10px] font-bold px-2 py-0.5 rounded-full bg-slate-100 text-slate-500 border border-slate-200'>No fee set</span>"
        )
        class_list_rows_html += f"""
        <a href="/finance/staff/collect/{school_id}/{s['id']}" class="flex items-center justify-between p-3 border-b last:border-0 hover:bg-slate-50 transition flex-wrap gap-1">
            <div>
                <span class="text-sm font-semibold text-slate-800">{esc(full_student_name(s))}</span>
                <span class="text-slate-400 font-normal text-xs block">#{esc(s['admission_number'])} — Balance: KSh {balance:,.0f}</span>
            </div>
            <div class="flex items-center gap-2">
                {status_badge}
                <span class="text-xs text-emerald-700 font-bold">Collect →</span>
            </div>
        </a>
        """

    return f"""
    <!DOCTYPE html>
    <html>
    <head><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Elimu Hub | Collect Fees</title><script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script></head>
    <body class="bg-[#F7F9F8] min-h-screen p-4 sm:p-8">
        <div class="max-w-lg mx-auto space-y-4">
            <div class="bg-white p-6 rounded-2xl border shadow-xs">
                <h2 class="text-lg font-black text-slate-800">💰 Collect Fees</h2>
                <p class="text-xs text-slate-400 mt-1">Search for a student by name, or browse a full class list by fee category below.</p>
                <form method="get" class="mt-3">
                    <input type="text" name="search" value="{esc(search or '')}" placeholder="Search by name or admission number..." class="w-full border p-3 rounded-xl text-sm" autofocus>
                </form>
            </div>

            {f'''<div class="bg-white rounded-2xl border shadow-xs overflow-hidden">{results_html or "<p class='p-4 text-slate-400 text-xs italic'>No students found.</p>"}</div>''' if search else ""}

            <div class="bg-white p-6 rounded-2xl border shadow-xs">
                <h3 class="text-sm font-bold text-slate-800 mb-3">📋 Browse by Class &amp; Category</h3>
                <form method="get" class="grid grid-cols-2 gap-2">
                    <select name="class_pick" onchange="const [g,l,s]=this.value.split('|'); document.getElementById('gn').value=g; document.getElementById('el').value=l; document.getElementById('st').value=s; this.form.submit();" class="col-span-2 border p-2.5 rounded-xl text-sm bg-white">
                        <option value="">— Select a class —</option>{class_options}
                    </select>
                    <input type="hidden" id="gn" name="grade_name" value="{esc(grade_name or '')}">
                    <input type="hidden" id="el" name="education_level" value="{esc(education_level or '')}">
                    <input type="hidden" id="st" name="stream" value="{esc(stream or '')}">
                    <select name="category_id" onchange="this.form.submit()" class="col-span-2 border p-2.5 rounded-xl text-sm bg-white">{category_options}</select>
                </form>
            </div>

            {f'''<div class="bg-white rounded-2xl border shadow-xs overflow-hidden">
                <div class="px-4 py-3 border-b bg-slate-50/60 flex items-center justify-between">
                    <h3 class="text-sm font-bold text-slate-800">{esc(grade_name)}{" — " + esc(stream) if stream != "SINGLE STREAM" else ""} — {esc(current_category["name"]) if current_category else ""}</h3>
                    <span class="text-xs text-slate-400">Fee: KSh {fee_amount:,.0f}</span>
                </div>
                {class_list_rows_html or "<p class='p-4 text-slate-400 text-xs italic'>No students in this class.</p>"}
            </div>''' if grade_name else ""}

            <a href="/finance/staff/my-collections/{school_id}" class="bg-white hover:bg-slate-50 text-slate-600 border border-slate-200 font-bold py-2.5 px-5 rounded-xl text-sm transition inline-block">📋 View My Collections</a>
        </div>
    </body>
    </html>
    """


@router.get("/finance/staff/collect/{school_id}/{student_id}", response_class=HTMLResponse)
def staff_collect_form(school_id: int, student_id: int, request: Request, saved: str = None):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error
    if not FINANCE_MODULE_ENABLED:
        return _coming_soon_page(school_id, request)

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            school, active_term, active_year = _get_school_and_settings(cur, school_id)
            _ensure_default_category(cur, school_id)
            conn.commit()

            cur.execute("""
                SELECT s.*, c.grade_name FROM students s JOIN classes c ON s.class_id = c.id
                WHERE s.id = %s AND s.school_id = %s;
            """, (student_id, school_id))
            student = cur.fetchone()
            if not student:
                raise HTTPException(status_code=404, detail="Student not found.")

            cur.execute("SELECT * FROM fee_categories WHERE school_id = %s ORDER BY is_default DESC, name ASC;", (school_id,))
            categories = cur.fetchall()

            # Only THIS staff member's own collections for this student —
            # never anyone else's, and no delete/edit link, matching the
            # "must not be able to delete/edit" requirement exactly.
            recorded_by = request.cookies.get("session_user_id")
            cur.execute("""
                SELECT fp.*, fc.name AS category_name FROM fee_payments fp
                LEFT JOIN fee_categories fc ON fp.fee_category_id = fc.id
                WHERE fp.student_id = %s AND fp.recorded_by_user_id = %s
                ORDER BY fp.paid_at DESC LIMIT 10;
            """, (student_id, recorded_by))
            my_payments_for_student = cur.fetchall()

    category_options = "".join(f"<option value='{c['id']}'>{esc(c['name'])}</option>" for c in categories)

    history_html = "".join(f"""
        <tr class="border-b border-slate-50">
            <td class="p-2 text-xs text-slate-400">{p['paid_at'].strftime('%d %b %Y') if p['paid_at'] else ''}</td>
            <td class="p-2 text-xs font-semibold text-indigo-700">{esc(p['category_name'] or 'School Fees')}</td>
            <td class="p-2 text-xs capitalize">{esc(p['payment_method'])}</td>
            <td class="p-2 text-right text-xs font-bold text-emerald-700">KSh {float(p['amount']):,.0f}</td>
            <td class="p-2 text-right"><a href="/finance/receipt/{school_id}/{p['id']}" target="_blank" class="text-indigo-700 hover:underline text-xs font-bold">Receipt</a></td>
        </tr>
    """ for p in my_payments_for_student)

    return f"""
    <!DOCTYPE html>
    <html>
    <head><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Elimu Hub | Collect Fee — {esc(full_student_name(student))}</title><script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script></head>
    <body class="bg-[#F7F9F8] min-h-screen p-4 sm:p-8">
        <div class="max-w-lg mx-auto space-y-4">
            <div class="bg-white p-6 rounded-2xl border shadow-xs">
                <h2 class="text-lg font-black text-slate-800">{esc(full_student_name(student))}</h2>
                <p class="text-xs text-slate-400">#{esc(student['admission_number'])} — {esc(student['grade_name'])}</p>
            </div>

            {"<div class='bg-emerald-50 border border-emerald-200 text-emerald-800 text-sm px-4 py-3 rounded-xl'>✅ Payment recorded successfully.</div>" if saved else ""}

            <div class="bg-white p-6 rounded-2xl border shadow-xs">
                <h3 class="text-sm font-bold text-slate-800 mb-3">Record a Payment</h3>
                <form action="/api/v1/finance/staff/collect/{school_id}/{student_id}" method="post" class="grid grid-cols-2 gap-3">
                    <input type="hidden" name="term" value="{esc(active_term)}"><input type="hidden" name="year" value="{active_year}">
                    <div class="col-span-2">
                        <label class="text-xs font-bold text-slate-600 block mb-1">Fee Category</label>
                        <select name="fee_category_id" class="w-full border p-2.5 rounded-xl text-sm bg-white" required>{category_options}</select>
                    </div>
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
                        <input type="text" name="reference_note" placeholder="e.g. M-Pesa code" class="w-full border p-2.5 rounded-xl text-sm">
                    </div>
                    <button type="submit" class="col-span-2 bg-emerald-700 hover:bg-emerald-800 text-white font-bold py-3 rounded-xl text-sm transition">+ Record Payment</button>
                </form>
            </div>

            {f'''<div class="bg-white rounded-2xl border shadow-xs overflow-hidden">
                <div class="px-5 py-3 border-b bg-slate-50/60"><h3 class="text-sm font-bold text-slate-800">Your Recent Collections for This Student</h3></div>
                <table class="w-full"><tbody>{history_html}</tbody></table>
            </div>''' if my_payments_for_student else ""}

            <a href="/finance/staff/collect/{school_id}" class="bg-slate-200 hover:bg-slate-300 text-slate-700 font-bold py-2.5 px-5 rounded-xl text-sm transition inline-block">← Search Another Student</a>
        </div>
    </body>
    </html>
    """


@router.post("/api/v1/finance/staff/collect/{school_id}/{student_id}")
def staff_collect_payment(
    school_id: int, student_id: int, request: Request,
    amount: float = Form(...), payment_method: str = Form("cash"),
    reference_note: str = Form(""), term: str = Form(...), year: int = Form(...),
    fee_category_id: int = Form(None),
):
    """Records a payment via the staff-scoped collection flow — same
    underlying insert as the admin payment form, just redirects back to
    the staff view instead of the full admin finance module."""
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

            category_id = fee_category_id or _ensure_default_category(cur, school_id)
            recorded_by = request.cookies.get("session_user_id")

            cur.execute("""
                INSERT INTO fee_payments (student_id, school_id, fee_category_id, amount, payment_method, reference_note, term, year, recorded_by_user_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id;
            """, (student_id, school_id, category_id, amount, payment_method, reference_note.strip() or None, term, year, recorded_by))
            new_payment_id = cur.fetchone()[0]
            cur.execute("UPDATE fee_payments SET receipt_number = %s WHERE id = %s;", (_receipt_number_from_id(school_id, new_payment_id), new_payment_id))
            conn.commit()

    return RedirectResponse(url=f"/finance/staff/collect/{school_id}/{student_id}?saved=1", status_code=303)


@router.get("/finance/staff/my-collections/{school_id}", response_class=HTMLResponse)
def staff_my_collections(school_id: int, request: Request):
    """Every payment THIS staff member has personally recorded — read-only,
    no delete/edit links anywhere on this page. That's a deliberate
    admin-only capability, enforced by require_admin_session on the
    existing delete route regardless of what's shown here."""
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error
    if not FINANCE_MODULE_ENABLED:
        return _coming_soon_page(school_id, request)

    recorded_by = request.cookies.get("session_user_id")
    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            school, _, _ = _get_school_and_settings(cur, school_id)

            cur.execute("""
                SELECT fp.*, fc.name AS category_name, s.first_name, s.middle_name, s.last_name, s.admission_number
                FROM fee_payments fp
                JOIN students s ON fp.student_id = s.id
                LEFT JOIN fee_categories fc ON fp.fee_category_id = fc.id
                WHERE fp.school_id = %s AND fp.recorded_by_user_id = %s
                ORDER BY fp.paid_at DESC LIMIT 200;
            """, (school_id, recorded_by))
            payments = cur.fetchall()

    total_collected = sum(float(p['amount']) for p in payments)

    rows_html = "".join(f"""
        <tr class="border-b border-slate-50">
            <td class="p-3 text-xs text-slate-400">{p['paid_at'].strftime('%d %b %Y, %H:%M') if p['paid_at'] else ''}</td>
            <td class="p-3 text-sm font-semibold text-slate-700">{esc(full_student_name(p))} <span class="text-slate-400 font-normal">#{esc(p['admission_number'])}</span></td>
            <td class="p-3 text-xs font-semibold text-indigo-700">{esc(p['category_name'] or 'School Fees')}</td>
            <td class="p-3 text-xs capitalize">{esc(p['payment_method'])}</td>
            <td class="p-3 text-right font-bold text-emerald-700">KSh {float(p['amount']):,.0f}</td>
            <td class="p-3 text-right"><a href="/finance/receipt/{school_id}/{p['id']}" target="_blank" class="text-indigo-700 hover:underline text-xs font-bold">Receipt</a></td>
        </tr>
    """ for p in payments)

    return f"""
    <!DOCTYPE html>
    <html>
    <head><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Elimu Hub | My Fee Collections</title><script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script></head>
    <body class="bg-[#F7F9F8] min-h-screen">
        <header class="bg-white border-b px-6 sm:px-8 py-4 flex flex-col sm:flex-row sm:justify-between sm:items-center gap-2">
            <div>
                <h1 class="text-base font-bold text-slate-900">📋 My Fee Collections — {esc(school['name'])}</h1>
                <p class="text-xs text-slate-400">{len(payments)} payment(s) recorded by you, totalling KSh {total_collected:,.0f}.</p>
            </div>
            <a href="/finance/staff/collect/{school_id}" class="bg-emerald-700 hover:bg-emerald-800 text-white px-4 py-2 rounded-xl text-xs font-bold transition">+ Collect a Fee</a>
        </header>
        <div class="p-4 sm:p-8 max-w-4xl mx-auto">
            <div class="bg-white rounded-2xl border shadow-xs overflow-hidden">
                <table class="w-full text-sm">
                    <thead><tr class="bg-slate-50 text-slate-500 text-xs border-b"><th class="p-3 text-left">Date</th><th class="p-3 text-left">Student</th><th class="p-3 text-left">Category</th><th class="p-3 text-left">Method</th><th class="p-3 text-right">Amount</th><th class="p-3"></th></tr></thead>
                    <tbody>{rows_html or "<tr><td colspan='6' class='p-8 text-center text-slate-400 text-xs italic'>You haven't recorded any fee collections yet.</td></tr>"}</tbody>
                </table>
            </div>
        </div>
    </body>
    </html>
    """


# ============================================================
# Historical backlog import — bring in past payments schools have
# been tracking in Excel, so balances are accurate from day one
# instead of everyone appearing to owe their full fee.
# ============================================================

IMPORT_CSV_COLUMNS = ["admission_number", "fee_category", "term", "year", "amount", "payment_method", "payment_date", "reference_note"]


@router.get("/finance/import/{school_id}", response_class=HTMLResponse)
def finance_import_view(school_id: int, request: Request, result: str = None):
    auth_error = require_admin_session(request, school_id)
    if auth_error:
        return auth_error
    if not FINANCE_MODULE_ENABLED:
        return _coming_soon_page(school_id, request)

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            school, _, _ = _get_school_and_settings(cur, school_id)
            _ensure_default_category(cur, school_id)
            conn.commit()

            cur.execute("SELECT name FROM fee_categories WHERE school_id = %s ORDER BY is_default DESC, name ASC;", (school_id,))
            categories = [r['name'] for r in cur.fetchall()]

            cur.execute("""
                SELECT fpi.*, u.full_name AS imported_by_name
                FROM fee_payment_imports fpi LEFT JOIN users u ON fpi.imported_by_user_id = u.id
                WHERE fpi.school_id = %s ORDER BY fpi.imported_at DESC LIMIT 20;
            """, (school_id,))
            past_imports = cur.fetchall()

    imports_html = "".join(f"""
        <div class="flex items-center justify-between py-2.5 border-b border-slate-50 last:border-0">
            <div>
                <p class="text-sm font-semibold text-slate-700">{esc(imp['filename'] or 'Untitled file')}</p>
                <p class="text-[11px] text-slate-400">{imp['imported_at'].strftime('%d %b %Y, %H:%M') if imp['imported_at'] else ''} by {esc(imp['imported_by_name'] or 'Unknown')} — {imp['row_count']} imported, {imp['skipped_count']} skipped</p>
            </div>
            <form action="/api/v1/finance/import/reverse/{school_id}/{imp['id']}" method="post" onsubmit="return confirm('Undo this entire import batch? This permanently removes all {imp['row_count']} payment(s) it added. This cannot be undone.');">
                <button type="submit" class="text-rose-600 hover:text-rose-800 text-xs font-bold">Undo Batch</button>
            </form>
        </div>
    """ for imp in past_imports)

    result_banner = ""
    if result:
        parts = result.split(":", 2)
        if len(parts) == 3:
            imported_n, skipped_n, errors_b64 = parts
            import base64
            try:
                error_lines = base64.b64decode(errors_b64).decode("utf-8").split("\n") if errors_b64 else []
            except Exception:
                error_lines = []
            error_list_html = "".join(f"<li>{esc(line)}</li>" for line in error_lines if line)
            result_banner = f"""
            <div class="bg-{'emerald' if int(skipped_n) == 0 else 'amber'}-50 border border-{'emerald' if int(skipped_n) == 0 else 'amber'}-200 rounded-xl p-4">
                <p class="text-sm font-bold text-slate-800">✅ {imported_n} payment(s) imported successfully{f', ⚠️ {skipped_n} row(s) skipped' if int(skipped_n) > 0 else ''}.</p>
                {f"<ul class='text-xs text-amber-800 mt-2 list-disc list-inside space-y-0.5'>{error_list_html}</ul>" if error_list_html else ""}
            </div>
            """

    return f"""
    <!DOCTYPE html>
    <html>
    <head><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Elimu Hub | Import Fee History</title><script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script></head>
    <body class="bg-[#F7F9F8] min-h-screen p-4 sm:p-8">
        <div class="max-w-2xl mx-auto space-y-4">
            <div class="bg-white p-6 rounded-2xl border shadow-xs">
                <h2 class="text-lg font-black text-slate-800">📥 Import Fee History</h2>
                <p class="text-xs text-slate-400 mt-1">Bring in payments a student already made before you started using this system (e.g. from an Excel sheet) — so their balance here is accurate from day one, instead of showing they owe their full fee.</p>
            </div>

            {result_banner}

            <div class="bg-white p-6 rounded-2xl border shadow-xs">
                <h3 class="text-sm font-bold text-slate-800 mb-2">Step 1 — Download the template</h3>
                <p class="text-xs text-slate-500 mb-3">Fill this in from your existing Excel sheet, one row per historical payment. In Excel: <b>File → Save As → CSV (Comma delimited)</b>.</p>
                <a href="/finance/import/template/{school_id}" class="bg-slate-100 hover:bg-slate-200 text-slate-700 font-bold px-4 py-2.5 rounded-xl text-sm transition inline-block">⬇ Download CSV Template</a>
                <div class="mt-3 text-xs text-slate-500 bg-slate-50 rounded-lg p-3">
                    <p class="font-bold text-slate-600 mb-1">Columns:</p>
                    <p><b>admission_number</b> — must match an existing student</p>
                    <p><b>fee_category</b> — one of: {esc(', '.join(categories))}</p>
                    <p><b>term</b> — e.g. Term 1, Term 2, Term 3</p>
                    <p><b>year</b> — e.g. 2024</p>
                    <p><b>amount</b> — the amount that was paid</p>
                    <p><b>payment_method</b> — cash / bank / mpesa / other (optional, defaults to cash)</p>
                    <p><b>payment_date</b> — YYYY-MM-DD (optional, defaults to today)</p>
                    <p><b>reference_note</b> — optional free text</p>
                </div>
            </div>

            <div class="bg-white p-6 rounded-2xl border shadow-xs">
                <h3 class="text-sm font-bold text-slate-800 mb-3">Step 2 — Upload your filled-in CSV</h3>
                <form action="/api/v1/finance/import/upload/{school_id}" method="post" enctype="multipart/form-data" class="space-y-3">
                    <input type="file" name="file" accept=".csv" required class="w-full border p-2.5 rounded-xl text-sm bg-white">
                    <button type="submit" class="w-full bg-indigo-800 hover:bg-indigo-900 text-white font-bold py-3 rounded-xl text-sm transition">Import Payments</button>
                </form>
                <p class="text-[11px] text-slate-400 mt-2">Every row that can't be matched or validated is skipped and listed afterward — nothing partial or guessed gets silently imported.</p>
            </div>

            <div class="bg-white rounded-2xl border shadow-xs p-6">
                <h3 class="text-sm font-bold text-slate-800 mb-2">Past Imports</h3>
                {imports_html or "<p class='text-slate-400 text-xs italic py-2'>No imports done yet.</p>"}
            </div>

            <a href="/finance/dashboard/{school_id}" class="bg-slate-200 hover:bg-slate-300 text-slate-700 font-bold py-2.5 px-5 rounded-xl text-sm transition inline-block">← Back to Finance</a>
        </div>
    </body>
    </html>
    """


@router.get("/finance/import/template/{school_id}")
def download_import_template(school_id: int, request: Request):
    auth_error = require_admin_session(request, school_id)
    if auth_error:
        return auth_error
    if not FINANCE_MODULE_ENABLED:
        return _coming_soon_page(school_id, request)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(IMPORT_CSV_COLUMNS)
    writer.writerow(["1001", "School Fees", "Term 1", "2025", "15000", "cash", "2025-01-15", "Term 1 opening balance"])
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=fee_import_template.csv"},
    )


@router.post("/api/v1/finance/import/upload/{school_id}")
async def upload_fee_import(school_id: int, request: Request, file: UploadFile = File(...)):
    auth_error = require_admin_session(request, school_id)
    if auth_error:
        return auth_error
    if not FINANCE_MODULE_ENABLED:
        return _coming_soon_page(school_id, request)

    raw_bytes = await file.read()
    try:
        text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw_bytes.decode("latin-1")

    reader = csv.DictReader(io.StringIO(text))
    missing_cols = [c for c in ["admission_number", "term", "year", "amount"] if c not in (reader.fieldnames or [])]
    if missing_cols:
        raise HTTPException(status_code=400, detail=f"This CSV is missing required column(s): {', '.join(missing_cols)}. Download the template and check your headers match exactly.")

    imported_rows = []
    errors = []
    recorded_by = request.cookies.get("session_user_id")

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            default_category_id = _ensure_default_category(cur, school_id)
            conn.commit()

            cur.execute("SELECT id, name FROM fee_categories WHERE school_id = %s;", (school_id,))
            category_by_name = {r['name'].strip().lower(): r['id'] for r in cur.fetchall()}

            cur.execute("SELECT id, admission_number FROM students WHERE school_id = %s;", (school_id,))
            student_by_adm = {r['admission_number'].strip().lower(): r['id'] for r in cur.fetchall()}

            for i, row in enumerate(reader, start=2):  # row 1 is the header
                adm = (row.get("admission_number") or "").strip()
                if not adm:
                    errors.append(f"Row {i}: missing admission_number — skipped.")
                    continue
                student_id = student_by_adm.get(adm.lower())
                if not student_id:
                    errors.append(f"Row {i}: no student found with admission number '{adm}' — skipped.")
                    continue

                category_name = (row.get("fee_category") or "School Fees").strip()
                category_id = category_by_name.get(category_name.lower(), default_category_id if not category_name else None)
                if category_id is None:
                    errors.append(f"Row {i}: fee category '{category_name}' doesn't exist for this school — skipped. Add it under Fee Categories first, or leave blank for School Fees.")
                    continue

                term = (row.get("term") or "").strip()
                if not term:
                    errors.append(f"Row {i}: missing term — skipped.")
                    continue

                try:
                    year = int((row.get("year") or "").strip())
                except ValueError:
                    errors.append(f"Row {i}: '{row.get('year')}' is not a valid year — skipped.")
                    continue

                try:
                    amount = float((row.get("amount") or "").strip())
                    if amount <= 0:
                        raise ValueError()
                except ValueError:
                    errors.append(f"Row {i}: '{row.get('amount')}' is not a valid positive amount — skipped.")
                    continue

                payment_method = (row.get("payment_method") or "cash").strip().lower()
                if payment_method not in ("cash", "bank", "mpesa", "other"):
                    payment_method = "other"

                payment_date_raw = (row.get("payment_date") or "").strip()
                paid_at = None
                if payment_date_raw:
                    try:
                        paid_at = datetime.strptime(payment_date_raw, "%Y-%m-%d")
                    except ValueError:
                        errors.append(f"Row {i}: date '{payment_date_raw}' isn't in YYYY-MM-DD format — used today's date instead.")

                reference_note = (row.get("reference_note") or "").strip() or "Imported from historical records"

                imported_rows.append({
                    "student_id": student_id, "category_id": category_id, "term": term, "year": year,
                    "amount": amount, "payment_method": payment_method, "paid_at": paid_at, "reference_note": reference_note,
                })

            if imported_rows:
                cur.execute("""
                    INSERT INTO fee_payment_imports (school_id, filename, imported_by_user_id, row_count, skipped_count)
                    VALUES (%s, %s, %s, %s, %s) RETURNING id;
                """, (school_id, file.filename, recorded_by, len(imported_rows), len(errors)))
                batch_id = cur.fetchone()['id']

                for row in imported_rows:
                    if row["paid_at"]:
                        cur.execute("""
                            INSERT INTO fee_payments (student_id, school_id, fee_category_id, amount, payment_method, reference_note, term, year, recorded_by_user_id, paid_at, import_batch_id)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id;
                        """, (row["student_id"], school_id, row["category_id"], row["amount"], row["payment_method"], row["reference_note"], row["term"], row["year"], recorded_by, row["paid_at"], batch_id))
                    else:
                        cur.execute("""
                            INSERT INTO fee_payments (student_id, school_id, fee_category_id, amount, payment_method, reference_note, term, year, recorded_by_user_id, import_batch_id)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id;
                        """, (row["student_id"], school_id, row["category_id"], row["amount"], row["payment_method"], row["reference_note"], row["term"], row["year"], recorded_by, batch_id))
                    new_payment_id = cur.fetchone()['id']
                    cur.execute("UPDATE fee_payments SET receipt_number = %s WHERE id = %s;", (_receipt_number_from_id(school_id, new_payment_id), new_payment_id))
                conn.commit()

    import base64
    errors_b64 = base64.b64encode("\n".join(errors[:50]).encode("utf-8")).decode("ascii") if errors else ""
    result_param = f"{len(imported_rows)}:{len(errors)}:{errors_b64}"
    return RedirectResponse(url=f"/finance/import/{school_id}?result={urllib.parse.quote(result_param)}", status_code=303)


@router.post("/api/v1/finance/import/reverse/{school_id}/{batch_id}")
def reverse_fee_import(school_id: int, batch_id: int, request: Request):
    auth_error = require_admin_session(request, school_id)
    if auth_error:
        return auth_error
    if not FINANCE_MODULE_ENABLED:
        return _coming_soon_page(school_id, request)

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            # Deletes every payment tagged with this batch (cascades from
            # fee_payment_imports), then the batch record itself — a clean,
            # complete undo of exactly what that one import added, leaving
            # every other payment (imported or manually entered) untouched.
            cur.execute("DELETE FROM fee_payment_imports WHERE id = %s AND school_id = %s;", (batch_id, school_id))
            conn.commit()

    return RedirectResponse(url=f"/finance/import/{school_id}", status_code=303)
