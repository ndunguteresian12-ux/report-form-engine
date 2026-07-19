"""
timetable_routes.py — the timetabling module, extracted out of main.py.

Owns its own three tables (timetable_periods, teacher_subject_assignments,
timetable_slots) and every /timetable* and /api/v1/timetable* route.
main.py just does:

    from timetable_routes import router as timetable_router, bootstrap_timetable_schema
    bootstrap_timetable_schema()
    app.include_router(timetable_router)

All shared plumbing (DB pool, auth checks, subject ordering) comes from
shared.py rather than from main.py, to avoid a circular import.

Timetables are per-STREAM (e.g. "Grade 6 — Stream N" and "Grade 6 — Stream L"
each get their own independent schedule) — for single-stream schools/classes,
the stream value is the literal string "SINGLE STREAM", consistent with the
convention used everywhere else in the app.
"""

import urllib.parse
from fastapi import APIRouter, Request, HTTPException, Form
from fastapi.responses import HTMLResponse, RedirectResponse

from shared import (
    esc,
    get_db_connection,
    RealDictCursor,
    require_school_session,
    get_dashboard_url,
    sort_subjects_for_display,
    abbreviate_subject,
)

router = APIRouter()


def bootstrap_timetable_schema():
    """Creates this module's tables if they don't exist yet. Called once at
    app startup, alongside main.py's own bootstrap_database_schema()."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS timetable_periods (
                    id SERIAL PRIMARY KEY,
                    school_id INTEGER REFERENCES schools(id) ON DELETE CASCADE,
                    period_order INTEGER NOT NULL,
                    label VARCHAR(50) NOT NULL,
                    short_label VARCHAR(20),
                    start_time VARCHAR(20),
                    end_time VARCHAR(20),
                    is_teaching_period BOOLEAN DEFAULT TRUE,
                    UNIQUE(school_id, period_order)
                );
            """)
            cur.execute("ALTER TABLE timetable_periods ADD COLUMN IF NOT EXISTS short_label VARCHAR(20);")

            cur.execute("""
                CREATE TABLE IF NOT EXISTS timetable_settings (
                    school_id INTEGER PRIMARY KEY REFERENCES schools(id) ON DELETE CASCADE,
                    days_per_week INTEGER NOT NULL DEFAULT 5
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS teacher_subject_assignments (
                    id SERIAL PRIMARY KEY,
                    school_id INTEGER REFERENCES schools(id) ON DELETE CASCADE,
                    staff_user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                    learning_area_id INTEGER REFERENCES learning_areas(id) ON DELETE CASCADE,
                    grade_name VARCHAR(100) NOT NULL,
                    education_level VARCHAR(100) NOT NULL,
                    stream VARCHAR(50) NOT NULL DEFAULT 'SINGLE STREAM',
                    UNIQUE(school_id, learning_area_id, grade_name, education_level, stream)
                );
            """)
            # Safe migration for this table if it already existed (pre-stream) in production.
            cur.execute("ALTER TABLE teacher_subject_assignments ADD COLUMN IF NOT EXISTS stream VARCHAR(50) NOT NULL DEFAULT 'SINGLE STREAM';")

            # Same schema-drift issue as timetable_slots below: the original
            # UNIQUE constraint on a pre-existing live table won't include the
            # later-added `stream` column, breaking this table's ON CONFLICT
            # clause too. Fix it the same way.
            cur.execute("""
                DELETE FROM teacher_subject_assignments a USING teacher_subject_assignments b
                WHERE a.id > b.id
                  AND a.school_id = b.school_id AND a.learning_area_id = b.learning_area_id
                  AND a.grade_name = b.grade_name AND a.education_level = b.education_level
                  AND a.stream = b.stream;
            """)
            cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_teacher_subject_assignments_slot ON teacher_subject_assignments (school_id, learning_area_id, grade_name, education_level, stream);")

            # Drop any OLDER unique constraint on this table that predates the
            # per-stream conversion — it won't include `stream`, so it would
            # otherwise still incorrectly reject two different streams having
            # the same subject assigned (they're not actually duplicates).
            cur.execute("""
                SELECT conname FROM pg_constraint c
                JOIN pg_class t ON c.conrelid = t.oid
                WHERE t.relname = 'teacher_subject_assignments' AND c.contype = 'u';
            """)
            for (conname,) in cur.fetchall():
                cur.execute(f'ALTER TABLE teacher_subject_assignments DROP CONSTRAINT IF EXISTS "{conname}";')

            cur.execute("""
                CREATE TABLE IF NOT EXISTS timetable_slots (
                    id SERIAL PRIMARY KEY,
                    school_id INTEGER REFERENCES schools(id) ON DELETE CASCADE,
                    grade_name VARCHAR(100) NOT NULL,
                    education_level VARCHAR(100) NOT NULL,
                    stream VARCHAR(50) NOT NULL DEFAULT 'SINGLE STREAM',
                    day_of_week VARCHAR(20) NOT NULL,
                    period_id INTEGER REFERENCES timetable_periods(id) ON DELETE CASCADE,
                    learning_area_id INTEGER REFERENCES learning_areas(id) ON DELETE SET NULL,
                    staff_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    UNIQUE(school_id, grade_name, education_level, stream, day_of_week, period_id)
                );
            """)
            cur.execute("ALTER TABLE timetable_slots ADD COLUMN IF NOT EXISTS stream VARCHAR(50) NOT NULL DEFAULT 'SINGLE STREAM';")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_timetable_slots_conflict ON timetable_slots (school_id, day_of_week, period_id, staff_user_id);")

            # Same schema-drift issue as teacher_subject_assignments above:
            # the original UNIQUE constraint on a pre-existing live table
            # won't include the later-added `stream` column, breaking this
            # table's ON CONFLICT clause. Fix it the same way.
            cur.execute("""
                DELETE FROM timetable_slots a USING timetable_slots b
                WHERE a.id > b.id
                  AND a.school_id = b.school_id AND a.grade_name = b.grade_name
                  AND a.education_level = b.education_level AND a.stream = b.stream
                  AND a.day_of_week = b.day_of_week AND a.period_id = b.period_id;
            """)
            cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_timetable_slots_slot ON timetable_slots (school_id, grade_name, education_level, stream, day_of_week, period_id);")

            cur.execute("""
                SELECT conname FROM pg_constraint c
                JOIN pg_class t ON c.conrelid = t.oid
                WHERE t.relname = 'timetable_slots' AND c.contype = 'u';
            """)
            for (conname,) in cur.fetchall():
                cur.execute(f'ALTER TABLE timetable_slots DROP CONSTRAINT IF EXISTS "{conname}";')

            # The table's original UNIQUE constraint (from before the per-stream
            # conversion) is stuck on live databases without the `stream` column,
            # since CREATE TABLE IF NOT EXISTS never re-runs on an existing table
            # and ALTER TABLE ADD COLUMN doesn't touch existing constraints. This
            # breaks the ON CONFLICT (...) clause used when saving a slot. Fix it
            # by explicitly creating the correctly-scoped unique index — Postgres's
            # ON CONFLICT matches any unique index with the exact column list,
            # regardless of the original constraint's name.
            cur.execute("""
                DELETE FROM timetable_slots a USING timetable_slots b
                WHERE a.id > b.id
                  AND a.school_id = b.school_id AND a.grade_name = b.grade_name
                  AND a.education_level = b.education_level AND a.stream = b.stream
                  AND a.day_of_week = b.day_of_week AND a.period_id = b.period_id;
            """)
            cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_timetable_slots_slot ON timetable_slots (school_id, grade_name, education_level, stream, day_of_week, period_id);")

            # Same reasoning as teacher_subject_assignments above — drop any
            # older unique constraint that predates the per-stream conversion.
            cur.execute("""
                SELECT conname FROM pg_constraint c
                JOIN pg_class t ON c.conrelid = t.oid
                WHERE t.relname = 'timetable_slots' AND c.contype = 'u';
            """)
            for (conname,) in cur.fetchall():
                cur.execute(f'ALTER TABLE timetable_slots DROP CONSTRAINT IF EXISTS "{conname}";')

            # Teacher availability — only exceptions are stored; a teacher with
            # no row for a given day/period is treated as available by default.
            cur.execute("""
                CREATE TABLE IF NOT EXISTS teacher_availability (
                    id SERIAL PRIMARY KEY,
                    school_id INTEGER REFERENCES schools(id) ON DELETE CASCADE,
                    staff_user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                    day_of_week VARCHAR(20) NOT NULL,
                    period_id INTEGER REFERENCES timetable_periods(id) ON DELETE CASCADE,
                    status VARCHAR(20) NOT NULL DEFAULT 'available',
                    UNIQUE(school_id, staff_user_id, day_of_week, period_id)
                );
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_teacher_availability_lookup ON teacher_availability (school_id, staff_user_id, day_of_week, period_id);")

            # Subject "time off" — mirrors teacher_availability, but keyed by
            # subject instead of teacher (e.g. "no Math after lunch", "PE only
            # in the afternoon"). Only exceptions are stored; a subject with
            # no row for a given day/period is available by default.
            cur.execute("""
                CREATE TABLE IF NOT EXISTS subject_availability (
                    id SERIAL PRIMARY KEY,
                    school_id INTEGER REFERENCES schools(id) ON DELETE CASCADE,
                    learning_area_id INTEGER REFERENCES learning_areas(id) ON DELETE CASCADE,
                    day_of_week VARCHAR(20) NOT NULL,
                    period_id INTEGER REFERENCES timetable_periods(id) ON DELETE CASCADE,
                    status VARCHAR(20) NOT NULL DEFAULT 'available',
                    UNIQUE(school_id, learning_area_id, day_of_week, period_id)
                );
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_subject_availability_lookup ON subject_availability (school_id, learning_area_id, day_of_week, period_id);")

            # "Same time" subject rule — a subject that must be scheduled at
            # one fixed day/period across every class/section that takes it
            # (e.g. a schoolwide Games period, or an assembly slot).
            cur.execute("""
                CREATE TABLE IF NOT EXISTS subject_sync_rules (
                    id SERIAL PRIMARY KEY,
                    school_id INTEGER REFERENCES schools(id) ON DELETE CASCADE,
                    learning_area_id INTEGER REFERENCES learning_areas(id) ON DELETE CASCADE,
                    day_of_week VARCHAR(20) NOT NULL,
                    period_id INTEGER REFERENCES timetable_periods(id) ON DELETE CASCADE,
                    UNIQUE(school_id, learning_area_id)
                );
            """)

            # Subject placement constraints ("card relationships") — per
            # class section, a rule between two subjects for the generator
            # (and manual edits) to respect.
            cur.execute("""
                CREATE TABLE IF NOT EXISTS subject_constraints (
                    id SERIAL PRIMARY KEY,
                    school_id INTEGER REFERENCES schools(id) ON DELETE CASCADE,
                    grade_name VARCHAR(100) NOT NULL,
                    education_level VARCHAR(100) NOT NULL,
                    stream VARCHAR(50) NOT NULL DEFAULT 'SINGLE STREAM',
                    subject_a_id INTEGER REFERENCES learning_areas(id) ON DELETE CASCADE,
                    subject_b_id INTEGER REFERENCES learning_areas(id) ON DELETE CASCADE,
                    constraint_type VARCHAR(30) NOT NULL,
                    UNIQUE(school_id, grade_name, education_level, stream, subject_a_id, subject_b_id, constraint_type)
                );
            """)

            # Self-healing migration: on any school whose timetable_slots or
            # teacher_subject_assignments table was created *before* the
            # per-stream conversion, the stream column got added via ALTER
            # TABLE but the original UNIQUE constraint (defined without
            # stream) was never updated to match — causing "ON CONFLICT"
            # in the app to fail with InvalidColumnReference. This detects
            # that mismatch and fixes it, without needing to guess whatever
            # Postgres auto-generated the old constraint's name as.
            _ensure_unique_constraint(
                cur, "timetable_slots",
                ["school_id", "grade_name", "education_level", "stream", "day_of_week", "period_id"],
                "uq_timetable_slots_section_slot",
            )
            _ensure_unique_constraint(
                cur, "teacher_subject_assignments",
                ["school_id", "learning_area_id", "grade_name", "education_level", "stream"],
                "uq_teacher_subject_assignments_section_subject",
            )

            conn.commit()


def _ensure_unique_constraint(cur, table_name: str, target_columns: list, new_constraint_name: str):
    """Ensures `table_name` has a UNIQUE constraint covering exactly
    `target_columns` — dropping any older UNIQUE constraint on that table
    that doesn't match (e.g. one predating a later-added column) and
    creating the correct one, only when actually needed."""
    cur.execute("""
        SELECT tc.constraint_name, array_agg(kcu.column_name ORDER BY kcu.ordinal_position) AS cols
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema
        WHERE tc.table_name = %s AND tc.constraint_type = 'UNIQUE' AND tc.table_schema = 'public'
        GROUP BY tc.constraint_name;
    """, (table_name,))
    existing = cur.fetchall()
    target_set = set(target_columns)
    if any(set(cols) == target_set for _, cols in existing):
        return  # already correct — nothing to do

    for constraint_name, _ in existing:
        cur.execute(f'ALTER TABLE {table_name} DROP CONSTRAINT "{constraint_name}";')
    cur.execute(f"""
        ALTER TABLE {table_name}
        ADD CONSTRAINT {new_constraint_name} UNIQUE ({", ".join(target_columns)});
    """)


# =====================================================================
# TIMETABLING MODULE
# =====================================================================
# No hardcoded periods, times, or breaks — every school configures its own
# day structure and bell times via /timetable/periods/{school_id}, since
# schools don't share a start time (boarding vs day schools especially).
ALL_POSSIBLE_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]


def get_school_days(cur, school_id: int):
    """Returns this school's configured list of teaching days (e.g. Mon-Fri
    or Mon-Sat), defaulting to a 5-day week only until the school sets its
    own value on the Periods & Days page."""
    cur.execute("SELECT days_per_week FROM timetable_settings WHERE school_id = %s;", (school_id,))
    row = cur.fetchone()
    days_per_week = row['days_per_week'] if row else 5
    return ALL_POSSIBLE_DAYS[:days_per_week]


def _section_label(grade_name: str, stream: str) -> str:
    """'Grade 6' for single-stream classes, 'Grade 6 — Stream N' otherwise."""
    if not stream or stream == "SINGLE STREAM":
        return grade_name
    return f"{grade_name} — Stream {stream}"


@router.get("/timetable/dashboard/{school_id}", response_class=HTMLResponse)
def timetable_dashboard(school_id: int, request: Request):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT name FROM schools WHERE id = %s;", (school_id,))
            school = cur.fetchone()
            if not school:
                raise HTTPException(status_code=404, detail="School not found.")


            cur.execute("""
                SELECT DISTINCT c.grade_name, c.education_level, s.stream, c.id AS class_order
                FROM students s
                JOIN classes c ON s.class_id = c.id
                WHERE s.school_id = %s AND (s.status IS NULL OR s.status != 'GRADUATED')
                ORDER BY c.id ASC, s.stream ASC;
            """, (school_id,))
            sections = cur.fetchall()

            cur.execute("""
                SELECT grade_name, education_level, stream, COUNT(*) AS slot_count
                FROM timetable_slots WHERE school_id = %s
                GROUP BY grade_name, education_level, stream;
            """, (school_id,))
            slot_counts = {(r['grade_name'], r['education_level'], r['stream']): r['slot_count'] for r in cur.fetchall()}

            cur.execute("SELECT COUNT(*) AS cnt FROM timetable_periods WHERE school_id = %s;", (school_id,))
            has_periods = cur.fetchone()['cnt'] > 0

    section_cards = ""
    for sec in sections:
        encoded_grade = urllib.parse.quote(sec['grade_name'])
        encoded_level = urllib.parse.quote(sec['education_level'])
        encoded_stream = urllib.parse.quote(sec['stream'])
        has_timetable = slot_counts.get((sec['grade_name'], sec['education_level'], sec['stream']), 0) > 0
        status_badge = (
            "<span class='text-[10px] font-bold px-2 py-0.5 rounded-full bg-emerald-50 text-emerald-700 border border-emerald-200'>Timetable set</span>"
            if has_timetable else
            "<span class='text-[10px] font-bold px-2 py-0.5 rounded-full bg-amber-50 text-amber-700 border border-amber-200'>Not yet created</span>"
        )
        section_cards += f"""
        <div class='bg-white border border-slate-200/80 p-5 rounded-2xl shadow-xs flex flex-col justify-between gap-3'>
            <div>
                <span class='text-[10px] bg-slate-100 text-slate-600 px-2.5 py-1 rounded-md font-bold uppercase tracking-wider'>{esc(sec['education_level'])}</span>
                <h3 class='text-base font-black text-slate-800 mt-2.5'>{esc(_section_label(sec['grade_name'], sec['stream']))}</h3>
                <div class="mt-2">{status_badge}</div>
            </div>
            <div class='grid grid-cols-3 gap-2'>
                <a href='/timetable/assignments/{school_id}?grade_name={encoded_grade}&education_level={encoded_level}&stream={encoded_stream}' class='bg-slate-700 hover:bg-slate-800 text-white text-center text-xs py-2 rounded-xl font-semibold transition'>Assign Teachers</a>
                <a href='/timetable/constraints/{school_id}?grade_name={encoded_grade}&education_level={encoded_level}&stream={encoded_stream}' class='bg-amber-600 hover:bg-amber-700 text-white text-center text-xs py-2 rounded-xl font-semibold transition'>Constraints</a>
                <a href='/timetable/grade/{school_id}?grade_name={encoded_grade}&education_level={encoded_level}&stream={encoded_stream}' class='bg-indigo-700 hover:bg-indigo-800 text-white text-center text-xs py-2 rounded-xl font-semibold transition'>Open Timetable</a>
            </div>
        </div>
        """

    return HTMLResponse(f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Timetabling — {esc(school['name'])}</title>
        <script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script>
    </head>
    <body class="bg-[#F8FAFC] min-h-screen">
        <header class="bg-white border-b border-slate-200/80 px-6 sm:px-8 py-4 flex flex-col sm:flex-row sm:justify-between sm:items-center gap-2">
            <div>
                <h1 class="text-base font-bold text-slate-900">📅 Timetabling — {esc(school['name'])}</h1>
                <p class="text-xs text-slate-400">Each stream has its own independent timetable.</p>
            </div>
            <div class="flex items-center gap-2 flex-wrap">
                <a href="/timetable/periods/{school_id}" class="bg-white hover:bg-slate-100 text-slate-700 border border-slate-200 px-4 py-2 rounded-xl text-xs font-bold text-center transition">⏱ Periods & Days</a>
                <a href="/timetable/availability/{school_id}" class="bg-indigo-700 hover:bg-indigo-800 text-white px-4 py-2 rounded-xl text-xs font-bold text-center transition">👩‍🏫 Teacher Availability</a>
                <a href="/timetable/subject-availability/{school_id}" class="bg-purple-700 hover:bg-purple-800 text-white px-4 py-2 rounded-xl text-xs font-bold text-center transition">📚 Subject Time-Off</a>
                <a href="/timetable/sync-rules/{school_id}" class="bg-amber-600 hover:bg-amber-700 text-white px-4 py-2 rounded-xl text-xs font-bold text-center transition">🔗 Same-Time Rules</a>
                <a href="/timetable/teachers/{school_id}" class="bg-slate-700 hover:bg-slate-800 text-white px-4 py-2 rounded-xl text-xs font-bold text-center transition">🖨 Teacher Timetables</a>
                <a href="/timetable/master/{school_id}" class="bg-emerald-700 hover:bg-emerald-800 text-white px-4 py-2 rounded-xl text-xs font-bold text-center transition">🗓 Whole School View</a>
                <a href="{get_dashboard_url(request, school_id)}" class="bg-slate-200 hover:bg-slate-300 text-slate-700 px-4 py-2 rounded-xl text-xs font-bold text-center transition">← Back to Dashboard</a>
            </div>
        </header>
        <div class="p-6 sm:p-8 max-w-6xl mx-auto">
            {"<div class='bg-amber-50 border border-amber-200 text-amber-800 text-sm px-4 py-3 rounded-xl mb-6'>⏱ <b>Set up your periods and bell times first</b> — go to <a href='/timetable/periods/" + str(school_id) + "' class='underline font-bold'>Periods &amp; Days</a> before generating any timetable.</div>" if not has_periods else ""}
            <div class="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 gap-4">
                {section_cards or "<p class='text-slate-400 text-xs italic col-span-full text-center py-8 bg-white border border-dashed rounded-2xl'>No classes with students yet — add students first.</p>"}
            </div>
        </div>
    </body>
    </html>
    """)


@router.get("/timetable/periods/{school_id}", response_class=HTMLResponse)
def timetable_periods_view(school_id: int, request: Request):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT name FROM schools WHERE id = %s;", (school_id,))
            school = cur.fetchone()
            if not school:
                raise HTTPException(status_code=404, detail="School not found.")

            cur.execute("SELECT days_per_week FROM timetable_settings WHERE school_id = %s;", (school_id,))
            settings_row = cur.fetchone()
            days_per_week = settings_row['days_per_week'] if settings_row else 5

            cur.execute("SELECT * FROM timetable_periods WHERE school_id = %s ORDER BY period_order ASC;", (school_id,))
            periods = cur.fetchall()

    days_options = "".join(
        f"<option value='{n}' {'selected' if n == days_per_week else ''}>{n} days ({', '.join(ALL_POSSIBLE_DAYS[:n])})</option>"
        for n in range(1, 7)
    )

    period_rows = ""
    for p in periods:
        row_type = "Break" if not p['is_teaching_period'] else "Teaching"
        row_style = "background:#f0fdf4;" if not p['is_teaching_period'] else ""
        period_rows += f"""
        <tr style="{row_style}" class="border-b text-sm">
            <td class="p-2.5 text-center text-slate-400 font-mono text-xs">{p['period_order']}</td>
            <td class="p-2.5 font-bold text-slate-800">{esc(p['label'])}</td>
            <td class="p-2.5 text-slate-500">{esc(p['short_label'] or '')}</td>
            <td class="p-2.5 text-slate-500">{esc(p['start_time'] or '')}</td>
            <td class="p-2.5 text-slate-500">{esc(p['end_time'] or '')}</td>
            <td class="p-2.5 text-center">
                <span class="text-[10px] font-bold px-2 py-0.5 rounded-full {'bg-emerald-50 text-emerald-700 border border-emerald-200' if p['is_teaching_period'] else 'bg-slate-100 text-slate-500 border border-slate-200'}">{row_type}</span>
            </td>
            <td class="p-2.5 text-right">
                <form action="/api/v1/timetable/periods/delete/{school_id}/{p['id']}" method="post" onsubmit="return confirm('Delete period \\'{esc(p['label'])}\\'? Any timetable slots using it will be cleared too.');">
                    <button type="submit" class="text-rose-600 hover:text-rose-800 text-xs font-bold">Delete</button>
                </form>
            </td>
        </tr>
        """

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Periods & Days — {esc(school['name'])}</title>
        <script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script>
    </head>
    <body class="bg-[#F8FAFC] min-h-screen">
        <header class="bg-white border-b border-slate-200/80 px-6 sm:px-8 py-4 flex flex-col sm:flex-row sm:justify-between sm:items-center gap-2">
            <div>
                <h1 class="text-base font-bold text-slate-900">⏱ Periods & Days — {esc(school['name'])}</h1>
                <p class="text-xs text-slate-400">Every school sets its own bell times — there's no shared default, since boarding and day schools (and different regions) start at different times.</p>
            </div>
            <a href="/timetable/dashboard/{school_id}" class="bg-slate-200 hover:bg-slate-300 text-slate-700 px-4 py-2 rounded-xl text-xs font-bold text-center transition">← Back</a>
        </header>

        <div class="p-4 sm:p-8 max-w-4xl mx-auto space-y-6">
            <div class="bg-white p-5 sm:p-6 rounded-2xl border shadow-xs">
                <h2 class="text-sm font-bold text-slate-800 mb-3">Number of Teaching Days</h2>
                <form action="/api/v1/timetable/periods/days/{school_id}" method="post" class="flex flex-col sm:flex-row gap-3">
                    <select name="days_per_week" class="flex-1 border p-2.5 rounded-xl text-sm font-medium">{days_options}</select>
                    <button type="submit" class="bg-indigo-700 hover:bg-indigo-800 text-white font-bold px-5 py-2.5 rounded-xl text-sm transition">Save</button>
                </form>
            </div>

            <div class="bg-white rounded-2xl border shadow-xs overflow-hidden">
                <div class="p-5 sm:p-6 border-b bg-slate-50/50">
                    <h2 class="text-sm font-bold text-slate-800">Periods & Bell Times</h2>
                    <p class="text-xs text-slate-400 mt-0.5">Add every period and break in order, with its actual start/end time.</p>
                </div>
                <div class="overflow-x-auto">
                    <table class="w-full text-left">
                        <thead>
                            <tr class="bg-slate-50 text-slate-500 text-xs font-semibold border-b">
                                <th class="p-2.5">#</th><th class="p-2.5">Name</th><th class="p-2.5">Short</th>
                                <th class="p-2.5">Start</th><th class="p-2.5">End</th><th class="p-2.5 text-center">Type</th><th class="p-2.5"></th>
                            </tr>
                        </thead>
                        <tbody>{period_rows or "<tr><td colspan='7' class='text-center p-6 text-slate-400 italic text-xs'>No periods configured yet — add your first one below.</td></tr>"}</tbody>
                    </table>
                </div>
                <form action="/api/v1/timetable/periods/add/{school_id}" method="post" class="p-5 sm:p-6 bg-slate-50/50 border-t grid grid-cols-1 sm:grid-cols-6 gap-3">
                    <div class="sm:col-span-2">
                        <label class="text-[11px] font-bold text-slate-500">Name</label>
                        <input type="text" name="label" placeholder="e.g. Period 1, Short Break" class="w-full border p-2 rounded-lg mt-1 text-sm" required>
                    </div>
                    <div>
                        <label class="text-[11px] font-bold text-slate-500">Short Label</label>
                        <input type="text" name="short_label" placeholder="1" class="w-full border p-2 rounded-lg mt-1 text-sm">
                    </div>
                    <div>
                        <label class="text-[11px] font-bold text-slate-500">Start Time</label>
                        <input type="time" name="start_time" class="w-full border p-2 rounded-lg mt-1 text-sm" required>
                    </div>
                    <div>
                        <label class="text-[11px] font-bold text-slate-500">End Time</label>
                        <input type="time" name="end_time" class="w-full border p-2 rounded-lg mt-1 text-sm" required>
                    </div>
                    <div class="flex flex-col justify-end">
                        <label class="text-[11px] font-bold text-slate-500 flex items-center gap-1.5 mb-2">
                            <input type="checkbox" name="is_break" value="1" class="w-4 h-4"> This is a break
                        </label>
                        <button type="submit" class="bg-emerald-700 hover:bg-emerald-800 text-white font-bold py-2 rounded-lg text-sm transition">+ Add</button>
                    </div>
                </form>
            </div>
        </div>
    </body>
    </html>
    """


@router.post("/api/v1/timetable/periods/days/{school_id}")
def save_timetable_days(school_id: int, request: Request, days_per_week: int = Form(...)):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    days_per_week = max(1, min(6, days_per_week))
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO timetable_settings (school_id, days_per_week) VALUES (%s, %s)
                ON CONFLICT (school_id) DO UPDATE SET days_per_week = EXCLUDED.days_per_week;
            """, (school_id, days_per_week))
            conn.commit()

    return RedirectResponse(url=f"/timetable/periods/{school_id}", status_code=303)


@router.post("/api/v1/timetable/periods/add/{school_id}")
def add_timetable_period(
    school_id: int,
    request: Request,
    label: str = Form(...),
    short_label: str = Form(""),
    start_time: str = Form(...),
    end_time: str = Form(...),
    is_break: str = Form(None),
):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    label = label.strip()
    short_label = short_label.strip() or label[:3].upper()
    if not label:
        raise HTTPException(status_code=400, detail="A name for this period is required.")

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COALESCE(MAX(period_order), 0) + 1 AS next_order FROM timetable_periods WHERE school_id = %s;", (school_id,))
            next_order = cur.fetchone()[0]
            cur.execute("""
                INSERT INTO timetable_periods (school_id, period_order, label, short_label, start_time, end_time, is_teaching_period)
                VALUES (%s, %s, %s, %s, %s, %s, %s);
            """, (school_id, next_order, label, short_label, start_time, end_time, not bool(is_break)))
            conn.commit()

    return RedirectResponse(url=f"/timetable/periods/{school_id}", status_code=303)


@router.post("/api/v1/timetable/periods/delete/{school_id}/{period_id}")
def delete_timetable_period(school_id: int, period_id: int, request: Request):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            # Cascades to timetable_slots and teacher_availability rows using this period.
            cur.execute("DELETE FROM timetable_periods WHERE id = %s AND school_id = %s;", (period_id, school_id))
            conn.commit()

    return RedirectResponse(url=f"/timetable/periods/{school_id}", status_code=303)


@router.get("/timetable/assignments/{school_id}", response_class=HTMLResponse)
def teacher_assignments_view(school_id: int, request: Request, grade_name: str, education_level: str, stream: str):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT id, name FROM learning_areas WHERE education_level = %s;", (education_level,))
            subjects = sort_subjects_for_display(cur.fetchall(), education_level)

            cur.execute("SELECT id, email, full_name FROM users WHERE school_id = %s AND role = 'staff' AND is_verified = TRUE ORDER BY full_name NULLS LAST, email ASC;", (school_id,))
            staff_members = cur.fetchall()

            cur.execute("""
                SELECT learning_area_id, staff_user_id FROM teacher_subject_assignments
                WHERE school_id = %s AND grade_name = %s AND education_level = %s AND stream = %s;
            """, (school_id, grade_name, education_level, stream))
            current_assignments = {r['learning_area_id']: r['staff_user_id'] for r in cur.fetchall()}

    rows_html = ""
    for sub in subjects:
        assigned_id = current_assignments.get(sub['id'])
        options = "<option value=''>— Unassigned —</option>" + "".join(
            f"<option value='{m['id']}' {'selected' if m['id'] == assigned_id else ''}>{esc(m['full_name'] or m['email'])}</option>"
            for m in staff_members
        )
        rows_html += f"""
        <div class="flex items-center justify-between gap-3 py-2.5 border-b border-slate-50 last:border-0">
            <span class="text-sm font-semibold text-slate-700">{esc(sub['name'])}</span>
            <select name="teacher_{sub['id']}" class="border p-2 rounded-lg text-xs font-semibold bg-white w-48">{options}</select>
        </div>
        """

    section_label = _section_label(grade_name, stream)
    return f"""
    <!DOCTYPE html>
    <html>
    <head><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Assign Teachers</title><script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script></head>
    <body class="bg-slate-100 min-h-screen p-4 sm:p-8">
        <div class="max-w-xl mx-auto bg-white p-6 rounded-2xl border shadow-xs">
            <h2 class="text-lg font-black text-slate-800">Assign Teachers</h2>
            <p class="text-xs text-slate-400 mb-4">{esc(section_label)} ({esc(education_level)}) — who teaches each subject to this class?</p>
            {"<p class='text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded-lg p-3 mb-4'>No verified staff accounts yet — add and activate staff first, then come back to assign them here.</p>" if not staff_members else ""}
            <form action="/api/v1/timetable/assignments/{school_id}" method="post" class="space-y-1">
                <input type="hidden" name="grade_name" value="{esc(grade_name)}">
                <input type="hidden" name="education_level" value="{esc(education_level)}">
                <input type="hidden" name="stream" value="{esc(stream)}">
                {rows_html or "<p class='text-slate-400 text-xs italic'>No subjects configured for this education level.</p>"}
                <div class="pt-4 flex gap-3">
                    <button type="submit" class="bg-indigo-700 hover:bg-indigo-800 text-white font-bold py-2.5 px-5 rounded-xl text-sm transition">Save Assignments</button>
                    <a href="/timetable/dashboard/{school_id}" class="bg-slate-200 hover:bg-slate-300 text-slate-700 font-bold py-2.5 px-5 rounded-xl text-sm transition">← Back</a>
                </div>
            </form>
        </div>
    </body>
    </html>
    """


@router.post("/api/v1/timetable/assignments/{school_id}")
async def save_teacher_assignments(school_id: int, request: Request):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    form = await request.form()
    grade_name = form.get("grade_name", "").strip()
    education_level = form.get("education_level", "").strip()
    stream = form.get("stream", "").strip() or "SINGLE STREAM"
    if not grade_name or not education_level:
        raise HTTPException(status_code=400, detail="Grade and education level are required.")

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            for key, value in form.items():
                if not key.startswith("teacher_"):
                    continue
                learning_area_id = int(key.replace("teacher_", ""))
                staff_user_id = int(value) if value else None

                if staff_user_id is None:
                    cur.execute("""
                        DELETE FROM teacher_subject_assignments
                        WHERE school_id = %s AND learning_area_id = %s AND grade_name = %s AND education_level = %s AND stream = %s;
                    """, (school_id, learning_area_id, grade_name, education_level, stream))
                else:
                    cur.execute("""
                        INSERT INTO teacher_subject_assignments (school_id, staff_user_id, learning_area_id, grade_name, education_level, stream)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON CONFLICT (school_id, learning_area_id, grade_name, education_level, stream)
                        DO UPDATE SET staff_user_id = EXCLUDED.staff_user_id;
                    """, (school_id, staff_user_id, learning_area_id, grade_name, education_level, stream))
            conn.commit()

    encoded_grade = urllib.parse.quote(grade_name)
    encoded_level = urllib.parse.quote(education_level)
    encoded_stream = urllib.parse.quote(stream)
    return RedirectResponse(url=f"/timetable/grade/{school_id}?grade_name={encoded_grade}&education_level={encoded_level}&stream={encoded_stream}", status_code=303)


@router.get("/timetable/availability/{school_id}", response_class=HTMLResponse)
def teacher_availability_picker(school_id: int, request: Request):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT id, email, full_name FROM users
                WHERE school_id = %s AND role = 'staff' AND is_verified = TRUE
                ORDER BY full_name NULLS LAST, email ASC;
            """, (school_id,))
            staff_members = cur.fetchall()

    rows_html = "".join(f"""
        <a href="/timetable/availability/{school_id}/{m['id']}" class="flex items-center justify-between p-4 border-b last:border-0 hover:bg-slate-50 transition">
            <span class="text-sm font-bold text-slate-800">{esc(m['full_name'] or m['email'])}</span>
            <span class="text-xs text-indigo-700 font-bold">Set Availability →</span>
        </a>
    """ for m in staff_members)

    return f"""
    <!DOCTYPE html>
    <html>
    <head><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Teacher Availability</title><script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script></head>
    <body class="bg-slate-100 min-h-screen p-4 sm:p-8">
        <div class="max-w-xl mx-auto bg-white p-6 rounded-2xl border shadow-xs">
            <h2 class="text-lg font-black text-slate-800">👩‍🏫 Teacher Availability</h2>
            <p class="text-xs text-slate-400 mb-4">Pick a teacher to set which days/periods they're available, unavailable, or conditional for.</p>
            <div>{rows_html or "<p class='text-slate-400 text-xs italic p-4'>No verified staff accounts yet.</p>"}</div>
            <div class="pt-4">
                <a href="/timetable/dashboard/{school_id}" class="bg-slate-200 hover:bg-slate-300 text-slate-700 font-bold py-2.5 px-5 rounded-xl text-sm transition inline-block">← Back</a>
            </div>
        </div>
    </body>
    </html>
    """


@router.get("/timetable/teachers/{school_id}", response_class=HTMLResponse)
def teacher_timetable_picker(school_id: int, request: Request):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT id, email, full_name FROM users
                WHERE school_id = %s AND role = 'staff' AND is_verified = TRUE
                ORDER BY full_name NULLS LAST, email ASC;
            """, (school_id,))
            staff_members = cur.fetchall()

    rows_html = "".join(f"""
        <div class="flex items-center justify-between p-4 border-b last:border-0 hover:bg-slate-50 transition">
            <span class="text-sm font-bold text-slate-800">{esc(m['full_name'] or m['email'])}</span>
            <a href="/timetable/print/teacher/{school_id}/{m['id']}" target="_blank" class="text-xs bg-indigo-700 hover:bg-indigo-800 text-white font-bold px-3 py-1.5 rounded-lg transition">🖨 View / Print</a>
        </div>
    """ for m in staff_members)

    return f"""
    <!DOCTYPE html>
    <html>
    <head><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Teacher Timetables</title><script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script></head>
    <body class="bg-slate-100 min-h-screen p-4 sm:p-8">
        <div class="max-w-xl mx-auto bg-white p-6 rounded-2xl border shadow-xs">
            <h2 class="text-lg font-black text-slate-800">🖨 Teacher Timetables</h2>
            <p class="text-xs text-slate-400 mb-4">Each teacher's timetable is built automatically from every class they're assigned to teach — pick a teacher to view or print theirs.</p>
            <div>{rows_html or "<p class='text-slate-400 text-xs italic p-4'>No verified staff accounts yet.</p>"}</div>
            <div class="pt-4">
                <a href="/timetable/dashboard/{school_id}" class="bg-slate-200 hover:bg-slate-300 text-slate-700 font-bold py-2.5 px-5 rounded-xl text-sm transition inline-block">← Back</a>
            </div>
        </div>
    </body>
    </html>
    """


@router.get("/timetable/availability/{school_id}/{teacher_id}", response_class=HTMLResponse)
def teacher_availability_grid(school_id: int, teacher_id: int, request: Request):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT id, full_name, email FROM users WHERE id = %s AND school_id = %s AND role = 'staff';", (teacher_id, school_id))
            teacher = cur.fetchone()
            if not teacher:
                raise HTTPException(status_code=404, detail="Teacher not found.")

            days = get_school_days(cur, school_id)
            conn.commit()

            cur.execute("SELECT id, period_order, label FROM timetable_periods WHERE school_id = %s AND is_teaching_period = TRUE ORDER BY period_order ASC;", (school_id,))
            periods = cur.fetchall()

            cur.execute("""
                SELECT day_of_week, period_id, status FROM teacher_availability
                WHERE school_id = %s AND staff_user_id = %s;
            """, (school_id, teacher_id))
            current = {(r['day_of_week'], r['period_id']): r['status'] for r in cur.fetchall()}

    status_options = [("available", "✅ Available"), ("conditional", "❔ Conditional"), ("not_available", "❌ Not Available")]

    header_cells = "".join(f"<th class='p-2 text-center text-xs'>{d}</th>" for d in days)
    body_rows = ""
    for period in periods:
        body_rows += f"<tr><td class='p-2 text-xs font-bold bg-slate-50 sticky left-0'>{esc(period['label'])}</td>"
        for day in days:
            cur_status = current.get((day, period['id']), "available")
            options = "".join(f"<option value='{val}' {'selected' if val == cur_status else ''}>{lbl}</option>" for val, lbl in status_options)
            body_rows += f"<td class='p-1 text-center'><select name='status_{day}_{period['id']}' class='text-xs border rounded-lg p-1.5 w-full'>{options}</select></td>"
        body_rows += "</tr>"

    return f"""
    <!DOCTYPE html>
    <html>
    <head><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Availability — {esc(teacher['full_name'] or teacher['email'])}</title><script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script></head>
    <body class="bg-slate-100 min-h-screen p-4 sm:p-8">
        <div class="max-w-4xl mx-auto bg-white p-6 rounded-2xl border shadow-xs">
            <h2 class="text-lg font-black text-slate-800">👩‍🏫 {esc(teacher['full_name'] or teacher['email'])} — Availability</h2>
            <p class="text-xs text-slate-400 mb-4">Mark when this teacher is unavailable (e.g. part-time, other commitments). The timetable generator and manual editor will both respect this.</p>
            <form action="/api/v1/timetable/availability/update/{school_id}/{teacher_id}" method="post">
                <div class="overflow-x-auto">
                    <table class="w-full border-collapse text-xs">
                        <thead><tr><th class="p-2 sticky left-0 bg-white"></th>{header_cells}</tr></thead>
                        <tbody>{body_rows}</tbody>
                    </table>
                </div>
                <div class="pt-4 flex gap-3">
                    <button type="submit" class="bg-indigo-700 hover:bg-indigo-800 text-white font-bold py-2.5 px-5 rounded-xl text-sm transition">Save Availability</button>
                    <a href="/timetable/availability/{school_id}" class="bg-slate-200 hover:bg-slate-300 text-slate-700 font-bold py-2.5 px-5 rounded-xl text-sm transition">← Back</a>
                </div>
            </form>
        </div>
    </body>
    </html>
    """


@router.post("/api/v1/timetable/availability/update/{school_id}/{teacher_id}")
async def save_teacher_availability(school_id: int, teacher_id: int, request: Request):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    form = await request.form()

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE id = %s AND school_id = %s AND role = 'staff';", (teacher_id, school_id))
            if not cur.fetchone():
                raise HTTPException(status_code=404, detail="Teacher not found.")

            cur.execute("SELECT id FROM timetable_periods WHERE school_id = %s AND is_teaching_period = TRUE;", (school_id,))
            period_ids = [r[0] for r in cur.fetchall()]
            days = get_school_days(cur, school_id)

            for day in days:
                for period_id in period_ids:
                    field_name = f"status_{day}_{period_id}"
                    status = form.get(field_name, "available")
                    if status == "available":
                        # Available is the implicit default — no need to store a row for it.
                        cur.execute("""
                            DELETE FROM teacher_availability
                            WHERE school_id = %s AND staff_user_id = %s AND day_of_week = %s AND period_id = %s;
                        """, (school_id, teacher_id, day, period_id))
                    else:
                        cur.execute("""
                            INSERT INTO teacher_availability (school_id, staff_user_id, day_of_week, period_id, status)
                            VALUES (%s, %s, %s, %s, %s)
                            ON CONFLICT (school_id, staff_user_id, day_of_week, period_id)
                            DO UPDATE SET status = EXCLUDED.status;
                        """, (school_id, teacher_id, day, period_id, status))
            conn.commit()

    return RedirectResponse(url=f"/timetable/availability/{school_id}/{teacher_id}", status_code=303)


@router.get("/timetable/subject-availability/{school_id}", response_class=HTMLResponse)
def subject_availability_picker(school_id: int, request: Request):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT id, name, education_level FROM learning_areas ORDER BY education_level ASC, name ASC;")
            subjects = cur.fetchall()

    rows_html = "".join(f"""
        <a href="/timetable/subject-availability/{school_id}/{s['id']}" class="flex items-center justify-between p-4 border-b last:border-0 hover:bg-slate-50 transition">
            <span class="text-sm font-bold text-slate-800">{esc(s['name'])} <span class="text-[10px] text-slate-400 font-normal">({esc(s['education_level'])})</span></span>
            <span class="text-xs text-indigo-700 font-bold">Set Time Off →</span>
        </a>
    """ for s in subjects)

    return f"""
    <!DOCTYPE html>
    <html>
    <head><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Subject Time Off</title><script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script></head>
    <body class="bg-slate-100 min-h-screen p-4 sm:p-8">
        <div class="max-w-xl mx-auto bg-white p-6 rounded-2xl border shadow-xs">
            <h2 class="text-lg font-black text-slate-800">📚 Subject Time Off</h2>
            <p class="text-xs text-slate-400 mb-4">Pick a subject to mark days/periods it can't (or preferably shouldn't) be scheduled in — e.g. no Math last period, PE unavailable when the field is in use.</p>
            <div>{rows_html or "<p class='text-slate-400 text-xs italic p-4'>No subjects configured yet.</p>"}</div>
            <div class="pt-4">
                <a href="/timetable/dashboard/{school_id}" class="bg-slate-200 hover:bg-slate-300 text-slate-700 font-bold py-2.5 px-5 rounded-xl text-sm transition inline-block">← Back</a>
            </div>
        </div>
    </body>
    </html>
    """


@router.get("/timetable/subject-availability/{school_id}/{learning_area_id}", response_class=HTMLResponse)
def subject_availability_grid(school_id: int, learning_area_id: int, request: Request):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT id, name FROM learning_areas WHERE id = %s;", (learning_area_id,))
            subject = cur.fetchone()
            if not subject:
                raise HTTPException(status_code=404, detail="Subject not found.")

            days = get_school_days(cur, school_id)
            conn.commit()

            cur.execute("SELECT id, period_order, label FROM timetable_periods WHERE school_id = %s AND is_teaching_period = TRUE ORDER BY period_order ASC;", (school_id,))
            periods = cur.fetchall()

            cur.execute("""
                SELECT day_of_week, period_id, status FROM subject_availability
                WHERE school_id = %s AND learning_area_id = %s;
            """, (school_id, learning_area_id))
            current = {(r['day_of_week'], r['period_id']): r['status'] for r in cur.fetchall()}

    status_options = [("available", "✅ Available"), ("conditional", "❔ Conditional"), ("not_available", "❌ Not Available")]

    header_cells = "".join(f"<th class='p-2 text-center text-xs'>{d}</th>" for d in days)
    body_rows = ""
    for period in periods:
        body_rows += f"<tr><td class='p-2 text-xs font-bold bg-slate-50 sticky left-0'>{esc(period['label'])}</td>"
        for day in days:
            cur_status = current.get((day, period['id']), "available")
            options = "".join(f"<option value='{val}' {'selected' if val == cur_status else ''}>{lbl}</option>" for val, lbl in status_options)
            body_rows += f"<td class='p-1 text-center'><select name='status_{day}_{period['id']}' class='text-xs border rounded-lg p-1.5 w-full'>{options}</select></td>"
        body_rows += "</tr>"

    return f"""
    <!DOCTYPE html>
    <html>
    <head><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Time Off — {esc(subject['name'])}</title><script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script></head>
    <body class="bg-slate-100 min-h-screen p-4 sm:p-8">
        <div class="max-w-4xl mx-auto bg-white p-6 rounded-2xl border shadow-xs">
            <h2 class="text-lg font-black text-slate-800">📚 {esc(subject['name'])} — Time Off</h2>
            <p class="text-xs text-slate-400 mb-4">"Not Available" is a hard block the generator and manual editor will never use for this subject. "Conditional" is a soft preference — used only if nothing better fits.</p>
            <form action="/api/v1/timetable/subject-availability/update/{school_id}/{learning_area_id}" method="post">
                <div class="overflow-x-auto">
                    <table class="w-full border-collapse text-xs">
                        <thead><tr><th class="p-2 sticky left-0 bg-white"></th>{header_cells}</tr></thead>
                        <tbody>{body_rows}</tbody>
                    </table>
                </div>
                <div class="pt-4 flex gap-3">
                    <button type="submit" class="bg-indigo-700 hover:bg-indigo-800 text-white font-bold py-2.5 px-5 rounded-xl text-sm transition">Save Time Off</button>
                    <a href="/timetable/subject-availability/{school_id}" class="bg-slate-200 hover:bg-slate-300 text-slate-700 font-bold py-2.5 px-5 rounded-xl text-sm transition">← Back</a>
                </div>
            </form>
        </div>
    </body>
    </html>
    """


@router.post("/api/v1/timetable/subject-availability/update/{school_id}/{learning_area_id}")
async def save_subject_availability(school_id: int, learning_area_id: int, request: Request):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    form = await request.form()

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM learning_areas WHERE id = %s;", (learning_area_id,))
            if not cur.fetchone():
                raise HTTPException(status_code=404, detail="Subject not found.")

            cur.execute("SELECT id FROM timetable_periods WHERE school_id = %s AND is_teaching_period = TRUE;", (school_id,))
            period_ids = [r[0] for r in cur.fetchall()]
            days = get_school_days(cur, school_id)

            for day in days:
                for period_id in period_ids:
                    field_name = f"status_{day}_{period_id}"
                    status = form.get(field_name, "available")
                    if status == "available":
                        cur.execute("""
                            DELETE FROM subject_availability
                            WHERE school_id = %s AND learning_area_id = %s AND day_of_week = %s AND period_id = %s;
                        """, (school_id, learning_area_id, day, period_id))
                    else:
                        cur.execute("""
                            INSERT INTO subject_availability (school_id, learning_area_id, day_of_week, period_id, status)
                            VALUES (%s, %s, %s, %s, %s)
                            ON CONFLICT (school_id, learning_area_id, day_of_week, period_id)
                            DO UPDATE SET status = EXCLUDED.status;
                        """, (school_id, learning_area_id, day, period_id, status))
            conn.commit()

    return RedirectResponse(url=f"/timetable/subject-availability/{school_id}/{learning_area_id}", status_code=303)


@router.get("/timetable/sync-rules/{school_id}", response_class=HTMLResponse)
def subject_sync_rules_view(school_id: int, request: Request):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            days = get_school_days(cur, school_id)
            conn.commit()

            cur.execute("SELECT id, period_order, label FROM timetable_periods WHERE school_id = %s AND is_teaching_period = TRUE ORDER BY period_order ASC;", (school_id,))
            periods = cur.fetchall()

            cur.execute("SELECT id, name, education_level FROM learning_areas ORDER BY education_level ASC, name ASC;")
            subjects = cur.fetchall()

            cur.execute("""
                SELECT r.learning_area_id, r.day_of_week, r.period_id, p.label AS period_label
                FROM subject_sync_rules r
                JOIN timetable_periods p ON r.period_id = p.id
                WHERE r.school_id = %s;
            """, (school_id,))
            rules = {r['learning_area_id']: r for r in cur.fetchall()}

    rows_html = ""
    for s in subjects:
        rule = rules.get(s['id'])
        current_note = (
            f"<span class='text-[10px] text-emerald-700 font-bold'>Locked: {esc(rule['day_of_week'])}, {esc(rule['period_label'])}</span>"
            if rule else
            "<span class='text-[10px] text-slate-400 italic'>No rule — scheduled normally</span>"
        )
        day_options = "<option value=''>— No rule —</option>" + "".join(
            f"<option value='{d}' {'selected' if rule and rule['day_of_week'] == d else ''}>{d}</option>" for d in days
        )
        period_options = "".join(
            f"<option value='{p['id']}' {'selected' if rule and rule['period_id'] == p['id'] else ''}>{esc(p['label'])}</option>" for p in periods
        )
        rows_html += f"""
        <tr class="border-b text-sm">
            <td class="p-3">
                <p class="font-bold text-slate-800">{esc(s['name'])}</p>
                <p class="text-[10px] text-slate-400">{esc(s['education_level'])}</p>
                {current_note}
            </td>
            <td class="p-3">
                <form action="/api/v1/timetable/sync-rules/save/{school_id}/{s['id']}" method="post" class="flex flex-wrap items-center gap-2">
                    <select name="day_of_week" class="border rounded-lg p-1.5 text-xs">{day_options}</select>
                    <select name="period_id" class="border rounded-lg p-1.5 text-xs">{period_options}</select>
                    <button type="submit" class="bg-indigo-700 hover:bg-indigo-800 text-white text-xs font-bold px-3 py-1.5 rounded-lg transition">Save</button>
                </form>
            </td>
        </tr>
        """

    return f"""
    <!DOCTYPE html>
    <html>
    <head><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Same-Time Subject Rules</title><script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script></head>
    <body class="bg-slate-100 min-h-screen p-4 sm:p-8">
        <div class="max-w-3xl mx-auto bg-white p-6 rounded-2xl border shadow-xs">
            <h2 class="text-lg font-black text-slate-800">🔗 Same-Time Subject Rules</h2>
            <p class="text-xs text-slate-400 mb-4">Lock a subject to one fixed day and period, school-wide — every class/stream that takes it will be scheduled at that exact slot (e.g. a schoolwide Games period or assembly). Choose "— No rule —" to let it schedule normally again.</p>
            <div class="overflow-x-auto">
                <table class="w-full text-left border-collapse">
                    <tbody>{rows_html or "<tr><td class='p-6 text-center text-slate-400 italic text-xs'>No subjects configured yet.</td></tr>"}</tbody>
                </table>
            </div>
            <div class="pt-4">
                <a href="/timetable/dashboard/{school_id}" class="bg-slate-200 hover:bg-slate-300 text-slate-700 font-bold py-2.5 px-5 rounded-xl text-sm transition inline-block">← Back</a>
            </div>
        </div>
    </body>
    </html>
    """


@router.post("/api/v1/timetable/sync-rules/save/{school_id}/{learning_area_id}")
def save_subject_sync_rule(school_id: int, learning_area_id: int, request: Request, day_of_week: str = Form(""), period_id: str = Form("")):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            if not day_of_week or not period_id:
                cur.execute("DELETE FROM subject_sync_rules WHERE school_id = %s AND learning_area_id = %s;", (school_id, learning_area_id))
            else:
                cur.execute("""
                    INSERT INTO subject_sync_rules (school_id, learning_area_id, day_of_week, period_id)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (school_id, learning_area_id) DO UPDATE SET day_of_week = EXCLUDED.day_of_week, period_id = EXCLUDED.period_id;
                """, (school_id, learning_area_id, day_of_week, int(period_id)))
            conn.commit()

    return RedirectResponse(url=f"/timetable/sync-rules/{school_id}", status_code=303)


@router.get("/timetable/constraints/{school_id}", response_class=HTMLResponse)
def subject_constraints_view(school_id: int, request: Request, grade_name: str, education_level: str, stream: str):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT id, name FROM learning_areas WHERE education_level = %s;", (education_level,))
            subjects = sort_subjects_for_display(cur.fetchall(), education_level)
            subject_names = {s['id']: s['name'] for s in subjects}

            cur.execute("""
                SELECT id, subject_a_id, subject_b_id, constraint_type FROM subject_constraints
                WHERE school_id = %s AND grade_name = %s AND education_level = %s AND stream = %s
                ORDER BY id ASC;
            """, (school_id, grade_name, education_level, stream))
            existing = cur.fetchall()

    type_labels = {
        "same_day_forbidden": "Cannot be on the same day",
        "consecutive_forbidden": "Cannot be back-to-back (consecutive periods)",
        "same_day_required": "Must be on the same day",
    }

    existing_rows = ""
    for c in existing:
        existing_rows += f"""
        <div class="flex items-center justify-between gap-2 py-2.5 border-b border-slate-50 last:border-0">
            <span class="text-xs text-slate-700">
                <b>{esc(subject_names.get(c['subject_a_id'], '?'))}</b> & <b>{esc(subject_names.get(c['subject_b_id'], '?'))}</b>
                <span class="block text-slate-400">{type_labels.get(c['constraint_type'], c['constraint_type'])}</span>
            </span>
            <form action="/api/v1/timetable/constraints/delete/{school_id}/{c['id']}" method="post" onsubmit="return confirm('Remove this constraint?');">
                <input type="hidden" name="grade_name" value="{esc(grade_name)}">
                <input type="hidden" name="education_level" value="{esc(education_level)}">
                <input type="hidden" name="stream" value="{esc(stream)}">
                <button type="submit" class="text-rose-600 hover:text-rose-800 text-xs font-bold">Remove</button>
            </form>
        </div>
        """

    subject_options = "".join(f"<option value='{s['id']}'>{esc(s['name'])}</option>" for s in subjects)
    section_label = _section_label(grade_name, stream)

    return f"""
    <!DOCTYPE html>
    <html>
    <head><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Subject Constraints</title><script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script></head>
    <body class="bg-slate-100 min-h-screen p-4 sm:p-8">
        <div class="max-w-xl mx-auto bg-white p-6 rounded-2xl border shadow-xs">
            <h2 class="text-lg font-black text-slate-800">⚙ Subject Constraints</h2>
            <p class="text-xs text-slate-400 mb-4">{esc(section_label)} ({esc(education_level)}) — rules the timetable generator and manual editor will both respect.</p>

            <div class="mb-4">{existing_rows or "<p class='text-slate-400 text-xs italic py-3'>No constraints set for this class yet.</p>"}</div>

            <form action="/api/v1/timetable/constraints/add/{school_id}" method="post" class="border-t pt-4 space-y-3">
                <input type="hidden" name="grade_name" value="{esc(grade_name)}">
                <input type="hidden" name="education_level" value="{esc(education_level)}">
                <input type="hidden" name="stream" value="{esc(stream)}">
                <div class="grid grid-cols-2 gap-3">
                    <div>
                        <label class="text-xs font-bold text-slate-600">Subject A</label>
                        <select name="subject_a_id" class="w-full border p-2 rounded-lg text-xs font-semibold bg-white mt-1" required>{subject_options}</select>
                    </div>
                    <div>
                        <label class="text-xs font-bold text-slate-600">Subject B</label>
                        <select name="subject_b_id" class="w-full border p-2 rounded-lg text-xs font-semibold bg-white mt-1" required>{subject_options}</select>
                    </div>
                </div>
                <div>
                    <label class="text-xs font-bold text-slate-600">Rule</label>
                    <select name="constraint_type" class="w-full border p-2 rounded-lg text-xs font-semibold bg-white mt-1" required>
                        <option value="same_day_forbidden">Cannot be on the same day</option>
                        <option value="consecutive_forbidden">Cannot be back-to-back (consecutive periods)</option>
                        <option value="same_day_required">Must be on the same day</option>
                    </select>
                </div>
                <div class="flex gap-3 pt-2">
                    <button type="submit" class="bg-amber-600 hover:bg-amber-700 text-white font-bold py-2.5 px-5 rounded-xl text-sm transition">Add Constraint</button>
                    <a href="/timetable/dashboard/{school_id}" class="bg-slate-200 hover:bg-slate-300 text-slate-700 font-bold py-2.5 px-5 rounded-xl text-sm transition">← Back</a>
                </div>
            </form>

            <div class="mt-6 pt-4 border-t">
                <a href="/timetable/linked-constraints/{school_id}" class="text-xs text-indigo-700 font-bold hover:underline">🔗 Need two subjects across different classes to run at the exact same time? Set that up here →</a>
            </div>
        </div>
    </body>
    </html>
    """


@router.post("/api/v1/timetable/constraints/add/{school_id}")
def add_subject_constraint(
    school_id: int,
    request: Request,
    grade_name: str = Form(...),
    education_level: str = Form(...),
    stream: str = Form(...),
    subject_a_id: int = Form(...),
    subject_b_id: int = Form(...),
    constraint_type: str = Form(...),
):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    if subject_a_id == subject_b_id:
        raise HTTPException(status_code=400, detail="Pick two different subjects for a constraint.")
    if constraint_type not in ("same_day_forbidden", "consecutive_forbidden"):
        raise HTTPException(status_code=400, detail="Unknown constraint type.")

    # Store the pair in a consistent order so (A,B) and (B,A) don't create duplicates.
    a, b = sorted([subject_a_id, subject_b_id])

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO subject_constraints (school_id, grade_name, education_level, stream, subject_a_id, subject_b_id, constraint_type)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (school_id, grade_name, education_level, stream, subject_a_id, subject_b_id, constraint_type) DO NOTHING;
            """, (school_id, grade_name, education_level, stream, a, b, constraint_type))
            conn.commit()

    encoded_grade = urllib.parse.quote(grade_name)
    encoded_level = urllib.parse.quote(education_level)
    encoded_stream = urllib.parse.quote(stream)
    return RedirectResponse(url=f"/timetable/constraints/{school_id}?grade_name={encoded_grade}&education_level={encoded_level}&stream={encoded_stream}", status_code=303)


@router.post("/api/v1/timetable/constraints/delete/{school_id}/{constraint_id}")
def delete_subject_constraint(
    school_id: int,
    constraint_id: int,
    request: Request,
    grade_name: str = Form(...),
    education_level: str = Form(...),
    stream: str = Form(...),
):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM subject_constraints WHERE id = %s AND school_id = %s;", (constraint_id, school_id))
            conn.commit()

    encoded_grade = urllib.parse.quote(grade_name)
    encoded_level = urllib.parse.quote(education_level)
    encoded_stream = urllib.parse.quote(stream)
    return RedirectResponse(url=f"/timetable/constraints/{school_id}?grade_name={encoded_grade}&education_level={encoded_level}&stream={encoded_stream}", status_code=303)


@router.get("/timetable/grade/{school_id}", response_class=HTMLResponse)
def timetable_grade_view(school_id: int, request: Request, grade_name: str, education_level: str, stream: str):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT name FROM schools WHERE id = %s;", (school_id,))
            school = cur.fetchone()

            days = get_school_days(cur, school_id)
            conn.commit()

            cur.execute("SELECT * FROM timetable_periods WHERE school_id = %s ORDER BY period_order ASC;", (school_id,))
            periods = cur.fetchall()

            cur.execute("SELECT id, name FROM learning_areas WHERE education_level = %s;", (education_level,))
            subjects = sort_subjects_for_display(cur.fetchall(), education_level)

            cur.execute("""
                SELECT ts.day_of_week, ts.period_id, ts.learning_area_id, la.name AS subject_name, u.full_name, u.email
                FROM timetable_slots ts
                LEFT JOIN learning_areas la ON ts.learning_area_id = la.id
                LEFT JOIN users u ON ts.staff_user_id = u.id
                WHERE ts.school_id = %s AND ts.grade_name = %s AND ts.education_level = %s AND ts.stream = %s;
            """, (school_id, grade_name, education_level, stream))
            slot_map = {(r['day_of_week'], r['period_id']): r for r in cur.fetchall()}

    encoded_grade = urllib.parse.quote(grade_name)
    encoded_level = urllib.parse.quote(education_level)
    encoded_stream = urllib.parse.quote(stream)
    section_label = _section_label(grade_name, stream)
    header_cells = "".join(f"<th class='p-2 text-center'>{d}</th>" for d in days)

    body_rows = ""
    for p in periods:
        if not p['is_teaching_period']:
            body_rows += f"""
            <tr class="bg-slate-50">
                <td class="p-2 text-xs font-bold text-slate-500 whitespace-nowrap">{esc(p['label'])}<br><span class="font-normal text-slate-400">{esc(p['start_time'] or '')}–{esc(p['end_time'] or '')}</span></td>
                <td colspan="{len(days)}" class="p-2 text-center text-xs italic text-slate-400">{esc(p['label'])}</td>
            </tr>
            """
            continue

        row_cells = ""
        for day in days:
            slot = slot_map.get((day, p['id']))
            current_subject_id = slot['learning_area_id'] if slot else None
            teacher_label = (slot['full_name'] or slot['email']) if slot and slot['full_name'] or (slot and slot['email']) else None
            options = "<option value=''>— Free —</option>" + "".join(
                f"<option value='{s['id']}' {'selected' if s['id'] == current_subject_id else ''}>{esc(s['name'])}</option>" for s in subjects
            )
            row_cells += f"""
            <td class="p-1.5 align-top">
                <form action="/api/v1/timetable/slot/update/{school_id}" method="post" class="space-y-1">
                    <input type="hidden" name="grade_name" value="{esc(grade_name)}">
                    <input type="hidden" name="education_level" value="{esc(education_level)}">
                    <input type="hidden" name="stream" value="{esc(stream)}">
                    <input type="hidden" name="day_of_week" value="{day}">
                    <input type="hidden" name="period_id" value="{p['id']}">
                    <select name="learning_area_id" onchange="this.form.submit()" class="w-full border p-1.5 rounded-lg text-[11px] font-semibold bg-white">{options}</select>
                    {f"<p class='text-[9px] text-slate-400 text-center truncate'>{esc(teacher_label)}</p>" if teacher_label else ""}
                </form>
            </td>
            """
        body_rows += f"""
        <tr class="border-b border-slate-100">
            <td class="p-2 text-xs font-bold text-slate-600 whitespace-nowrap align-top">{esc(p['label'])}<br><span class="font-normal text-slate-400">{esc(p['start_time'] or '')}–{esc(p['end_time'] or '')}</span></td>
            {row_cells}
        </tr>
        """

    return f"""
    <!DOCTYPE html>
    <html>
    <head><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Timetable — {esc(section_label)}</title><script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script></head>
    <body class="bg-[#F8FAFC] min-h-screen">
        <header class="bg-white border-b px-6 py-4 flex flex-col sm:flex-row sm:justify-between sm:items-center gap-3">
            <div>
                <h1 class="text-base font-bold text-slate-900">📅 {esc(section_label)} Timetable</h1>
                <p class="text-xs text-slate-400">{esc(school['name'] if school else '')} — {esc(education_level)}</p>
            </div>
            <div class="flex flex-wrap gap-2">
                <form action="/api/v1/timetable/generate/{school_id}" method="post" onsubmit="return confirm('Generate a fresh draft timetable for {esc(section_label)}? This replaces any existing entries for this class.');">
                    <input type="hidden" name="grade_name" value="{esc(grade_name)}">
                    <input type="hidden" name="education_level" value="{esc(education_level)}">
                    <input type="hidden" name="stream" value="{esc(stream)}">
                    <button type="submit" class="bg-emerald-600 hover:bg-emerald-700 text-white px-4 py-2 rounded-xl text-xs font-bold transition">🎲 Generate Draft</button>
                </form>
                <a href="/timetable/print/{school_id}?grade_name={encoded_grade}&education_level={encoded_level}&stream={encoded_stream}" target="_blank" class="bg-slate-700 hover:bg-slate-800 text-white px-4 py-2 rounded-xl text-xs font-bold transition">🖨 Print</a>
                <a href="/timetable/assignments/{school_id}?grade_name={encoded_grade}&education_level={encoded_level}&stream={encoded_stream}" class="bg-white border hover:bg-slate-50 text-slate-700 px-4 py-2 rounded-xl text-xs font-bold transition">Teachers</a>
                <a href="/timetable/dashboard/{school_id}" class="bg-white border hover:bg-slate-50 text-slate-700 px-4 py-2 rounded-xl text-xs font-bold transition">← Back</a>
            </div>
        </header>
        <div class="p-4 sm:p-8 max-w-6xl mx-auto overflow-x-auto">
            <table class="w-full border-collapse bg-white rounded-2xl overflow-hidden border shadow-xs text-xs" style="min-width:700px;">
                <thead>
                    <tr class="bg-slate-50 text-slate-500 text-[10px] font-bold uppercase tracking-wider border-b">
                        <th class="p-2 text-left">Period</th>
                        {header_cells}
                    </tr>
                </thead>
                <tbody>{body_rows}</tbody>
            </table>
        </div>
    </body>
    </html>
    """


@router.post("/api/v1/timetable/generate/{school_id}")
def generate_draft_timetable(school_id: int, request: Request, grade_name: str = Form(...), education_level: str = Form(...), stream: str = Form(...)):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            days = get_school_days(cur, school_id)
            conn.commit()

            cur.execute("SELECT id, period_order FROM timetable_periods WHERE school_id = %s AND is_teaching_period = TRUE ORDER BY period_order ASC;", (school_id,))
            teaching_periods = cur.fetchall()

            cur.execute("SELECT id, name FROM learning_areas WHERE education_level = %s;", (education_level,))
            subjects = sort_subjects_for_display(cur.fetchall(), education_level)

            cur.execute("""
                SELECT learning_area_id, staff_user_id FROM teacher_subject_assignments
                WHERE school_id = %s AND grade_name = %s AND education_level = %s AND stream = %s;
            """, (school_id, grade_name, education_level, stream))
            teacher_for_subject = {r['learning_area_id']: r['staff_user_id'] for r in cur.fetchall()}

            if not subjects or not teaching_periods:
                raise HTTPException(status_code=400, detail="No subjects or teaching periods configured — nothing to generate.")

            teaching_period_ids = {p['id'] for p in teaching_periods}

            # "Same time" rules — a subject locked to one fixed day/period
            # school-wide gets force-placed there and excluded from the
            # normal round-robin for every other slot.
            cur.execute("SELECT learning_area_id, day_of_week, period_id FROM subject_sync_rules WHERE school_id = %s;", (school_id,))
            sync_rules = {r['learning_area_id']: (r['day_of_week'], r['period_id']) for r in cur.fetchall()}

            locked_placements = {}
            free_subjects = []
            for subj in subjects:
                rule = sync_rules.get(subj['id'])
                if rule and rule[0] in days and rule[1] in teaching_period_ids:
                    locked_placements[(rule[0], rule[1])] = subj
                else:
                    free_subjects.append(subj)
            if not free_subjects:
                free_subjects = subjects  # safety net: never leave the queue empty

            # Subject "time off" — a subject marked "not_available" at a slot
            # is a hard block; "conditional" is a soft preference to avoid.
            cur.execute("""
                SELECT learning_area_id, day_of_week, period_id, status FROM subject_availability
                WHERE school_id = %s AND status IN ('not_available', 'conditional');
            """, (school_id,))
            subject_unavailable, subject_conditional = set(), set()
            for r in cur.fetchall():
                key = (r['learning_area_id'], r['day_of_week'], r['period_id'])
                (subject_unavailable if r['status'] == 'not_available' else subject_conditional).add(key)

            # Build a round-robin queue of subjects sized to fill every slot,
            # distributing periods as evenly as possible across the week.
            total_slots = len(days) * len(teaching_periods)
            queue = []
            i = 0
            while len(queue) < total_slots:
                queue.append(free_subjects[i % len(free_subjects)])
                i += 1

            # Track which teacher is already booked at each (day, period) across
            # the WHOLE school (every other grade+stream) to avoid double-booking —
            # includes existing slots for other sections already saved.
            cur.execute("""
                SELECT day_of_week, period_id, staff_user_id FROM timetable_slots
                WHERE school_id = %s AND staff_user_id IS NOT NULL
                  AND NOT (grade_name = %s AND education_level = %s AND stream = %s);
            """, (school_id, grade_name, education_level, stream))
            booked = {(r['day_of_week'], r['period_id']): r['staff_user_id'] for r in cur.fetchall()}

            # Teachers' explicit unavailability — a slot they're marked
            # "not_available" for is a hard block; "conditional" is a soft
            # preference to avoid, used only if no cleaner slot works out.
            cur.execute("""
                SELECT staff_user_id, day_of_week, period_id, status FROM teacher_availability
                WHERE school_id = %s AND status IN ('not_available', 'conditional');
            """, (school_id,))
            unavailable, conditional = set(), set()
            for r in cur.fetchall():
                key = (r['staff_user_id'], r['day_of_week'], r['period_id'])
                (unavailable if r['status'] == 'not_available' else conditional).add(key)

            # Subject placement rules ("card relationships") for this section.
            cur.execute("""
                SELECT subject_a_id, subject_b_id, constraint_type FROM subject_constraints
                WHERE school_id = %s AND grade_name = %s AND education_level = %s AND stream = %s;
            """, (school_id, grade_name, education_level, stream))
            same_day_forbidden, consecutive_forbidden = set(), set()
            for c in cur.fetchall():
                a, b = c['subject_a_id'], c['subject_b_id']
                target = same_day_forbidden if c['constraint_type'] == 'same_day_forbidden' else consecutive_forbidden
                target.add((a, b))
                target.add((b, a))

            # Clear this section's existing plan before laying down the new one.
            cur.execute("DELETE FROM timetable_slots WHERE school_id = %s AND grade_name = %s AND education_level = %s AND stream = %s;", (school_id, grade_name, education_level, stream))

            qi = 0
            for day in days:
                used_today = set()
                last_subject_id = None
                for period in teaching_periods:
                    # A subject locked to this exact day/period by a "same
                    # time" rule always wins this slot outright — it isn't
                    # drawn from the round-robin queue at all.
                    locked_subject = locked_placements.get((day, period['id']))
                    if locked_subject:
                        lcid = locked_subject['id']
                        chosen_subject = locked_subject
                        chosen_teacher = teacher_for_subject.get(lcid)
                        if chosen_teacher and booked.get((day, period['id'])) == chosen_teacher:
                            chosen_teacher = None  # conflict — place subject anyway, flagged for manual fix
                        cur.execute("""
                            INSERT INTO timetable_slots (school_id, grade_name, education_level, stream, day_of_week, period_id, learning_area_id, staff_user_id)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s);
                        """, (school_id, grade_name, education_level, stream, day, period['id'], lcid, chosen_teacher))
                        if chosen_teacher:
                            booked[(day, period['id'])] = chosen_teacher
                        used_today.add(lcid)
                        last_subject_id = lcid
                        continue

                    # Search the queue for the best candidate subject that
                    # respects: not already used today, not blocked by a
                    # same-day/consecutive constraint against what's already
                    # placed, and whose teacher (if any) isn't marked
                    # unavailable at this exact slot. Falls back to the plain
                    # round-robin pick if nothing satisfies every rule.
                    # Two passes: first try to avoid "conditional" slots for
                    # the candidate's teacher too (not just hard-unavailable
                    # ones); if nothing fits without using a conditional slot,
                    # retry allowing it — it's a soft preference, not a block.
                    chosen_idx, chosen_subject, chosen_teacher = None, None, None
                    for avoid_conditional in (True, False):
                        if chosen_subject is not None:
                            break
                        for attempt in range(len(queue)):
                            idx = (qi + attempt) % len(queue)
                            candidate = queue[idx]
                            cid = candidate['id']
                            if cid in used_today:
                                continue
                            if any((cid, other) in same_day_forbidden for other in used_today):
                                continue
                            if last_subject_id is not None and (cid, last_subject_id) in consecutive_forbidden:
                                continue
                            if (cid, day, period['id']) in subject_unavailable:
                                continue  # this subject has "time off" at this exact slot
                            if avoid_conditional and (cid, day, period['id']) in subject_conditional:
                                continue  # soft preference: try to avoid this slot first

                            cand_teacher = teacher_for_subject.get(cid)
                            if cand_teacher and (cand_teacher, day, period['id']) in unavailable:
                                continue  # try a different subject rather than use this slot
                            if cand_teacher and avoid_conditional and (cand_teacher, day, period['id']) in conditional:
                                continue  # soft preference: try to avoid this slot first
                            if cand_teacher and booked.get((day, period['id'])) == cand_teacher:
                                cand_teacher = None  # double-booked — place subject without a teacher, flagged for manual fix

                            chosen_idx, chosen_subject, chosen_teacher = idx, candidate, cand_teacher
                            break

                    if chosen_subject is None:
                        # Nothing satisfied every rule — fall back to the
                        # plain round-robin pick rather than leave a gap.
                        chosen_idx = qi % len(queue)
                        chosen_subject = queue[chosen_idx]
                        chosen_teacher = teacher_for_subject.get(chosen_subject['id'])
                        if chosen_teacher and (
                            booked.get((day, period['id'])) == chosen_teacher
                            or (chosen_teacher, day, period['id']) in unavailable
                        ):
                            chosen_teacher = None

                    qi = chosen_idx + 1
                    cur.execute("""
                        INSERT INTO timetable_slots (school_id, grade_name, education_level, stream, day_of_week, period_id, learning_area_id, staff_user_id)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s);
                    """, (school_id, grade_name, education_level, stream, day, period['id'], chosen_subject['id'], chosen_teacher))

                    if chosen_teacher:
                        booked[(day, period['id'])] = chosen_teacher
                    used_today.add(chosen_subject['id'])
                    last_subject_id = chosen_subject['id']
            conn.commit()

    encoded_grade = urllib.parse.quote(grade_name)
    encoded_level = urllib.parse.quote(education_level)
    encoded_stream = urllib.parse.quote(stream)
    return RedirectResponse(url=f"/timetable/grade/{school_id}?grade_name={encoded_grade}&education_level={encoded_level}&stream={encoded_stream}", status_code=303)


@router.post("/api/v1/timetable/slot/update/{school_id}")
def update_timetable_slot(
    school_id: int,
    request: Request,
    grade_name: str = Form(...),
    education_level: str = Form(...),
    stream: str = Form(...),
    day_of_week: str = Form(...),
    period_id: int = Form(...),
    learning_area_id: str = Form(""),
):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    encoded_grade = urllib.parse.quote(grade_name)
    encoded_level = urllib.parse.quote(education_level)
    encoded_stream = urllib.parse.quote(stream)
    redirect_url = f"/timetable/grade/{school_id}?grade_name={encoded_grade}&education_level={encoded_level}&stream={encoded_stream}"

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if not learning_area_id:
                cur.execute("""
                    DELETE FROM timetable_slots
                    WHERE school_id = %s AND grade_name = %s AND education_level = %s AND stream = %s AND day_of_week = %s AND period_id = %s;
                """, (school_id, grade_name, education_level, stream, day_of_week, period_id))
                conn.commit()
                return RedirectResponse(url=redirect_url, status_code=303)

            learning_area_id = int(learning_area_id)
            cur.execute("""
                SELECT staff_user_id FROM teacher_subject_assignments
                WHERE school_id = %s AND learning_area_id = %s AND grade_name = %s AND education_level = %s AND stream = %s;
            """, (school_id, learning_area_id, grade_name, education_level, stream))
            assignment = cur.fetchone()
            teacher_id = assignment['staff_user_id'] if assignment else None

            if teacher_id:
                cur.execute("""
                    SELECT ts.grade_name, ts.stream FROM timetable_slots ts
                    WHERE ts.school_id = %s AND ts.day_of_week = %s AND ts.period_id = %s
                      AND ts.staff_user_id = %s
                      AND NOT (ts.grade_name = %s AND ts.education_level = %s AND ts.stream = %s);
                """, (school_id, day_of_week, period_id, teacher_id, grade_name, education_level, stream))
                clash = cur.fetchone()
                if clash:
                    clash_label = _section_label(clash['grade_name'], clash['stream'])
                    raise HTTPException(
                        status_code=400,
                        detail=f"That teacher is already scheduled to teach {clash_label} at this exact day/period. Reassign the teacher for this subject, or pick a different subject for this slot."
                    )

                cur.execute("""
                    SELECT status FROM teacher_availability
                    WHERE school_id = %s AND staff_user_id = %s AND day_of_week = %s AND period_id = %s;
                """, (school_id, teacher_id, day_of_week, period_id))
                availability = cur.fetchone()
                if availability and availability['status'] == 'not_available':
                    raise HTTPException(
                        status_code=400,
                        detail="That teacher has been marked unavailable at this exact day/period. Set them to Available on their Availability page first, or pick a different teacher/subject."
                    )

            # Subject placement constraints ("card relationships") against
            # whatever else is already scheduled for this section.
            cur.execute("""
                SELECT subject_a_id, subject_b_id, constraint_type FROM subject_constraints
                WHERE school_id = %s AND grade_name = %s AND education_level = %s AND stream = %s
                  AND (subject_a_id = %s OR subject_b_id = %s);
            """, (school_id, grade_name, education_level, stream, learning_area_id, learning_area_id))
            relevant_constraints = cur.fetchall()
            if relevant_constraints:
                other_subject_ids = {c['subject_b_id'] if c['subject_a_id'] == learning_area_id else c['subject_a_id'] for c in relevant_constraints}
                constraint_type_by_other = {
                    (c['subject_b_id'] if c['subject_a_id'] == learning_area_id else c['subject_a_id']): c['constraint_type']
                    for c in relevant_constraints
                }

                cur.execute("""
                    SELECT day_of_week, period_id, learning_area_id, la.name AS subject_name
                    FROM timetable_slots ts
                    JOIN learning_areas la ON ts.learning_area_id = la.id
                    WHERE ts.school_id = %s AND ts.grade_name = %s AND ts.education_level = %s AND ts.stream = %s
                      AND ts.learning_area_id = ANY(%s)
                      AND NOT (ts.day_of_week = %s AND ts.period_id = %s);
                """, (school_id, grade_name, education_level, stream, list(other_subject_ids), day_of_week, period_id))
                for other_slot in cur.fetchall():
                    ctype = constraint_type_by_other.get(other_slot['learning_area_id'])
                    if ctype == 'same_day_forbidden' and other_slot['day_of_week'] == day_of_week:
                        raise HTTPException(
                            status_code=400,
                            detail=f"This subject can't be placed on the same day as {other_slot['subject_name']} for this class — see Constraints."
                        )
                    if ctype == 'consecutive_forbidden' and other_slot['day_of_week'] == day_of_week:
                        cur.execute("SELECT period_order FROM timetable_periods WHERE id = %s;", (period_id,))
                        this_order = cur.fetchone()['period_order']
                        cur.execute("SELECT period_order FROM timetable_periods WHERE id = %s;", (other_slot['period_id'],))
                        other_order = cur.fetchone()['period_order']
                        if abs(this_order - other_order) == 1:
                            raise HTTPException(
                                status_code=400,
                                detail=f"This subject can't be placed back-to-back with {other_slot['subject_name']} for this class — see Constraints."
                            )

            # Subject "time off" — this subject marked unavailable at this
            # exact slot is a hard block, mirroring the teacher check above.
            cur.execute("""
                SELECT status FROM subject_availability
                WHERE school_id = %s AND learning_area_id = %s AND day_of_week = %s AND period_id = %s;
            """, (school_id, learning_area_id, day_of_week, period_id))
            subj_availability = cur.fetchone()
            if subj_availability and subj_availability['status'] == 'not_available':
                raise HTTPException(
                    status_code=400,
                    detail="This subject has been marked unavailable at this exact day/period. Adjust it on the Subject Time Off page first, or pick a different slot."
                )

            # "Same time" school-wide lock — if this subject is locked to a
            # specific day/period, it can't be placed anywhere else.
            cur.execute("SELECT day_of_week, period_id FROM subject_sync_rules WHERE school_id = %s AND learning_area_id = %s;", (school_id, learning_area_id))
            sync_rule = cur.fetchone()
            if sync_rule and (sync_rule['day_of_week'] != day_of_week or sync_rule['period_id'] != period_id):
                cur.execute("SELECT label FROM timetable_periods WHERE id = %s;", (sync_rule['period_id'],))
                locked_period = cur.fetchone()
                raise HTTPException(
                    status_code=400,
                    detail=f"This subject is locked school-wide to {sync_rule['day_of_week']}, {locked_period['label'] if locked_period else 'a fixed period'} — see Same-Time Subject Rules to change it."
                )

            cur.execute("""
                INSERT INTO timetable_slots (school_id, grade_name, education_level, stream, day_of_week, period_id, learning_area_id, staff_user_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (school_id, grade_name, education_level, stream, day_of_week, period_id)
                DO UPDATE SET learning_area_id = EXCLUDED.learning_area_id, staff_user_id = EXCLUDED.staff_user_id;
            """, (school_id, grade_name, education_level, stream, day_of_week, period_id, learning_area_id, teacher_id))
            conn.commit()

    return RedirectResponse(url=redirect_url, status_code=303)


def _build_timetable_grid_html(days, periods, cell_lookup_fn):
    """Builds the <table> for a printable timetable laid out exactly like a
    physical school timetable: rows = days, columns = periods in order.
    Break periods (short break, lunch, etc.) render as one column spanning
    every day row, with the label rotated vertically. cell_lookup_fn(day,
    period) returns the inner HTML for a teaching-period cell, or None/''
    for a free slot."""
    header_cells = "".join(
        f"<th style='padding:4px 6px;{'background:#eef2f7;' if not p['is_teaching_period'] else ''}'>{esc(p['short_label'] or p['label'])}</th>"
        for p in periods
    )
    time_cells = "".join(
        f"<th style='font-weight:normal;font-size:9px;color:#64748b;'>{esc(p['start_time'] or '')}-{esc(p['end_time'] or '')}</th>"
        for p in periods
    )

    body_rows = ""
    for day_i, day in enumerate(days):
        row = f"<td style='padding:6px 8px;font-weight:bold;white-space:nowrap;border:1px solid #cbd5e1;'>{esc(day[:2].upper())}</td>"
        for p in periods:
            if not p['is_teaching_period']:
                if day_i == 0:
                    row += (
                        f"<td rowspan='{len(days)}' style='border:1px solid #cbd5e1;text-align:center;background:#f1f5f9;'>"
                        f"<div style='writing-mode:vertical-rl;transform:rotate(180deg);font-size:10px;font-weight:bold;"
                        f"color:#475569;white-space:nowrap;margin:0 auto;'>{esc(p['label'])}</div></td>"
                    )
                continue  # subsequent days: cell already covered by row 1's rowspan
            content = cell_lookup_fn(day, p) or "<span style='color:#cbd5e1;'>-</span>"
            row += f"<td style='padding:4px 6px;text-align:center;border:1px solid #e2e8f0;'>{content}</td>"
        body_rows += f"<tr>{row}</tr>"

    return f"""
    <table style="width:100%;border-collapse:collapse;font-size:11px;margin-top:14px;">
        <thead>
            <tr style="background:#f8fafc;"><th style="padding:4px 8px;"></th>{header_cells}</tr>
            <tr style="background:#f8fafc;"><th></th>{time_cells}</tr>
        </thead>
        <tbody>{body_rows}</tbody>
    </table>
    """


@router.get("/timetable/print/{school_id}", response_class=HTMLResponse)
def print_timetable(school_id: int, request: Request, grade_name: str, education_level: str, stream: str):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT name, logo_url FROM schools WHERE id = %s;", (school_id,))
            school = cur.fetchone()

            days = get_school_days(cur, school_id)

            cur.execute("SELECT * FROM timetable_periods WHERE school_id = %s ORDER BY period_order ASC;", (school_id,))
            periods = cur.fetchall()

            cur.execute("""
                SELECT ts.day_of_week, ts.period_id, la.name AS subject_name, u.full_name, u.email
                FROM timetable_slots ts
                LEFT JOIN learning_areas la ON ts.learning_area_id = la.id
                LEFT JOIN users u ON ts.staff_user_id = u.id
                WHERE ts.school_id = %s AND ts.grade_name = %s AND ts.education_level = %s AND ts.stream = %s;
            """, (school_id, grade_name, education_level, stream))
            slot_map = {(r['day_of_week'], r['period_id']): r for r in cur.fetchall()}

    section_label = _section_label(grade_name, stream)
    logo_html = ""
    if school and school.get('logo_url'):
        logo_src = school['logo_url']
        final_src = logo_src if logo_src.startswith("http") else f"/{logo_src.lstrip('/')}"
        logo_html = f"<img src='{final_src}' style='width:56px;height:56px;object-fit:contain;' />"

    def _class_cell(day, p):
        slot = slot_map.get((day, p['id']))
        if not slot or not slot['subject_name']:
            return None
        teacher_short = (slot['full_name'] or slot['email'] or "").split(" ")[-1] if (slot['full_name'] or slot['email']) else ""
        teacher_line = f"<br><span style='font-size:9px;color:#64748b;'>{esc(teacher_short)}</span>" if teacher_short else ""
        return f"<b>{esc(abbreviate_subject(slot['subject_name']))}</b>{teacher_line}"

    grid_html = _build_timetable_grid_html(days, periods, _class_cell)

    if not periods:
        grid_html = "<p style='padding:24px;text-align:center;color:#94a3b8;font-style:italic;'>No periods configured yet for this school. Set them up on the Periods &amp; Days page first.</p>"

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Timetable — {esc(section_label)}</title>
        <style>
            @page {{ size: landscape; margin: 10mm; }}
            body {{ font-family: Arial, sans-serif; padding: 20px; color: #1e293b; }}
            @media print {{ .no-print {{ display: none !important; }} }}
            th {{ background:#f8fafc; border-bottom:2px solid #cbd5e1; font-size:10px; text-transform:uppercase; color:#64748b; }}
        </style>
    </head>
    <body>
        <div class="no-print" style="text-align:right; margin-bottom:16px;">
            <button onclick="window.print()" style="background:#4f46e5;color:white;border:none;padding:10px 18px;border-radius:8px;font-weight:bold;cursor:pointer;">🖨 Print</button>
        </div>
        <div style="display:flex;align-items:center;gap:16px;border-bottom:3px double #4f46e5;padding-bottom:12px;">
            {logo_html}
            <div>
                <h1 style="margin:0;font-size:18px;">{esc(school['name'] if school else '')}</h1>
                <p style="margin:2px 0 0;font-size:15px;font-weight:bold;">CLASS TIMETABLE — {esc(section_label)} ({esc(education_level)})</p>
            </div>
        </div>
        {grid_html}
        <div style="display:flex;justify-content:space-between;margin-top:16px;font-size:9px;color:#94a3b8;">
            <span>Timetable generated: {esc(__import__('datetime').date.today().strftime('%-d/%-m/%Y'))}</span>
            <span>{esc(school['name'] if school else '')} — Timetable System</span>
        </div>
    </body>
    </html>
    """


@router.get("/timetable/print/teacher/{school_id}/{teacher_id}", response_class=HTMLResponse)
def print_teacher_timetable(school_id: int, teacher_id: int, request: Request):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT name, logo_url FROM schools WHERE id = %s;", (school_id,))
            school = cur.fetchone()

            cur.execute("SELECT full_name, email FROM users WHERE id = %s AND school_id = %s AND role = 'staff';", (teacher_id, school_id))
            teacher = cur.fetchone()
            if not teacher:
                raise HTTPException(status_code=404, detail="Teacher not found.")

            days = get_school_days(cur, school_id)

            cur.execute("SELECT * FROM timetable_periods WHERE school_id = %s ORDER BY period_order ASC;", (school_id,))
            periods = cur.fetchall()

            cur.execute("""
                SELECT ts.day_of_week, ts.period_id, ts.grade_name, ts.stream, la.name AS subject_name
                FROM timetable_slots ts
                LEFT JOIN learning_areas la ON ts.learning_area_id = la.id
                WHERE ts.school_id = %s AND ts.staff_user_id = %s;
            """, (school_id, teacher_id))
            slot_map = {(r['day_of_week'], r['period_id']): r for r in cur.fetchall()}

    teacher_name = teacher['full_name'] or teacher['email']
    logo_html = ""
    if school and school.get('logo_url'):
        logo_src = school['logo_url']
        final_src = logo_src if logo_src.startswith("http") else f"/{logo_src.lstrip('/')}"
        logo_html = f"<img src='{final_src}' style='width:56px;height:56px;object-fit:contain;' />"

    def _teacher_cell(day, p):
        slot = slot_map.get((day, p['id']))
        if not slot or not slot['subject_name']:
            return None
        class_label = _section_label(slot['grade_name'], slot['stream'])
        return f"<b>{esc(abbreviate_subject(slot['subject_name']))}</b><br><span style='font-size:9px;color:#64748b;'>{esc(class_label)}</span>"

    grid_html = _build_timetable_grid_html(days, periods, _teacher_cell)

    if not periods:
        grid_html = "<p style='padding:24px;text-align:center;color:#94a3b8;font-style:italic;'>No periods configured yet for this school. Set them up on the Periods &amp; Days page first.</p>"

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Timetable — {esc(teacher_name)}</title>
        <style>
            @page {{ size: landscape; margin: 10mm; }}
            body {{ font-family: Arial, sans-serif; padding: 20px; color: #1e293b; }}
            @media print {{ .no-print {{ display: none !important; }} }}
            th {{ background:#f8fafc; border-bottom:2px solid #cbd5e1; font-size:10px; text-transform:uppercase; color:#64748b; }}
        </style>
    </head>
    <body>
        <div class="no-print" style="text-align:right; margin-bottom:16px;">
            <button onclick="window.print()" style="background:#4f46e5;color:white;border:none;padding:10px 18px;border-radius:8px;font-weight:bold;cursor:pointer;">🖨 Print</button>
        </div>
        <div style="display:flex;align-items:center;gap:16px;border-bottom:3px double #4f46e5;padding-bottom:12px;">
            {logo_html}
            <div>
                <h1 style="margin:0;font-size:18px;">{esc(school['name'] if school else '')}</h1>
                <p style="margin:2px 0 0;font-size:15px;font-weight:bold;">TEACHER TIMETABLE — {esc(teacher_name)}</p>
            </div>
        </div>
        {grid_html}
        <div style="display:flex;justify-content:space-between;margin-top:16px;font-size:9px;color:#94a3b8;">
            <span>Timetable generated: {esc(__import__('datetime').date.today().strftime('%-d/%-m/%Y'))}</span>
            <span>{esc(school['name'] if school else '')} — Timetable System</span>
        </div>
    </body>
    </html>
    """


@router.get("/timetable/master/{school_id}", response_class=HTMLResponse)
def timetable_master_view(school_id: int, request: Request):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT name FROM schools WHERE id = %s;", (school_id,))
            school = cur.fetchone()
            if not school:
                raise HTTPException(status_code=404, detail="School not found.")

            days = get_school_days(cur, school_id)
            conn.commit()

            cur.execute("""
                SELECT DISTINCT c.grade_name, c.education_level, s.stream, c.id AS class_order
                FROM students s
                JOIN classes c ON s.class_id = c.id
                WHERE s.school_id = %s AND (s.status IS NULL OR s.status != 'GRADUATED')
                ORDER BY c.id ASC, s.stream ASC;
            """, (school_id,))
            sections = cur.fetchall()

            cur.execute("SELECT id, period_order, label FROM timetable_periods WHERE school_id = %s AND is_teaching_period = TRUE ORDER BY period_order ASC;", (school_id,))
            periods = cur.fetchall()

            cur.execute("""
                SELECT ts.grade_name, ts.education_level, ts.stream, ts.day_of_week, ts.period_id,
                       la.name AS subject_name, u.full_name AS teacher_name
                FROM timetable_slots ts
                LEFT JOIN learning_areas la ON ts.learning_area_id = la.id
                LEFT JOIN users u ON ts.staff_user_id = u.id
                WHERE ts.school_id = %s;
            """, (school_id,))
            slot_map = {}
            for row in cur.fetchall():
                key = (row['grade_name'], row['education_level'], row['stream'], row['day_of_week'], row['period_id'])
                slot_map[key] = row

    if not sections or not periods:
        body_html = "<p class='text-slate-400 text-sm italic text-center py-16'>Nothing to show yet — add students to at least one class and configure periods first.</p>"
    else:
        day_header_cells = "".join(
            f"<th colspan='{len(periods)}' style='text-align:center;border-left:2px solid #cbd5e1;'>{day}</th>"
            for day in days
        )
        period_header_cells = "".join(
            "".join(f"<th style='font-size:9px;font-weight:normal;color:#94a3b8;{'border-left:2px solid #cbd5e1;' if p_i == 0 else ''}'>{p['period_order']}</th>"
                    for p_i, p in enumerate(periods))
            for _ in days
        )

        body_rows = ""
        for sec in sections:
            row_cells = ""
            for day in days:
                for p_i, p in enumerate(periods):
                    entry = slot_map.get((sec['grade_name'], sec['education_level'], sec['stream'], day, p['id']))
                    border = "border-left:2px solid #cbd5e1;" if p_i == 0 else ""
                    if entry and entry['subject_name']:
                        label = abbreviate_subject(entry['subject_name'])
                        teacher_title = f" title='{esc(entry['teacher_name'])}'" if entry.get('teacher_name') else ""
                        row_cells += f"<td{teacher_title} style='{border}text-align:center;font-size:10px;padding:4px 2px;border-bottom:1px solid #f1f5f9;'>{esc(label)}</td>"
                    else:
                        row_cells += f"<td style='{border}background:#f8fafc;border-bottom:1px solid #f1f5f9;'></td>"
            body_rows += f"""
            <tr>
                <td style='padding:6px 8px;font-weight:bold;background:white;position:sticky;left:0;border-right:2px solid #cbd5e1;white-space:nowrap;'>{esc(_section_label(sec['grade_name'], sec['stream']))}<br><span style='font-weight:normal;color:#94a3b8;font-size:9px;'>{esc(sec['education_level'])}</span></td>
                {row_cells}
            </tr>
            """

        body_html = f"""
        <div style="overflow-x:auto; border:1px solid #e2e8f0; border-radius:12px;">
            <table style="border-collapse:collapse; font-size:11px; min-width:100%;">
                <thead>
                    <tr style="background:#f8fafc;"><th style="padding:6px 8px; text-align:left; position:sticky; left:0; background:#f8fafc; border-right:2px solid #cbd5e1;">Class</th>{day_header_cells}</tr>
                    <tr style="background:#f8fafc;"><th style="position:sticky; left:0; background:#f8fafc; border-right:2px solid #cbd5e1;"></th>{period_header_cells}</tr>
                </thead>
                <tbody>{body_rows}</tbody>
            </table>
        </div>
        """

    return HTMLResponse(f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Whole School Timetable — {esc(school['name'])}</title>
        <script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script>
    </head>
    <body class="bg-[#F8FAFC] min-h-screen">
        <header class="bg-white border-b border-slate-200/80 px-6 sm:px-8 py-4 flex flex-col sm:flex-row sm:justify-between sm:items-center gap-2">
            <div>
                <h1 class="text-base font-bold text-slate-900">🗓 Whole School Timetable — {esc(school['name'])}</h1>
                <p class="text-xs text-slate-400">Every class's week, at a glance.</p>
            </div>
            <div class="flex items-center gap-2">
                <a href="/timetable/master/print/{school_id}" target="_blank" class="bg-slate-700 hover:bg-slate-800 text-white px-4 py-2 rounded-xl text-xs font-bold text-center transition">🖨 Print</a>
                <a href="/timetable/dashboard/{school_id}" class="bg-slate-200 hover:bg-slate-300 text-slate-700 px-4 py-2 rounded-xl text-xs font-bold text-center transition">← Back</a>
            </div>
        </header>
        <div class="p-4 sm:p-8 max-w-full">
            {body_html}
        </div>
    </body>
    </html>
    """)


@router.get("/timetable/master/print/{school_id}", response_class=HTMLResponse)
def timetable_master_print(school_id: int, request: Request):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT name FROM schools WHERE id = %s;", (school_id,))
            school = cur.fetchone()
            if not school:
                raise HTTPException(status_code=404, detail="School not found.")

            days = get_school_days(cur, school_id)
            conn.commit()

            cur.execute("""
                SELECT DISTINCT c.grade_name, c.education_level, s.stream, c.id AS class_order
                FROM students s
                JOIN classes c ON s.class_id = c.id
                WHERE s.school_id = %s AND (s.status IS NULL OR s.status != 'GRADUATED')
                ORDER BY c.id ASC, s.stream ASC;
            """, (school_id,))
            sections = cur.fetchall()

            cur.execute("SELECT id, period_order FROM timetable_periods WHERE school_id = %s AND is_teaching_period = TRUE ORDER BY period_order ASC;", (school_id,))
            periods = cur.fetchall()

            cur.execute("""
                SELECT ts.grade_name, ts.education_level, ts.stream, ts.day_of_week, ts.period_id, la.name AS subject_name
                FROM timetable_slots ts
                LEFT JOIN learning_areas la ON ts.learning_area_id = la.id
                WHERE ts.school_id = %s;
            """, (school_id,))
            slot_map = {}
            for row in cur.fetchall():
                key = (row['grade_name'], row['education_level'], row['stream'], row['day_of_week'], row['period_id'])
                slot_map[key] = row

    day_header_cells = "".join(f"<th colspan='{len(periods)}' style='text-align:center;'>{day}</th>" for day in days)
    period_header_cells = "".join("".join(f"<th style='font-weight:normal;'>{p['period_order']}</th>" for p in periods) for _ in days)

    body_rows = ""
    for sec in sections:
        row_cells = ""
        for day in days:
            for p in periods:
                entry = slot_map.get((sec['grade_name'], sec['education_level'], sec['stream'], day, p['id']))
                label = abbreviate_subject(entry['subject_name']) if (entry and entry['subject_name']) else ""
                row_cells += f"<td style='text-align:center;padding:3px;'>{esc(label)}</td>"
        body_rows += f"<tr><td style='font-weight:bold;padding:4px 6px;white-space:nowrap;'>{esc(_section_label(sec['grade_name'], sec['stream']))}</td>{row_cells}</tr>"

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Whole School Timetable — {esc(school['name'])}</title>
        <style>
            @page {{ size: landscape; margin: 8mm; }}
            body {{ font-family: Arial, sans-serif; padding: 12px; color: #1e293b; }}
            @media print {{ .no-print {{ display: none !important; }} }}
            table {{ width: 100%; border-collapse: collapse; margin-top: 12px; font-size: 9px; }}
            th, td {{ border: 1px solid #cbd5e1; }}
            th {{ background:#f8fafc; }}
        </style>
    </head>
    <body>
        <div class="no-print" style="text-align:right; margin-bottom:12px;">
            <button onclick="window.print()" style="background:#4f46e5;color:white;border:none;padding:10px 18px;border-radius:8px;font-weight:bold;cursor:pointer;">🖨 Print</button>
        </div>
        <h1 style="margin:0;font-size:16px;">{esc(school['name'])} — Whole School Timetable</h1>
        <table>
            <thead>
                <tr><th>Class</th>{day_header_cells}</tr>
                <tr><th></th>{period_header_cells}</tr>
            </thead>
            <tbody>{body_rows}</tbody>
        </table>
    </body>
    </html>
    """