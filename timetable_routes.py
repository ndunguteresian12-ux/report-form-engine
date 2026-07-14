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
                    start_time VARCHAR(20),
                    end_time VARCHAR(20),
                    is_teaching_period BOOLEAN DEFAULT TRUE,
                    UNIQUE(school_id, period_order)
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
            conn.commit()


# =====================================================================
# TIMETABLING MODULE
# =====================================================================
TIMETABLE_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]


def ensure_default_periods(cur, school_id: int):
    """Creates a standard Kenyan-school-day period structure the first time
    a school opens the timetable module. Safe to call repeatedly — does
    nothing if periods already exist for this school."""
    cur.execute("SELECT COUNT(*) AS cnt FROM timetable_periods WHERE school_id = %s;", (school_id,))
    if cur.fetchone()['cnt'] > 0:
        return
    default_periods = [
        (1, "Period 1", "8:00 AM", "8:40 AM", True),
        (2, "Period 2", "8:40 AM", "9:20 AM", True),
        (3, "Short Break", "9:20 AM", "9:40 AM", False),
        (4, "Period 3", "9:40 AM", "10:20 AM", True),
        (5, "Period 4", "10:20 AM", "11:00 AM", True),
        (6, "Period 5", "11:00 AM", "11:40 AM", True),
        (7, "Lunch Break", "11:40 AM", "12:40 PM", False),
        (8, "Period 6", "12:40 PM", "1:20 PM", True),
        (9, "Period 7", "1:20 PM", "2:00 PM", True),
        (10, "Period 8", "2:00 PM", "2:40 PM", True),
    ]
    for order, label, start, end, is_teaching in default_periods:
        cur.execute("""
            INSERT INTO timetable_periods (school_id, period_order, label, start_time, end_time, is_teaching_period)
            VALUES (%s, %s, %s, %s, %s, %s);
        """, (school_id, order, label, start, end, is_teaching))


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

            ensure_default_periods(cur, school_id)
            conn.commit()

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
            <div class='grid grid-cols-2 gap-2'>
                <a href='/timetable/assignments/{school_id}?grade_name={encoded_grade}&education_level={encoded_level}&stream={encoded_stream}' class='bg-slate-700 hover:bg-slate-800 text-white text-center text-xs py-2 rounded-xl font-semibold transition'>Assign Teachers</a>
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
            <div class="flex items-center gap-2">
                <a href="/timetable/master/{school_id}" class="bg-emerald-700 hover:bg-emerald-800 text-white px-4 py-2 rounded-xl text-xs font-bold text-center transition">🗓 Whole School View</a>
                <a href="{get_dashboard_url(request, school_id)}" class="bg-slate-200 hover:bg-slate-300 text-slate-700 px-4 py-2 rounded-xl text-xs font-bold text-center transition">← Back to Dashboard</a>
            </div>
        </header>
        <div class="p-6 sm:p-8 max-w-6xl mx-auto">
            <div class="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 gap-4">
                {section_cards or "<p class='text-slate-400 text-xs italic col-span-full text-center py-8 bg-white border border-dashed rounded-2xl'>No classes with students yet — add students first.</p>"}
            </div>
        </div>
    </body>
    </html>
    """)


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


@router.get("/timetable/grade/{school_id}", response_class=HTMLResponse)
def timetable_grade_view(school_id: int, request: Request, grade_name: str, education_level: str, stream: str):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT name FROM schools WHERE id = %s;", (school_id,))
            school = cur.fetchone()

            ensure_default_periods(cur, school_id)
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
    header_cells = "".join(f"<th class='p-2 text-center'>{d}</th>" for d in TIMETABLE_DAYS)

    body_rows = ""
    for p in periods:
        if not p['is_teaching_period']:
            body_rows += f"""
            <tr class="bg-slate-50">
                <td class="p-2 text-xs font-bold text-slate-500 whitespace-nowrap">{esc(p['label'])}<br><span class="font-normal text-slate-400">{esc(p['start_time'] or '')}–{esc(p['end_time'] or '')}</span></td>
                <td colspan="{len(TIMETABLE_DAYS)}" class="p-2 text-center text-xs italic text-slate-400">{esc(p['label'])}</td>
            </tr>
            """
            continue

        row_cells = ""
        for day in TIMETABLE_DAYS:
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
            ensure_default_periods(cur, school_id)
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

            # Build a round-robin queue of subjects sized to fill every slot,
            # distributing periods as evenly as possible across the week.
            total_slots = len(TIMETABLE_DAYS) * len(teaching_periods)
            queue = []
            i = 0
            while len(queue) < total_slots:
                queue.append(subjects[i % len(subjects)])
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

            # Clear this section's existing plan before laying down the new one.
            cur.execute("DELETE FROM timetable_slots WHERE school_id = %s AND grade_name = %s AND education_level = %s AND stream = %s;", (school_id, grade_name, education_level, stream))

            qi = 0
            for day in TIMETABLE_DAYS:
                used_today = set()
                for period in teaching_periods:
                    # Try to avoid repeating the same subject twice in one day
                    # where a different option exists in the queue.
                    attempts = 0
                    subject = queue[qi % len(queue)]
                    while subject['id'] in used_today and attempts < len(queue):
                        qi += 1
                        subject = queue[qi % len(queue)]
                        attempts += 1

                    teacher_id = teacher_for_subject.get(subject['id'])
                    # If that teacher's already booked elsewhere this exact
                    # day/period, place the subject anyway but without a
                    # teacher attached — flagged for manual resolution rather
                    # than silently double-booking someone.
                    if teacher_id and booked.get((day, period['id'])) == teacher_id:
                        teacher_id = None

                    cur.execute("""
                        INSERT INTO timetable_slots (school_id, grade_name, education_level, stream, day_of_week, period_id, learning_area_id, staff_user_id)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s);
                    """, (school_id, grade_name, education_level, stream, day, period['id'], subject['id'], teacher_id))

                    if teacher_id:
                        booked[(day, period['id'])] = teacher_id
                    used_today.add(subject['id'])
                    qi += 1
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
                INSERT INTO timetable_slots (school_id, grade_name, education_level, stream, day_of_week, period_id, learning_area_id, staff_user_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (school_id, grade_name, education_level, stream, day_of_week, period_id)
                DO UPDATE SET learning_area_id = EXCLUDED.learning_area_id, staff_user_id = EXCLUDED.staff_user_id;
            """, (school_id, grade_name, education_level, stream, day_of_week, period_id, learning_area_id, teacher_id))
            conn.commit()

    return RedirectResponse(url=redirect_url, status_code=303)


@router.get("/timetable/print/{school_id}", response_class=HTMLResponse)
def print_timetable(school_id: int, request: Request, grade_name: str, education_level: str, stream: str):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT name, logo_url FROM schools WHERE id = %s;", (school_id,))
            school = cur.fetchone()

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

    header_cells = "".join(f"<th style='padding:6px 8px;text-align:center;'>{d}</th>" for d in TIMETABLE_DAYS)
    body_rows = ""
    for p in periods:
        if not p['is_teaching_period']:
            body_rows += f"<tr style='background:#f8fafc;'><td style='padding:6px 8px;font-weight:bold;'>{esc(p['label'])}</td><td colspan='{len(TIMETABLE_DAYS)}' style='text-align:center;font-style:italic;color:#94a3b8;'>{esc(p['label'])}</td></tr>"
            continue
        cells = ""
        for day in TIMETABLE_DAYS:
            slot = slot_map.get((day, p['id']))
            if slot and slot['subject_name']:
                teacher = slot['full_name'] or slot['email'] or ""
                cells += f"<td style='padding:6px 8px;text-align:center;border:1px solid #e2e8f0;'><b>{esc(slot['subject_name'])}</b><br><span style='font-size:9px;color:#64748b;'>{esc(teacher)}</span></td>"
            else:
                cells += "<td style='padding:6px 8px;text-align:center;border:1px solid #e2e8f0;color:#cbd5e1;'>-</td>"
        body_rows += f"<tr><td style='padding:6px 8px;font-weight:bold;border:1px solid #e2e8f0;white-space:nowrap;'>{esc(p['label'])}<br><span style='font-weight:normal;color:#64748b;font-size:9px;'>{esc(p['start_time'] or '')}–{esc(p['end_time'] or '')}</span></td>{cells}</tr>"

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
            table {{ width: 100%; border-collapse: collapse; margin-top: 14px; font-size: 11px; }}
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
                <p style="margin:2px 0 0;font-size:13px;font-weight:bold;">CLASS TIMETABLE — {esc(section_label)} ({esc(education_level)})</p>
            </div>
        </div>
        <table>
            <thead><tr><th style="text-align:left;padding:6px 8px;">Period</th>{header_cells}</tr></thead>
            <tbody>{body_rows}</tbody>
        </table>
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

            ensure_default_periods(cur, school_id)
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
            for day in TIMETABLE_DAYS
        )
        period_header_cells = "".join(
            "".join(f"<th style='font-size:9px;font-weight:normal;color:#94a3b8;{'border-left:2px solid #cbd5e1;' if p_i == 0 else ''}'>{p['period_order']}</th>"
                    for p_i, p in enumerate(periods))
            for _ in TIMETABLE_DAYS
        )

        body_rows = ""
        for sec in sections:
            row_cells = ""
            for day in TIMETABLE_DAYS:
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

            ensure_default_periods(cur, school_id)
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

    day_header_cells = "".join(f"<th colspan='{len(periods)}' style='text-align:center;'>{day}</th>" for day in TIMETABLE_DAYS)
    period_header_cells = "".join("".join(f"<th style='font-weight:normal;'>{p['period_order']}</th>" for p in periods) for _ in TIMETABLE_DAYS)

    body_rows = ""
    for sec in sections:
        row_cells = ""
        for day in TIMETABLE_DAYS:
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
