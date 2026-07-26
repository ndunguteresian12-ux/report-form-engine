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
import urllib.parse
from datetime import datetime
from fastapi import APIRouter, Request, HTTPException, Form, File, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

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
            try:
                cur.execute("ALTER TABLE fee_structures DROP CONSTRAINT IF EXISTS fee_structures_school_id_grade_name_term_year_key;")
                cur.execute("ALTER TABLE fee_structures ADD CONSTRAINT fee_structures_category_grade_term_year_key UNIQUE (school_id, fee_category_id, grade_name, term, year);")
            except Exception:
                conn.rollback()
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


def _generate_receipt_number(cur, school_id: int) -> str:
    """A simple, human-friendly sequential receipt number per school, e.g.
    RCT-000123. Not globally unique by design — scoped per school, which is
    all that matters since each school only ever sees its own receipts."""
    cur.execute("SELECT COUNT(*) FROM fee_payments WHERE school_id = %s;", (school_id,))
    count_row = cur.fetchone()
    count = (count_row[0] if not isinstance(count_row, dict) else count_row['count']) or 0
    return f"RCT-{school_id:03d}-{count + 1:06d}"


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

            cur.execute("SELECT DISTINCT grade_name, education_level FROM classes ORDER BY id ASC;")
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

            cur.execute("SELECT DISTINCT grade_name, education_level FROM classes ORDER BY id ASC;")
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
                cur.execute("""
                    INSERT INTO fee_structures (school_id, fee_category_id, grade_name, education_level, term, year, amount)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (school_id, fee_category_id, grade_name, term, year) DO UPDATE SET amount = EXCLUDED.amount;
                """, (school_id, category_id, g['grade_name'], g['education_level'], term, year, amount))
            conn.commit()

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
                       COALESCE((SELECT SUM(fp.amount) FROM fee_payments fp WHERE fp.student_id = s.id AND fp.fee_category_id = %s AND fp.term = %s AND fp.year = %s), 0) AS paid
                FROM students s
                JOIN classes c ON s.class_id = c.id
                WHERE s.school_id = %s AND c.grade_name = %s AND c.education_level = %s AND s.stream = %s
                  AND (s.status IS NULL OR s.status != 'GRADUATED')
                ORDER BY s.admission_number ASC;
            """, (category_id, term, year, school_id, grade_name, education_level, stream))
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
                <p class="text-xs text-slate-400">{esc(term)} {year} — {esc(current_category['name']) if current_category else ''}: KSh {fee_amount:,.0f} per student</p>
            </div>
            <a href="/finance/dashboard/{school_id}" class="bg-slate-800 hover:bg-slate-900 text-white px-4 py-2 rounded-xl text-xs font-bold transition">← Back to Finance</a>
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
    total_paid = sum(paid_by_category.values())
    total_balance = total_fee - total_paid

    category_options = "".join(f"<option value='{c['id']}'>{esc(c['name'])}</option>" for c in categories)

    period_options = "".join(
        f"<option value='{esc(p['term'])}|{p['year']}' {'selected' if p['term'] == term and p['year'] == year else ''}>{esc(p['term'])} {p['year']}</option>"
        for p in available_periods
    )

    breakdown_html = ""
    for c in categories:
        fee = fee_by_category.get(c['id'], 0)
        paid = paid_by_category.get(c['id'], 0)
        bal = fee - paid
        if fee == 0 and paid == 0:
            continue  # skip categories entirely irrelevant to this student
        breakdown_html += f"""
        <div class="flex items-center justify-between py-2 border-b border-slate-50 last:border-0 text-sm">
            <span class="font-semibold text-slate-700">{esc(c['name'])}</span>
            <span class="text-slate-400">Fee: KSh {fee:,.0f}</span>
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
                    <div class="bg-slate-50 rounded-xl p-3 text-center"><p class="text-[10px] font-bold text-slate-400 uppercase">Fees ({esc(term)} {year})</p><p class="font-black text-slate-800">KSh {total_fee:,.0f}</p></div>
                    <div class="bg-emerald-50 rounded-xl p-3 text-center"><p class="text-[10px] font-bold text-emerald-600 uppercase">Paid ({esc(term)} {year})</p><p class="font-black text-emerald-800">KSh {total_paid:,.0f}</p></div>
                    <div class="bg-rose-50 rounded-xl p-3 text-center"><p class="text-[10px] font-bold text-rose-600 uppercase">Balance</p><p class="font-black text-rose-800">KSh {total_balance:,.0f}</p></div>
                </div>
                {f"<div class='mt-3 pt-3 border-t'>{breakdown_html}</div>" if breakdown_html else ""}
            </div>

            {"<div class='bg-emerald-50 border border-emerald-200 text-emerald-800 text-sm px-4 py-3 rounded-xl'>✅ Payment recorded successfully.</div>" if saved else ""}

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
            receipt_number = _generate_receipt_number(cur, school_id)

            cur.execute("""
                INSERT INTO fee_payments (student_id, school_id, fee_category_id, amount, payment_method, reference_note, receipt_number, term, year, recorded_by_user_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
            """, (student_id, school_id, category_id, amount, payment_method, reference_note.strip() or None, receipt_number, term, year, recorded_by))
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

    logo_src = school.get('logo_url')
    logo_html = ""
    if logo_src:
        final_src = logo_src if logo_src.startswith("http") else f"/{logo_src.lstrip('/')}"
        logo_html = f"<img src='{final_src}' style='width:70px;height:70px;object-fit:contain;' />"

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
    </head>
    <body>
        <div class="no-print" style="text-align:right; margin-bottom:16px; max-width:420px; margin-left:auto; margin-right:auto;">
            <button onclick="window.print()" style="background:#4f46e5;color:white;border:none;padding:10px 18px;border-radius:8px;font-weight:bold;cursor:pointer;">🖨 Print / Save as PDF</button>
        </div>
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

            <p style="margin-top:20px; font-size:10px; color:#94a3b8; text-align:center;">Recorded by {esc(payment['recorded_by_name'] or 'Staff')} — Elimu Hub Finance</p>
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
                    receipt_number = _generate_receipt_number(cur, school_id)
                    if row["paid_at"]:
                        cur.execute("""
                            INSERT INTO fee_payments (student_id, school_id, fee_category_id, amount, payment_method, reference_note, receipt_number, term, year, recorded_by_user_id, paid_at, import_batch_id)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
                        """, (row["student_id"], school_id, row["category_id"], row["amount"], row["payment_method"], row["reference_note"], receipt_number, row["term"], row["year"], recorded_by, row["paid_at"], batch_id))
                    else:
                        cur.execute("""
                            INSERT INTO fee_payments (student_id, school_id, fee_category_id, amount, payment_method, reference_note, receipt_number, term, year, recorded_by_user_id, import_batch_id)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
                        """, (row["student_id"], school_id, row["category_id"], row["amount"], row["payment_method"], row["reference_note"], receipt_number, row["term"], row["year"], recorded_by, batch_id))
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
