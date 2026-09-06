"""
notifications_routes.py — bulk SMS to parents: progress-report summaries
and general custom announcements.

Kept entirely separate from main.py, matching the same pattern as
timetable_routes.py / finance_routes.py / schemes_routes.py /
student_portal_routes.py: a distinct router, included into the main app.

Two things live here:
  1. Send Progress Reports — a concise, personalized SMS per student
     summarizing their current assessment cycle's results, sent to
     whichever parent phone number(s) are on file.
  2. Send Custom Notification — a free-text message an admin writes once
     and sends to every parent phone number for a chosen audience (a
     specific class, or the whole school).

Both show exactly how many messages will actually go out BEFORE
sending — SMS costs real money per message, so there's no "oops, that
just sent 400 texts" surprise. Both run the actual sending as a
background task so the request returns immediately rather than risking
a timeout waiting for every message to finish for a large school.

WhatsApp is NOT built here. That needs a real WhatsApp Business API
account (Meta Cloud API or a provider like Twilio) — a separate piece of
infrastructure to set up, the same way SMTP needed real Gmail
credentials before email could work. SMS is what's ready to build on
right now.
"""

import urllib.parse
from fastapi import APIRouter, Request, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse
from psycopg2.extras import RealDictCursor

from shared import (
    esc, get_db_connection, send_sms,
    require_school_session, require_admin_session, full_student_name,
    get_current_session_user, get_teacher_class_keys, teacher_can_access_class,
)

router = APIRouter()

# Temporarily disabled while switching SMS providers to Celcom Africa —
# all the working code below (Progress Reports, Custom Notification) is
# left completely intact, just gated behind this single flag. Flip back
# to True once the new provider is actually configured; nothing else
# needs to change.
NOTIFICATIONS_FEATURE_ENABLED = False


def _coming_soon_page(title: str) -> HTMLResponse:
    return HTMLResponse(f"""
    <!DOCTYPE html>
    <html>
    <head><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Elimu Hub | {esc(title)}</title><script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script></head>
    <body class="bg-slate-100 min-h-screen flex items-center justify-center p-4">
        <div class="bg-white p-8 rounded-2xl border shadow-xs w-full max-w-md text-center">
            <p class="text-4xl mb-3">🚧</p>
            <h2 class="text-lg font-black text-slate-800">{esc(title)}</h2>
            <p class="text-sm text-slate-500 mt-2">Coming soon — we're setting up a new SMS provider to make this even more reliable. Thanks for your patience!</p>
        </div>
    </body>
    </html>
    """)


def bootstrap_notifications_schema():
    """One new table: a log of every bulk send, for accountability and so
    re-visiting this page doesn't leave an admin guessing whether
    something was already sent. Called once at startup from main.py,
    same pattern as the other modules' bootstrap_*_schema functions."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS notification_log (
                    id SERIAL PRIMARY KEY,
                    school_id INTEGER REFERENCES schools(id) ON DELETE CASCADE,
                    kind VARCHAR(30) NOT NULL,
                    grade_name VARCHAR(100),
                    education_level VARCHAR(100),
                    stream VARCHAR(100),
                    message_preview TEXT,
                    recipient_count INTEGER NOT NULL DEFAULT 0,
                    sent_count INTEGER NOT NULL DEFAULT 0,
                    failed_count INTEGER NOT NULL DEFAULT 0,
                    sent_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    created_at TIMESTAMP DEFAULT NOW()
                );
            """)
            conn.commit()


def _build_progress_report_message(school_name: str, student_name: str, cycle: str, term: str, year: int, avg_score: float, subject_count: int) -> str:
    """Deliberately concise — a full per-subject breakdown would run to
    several SMS segments (each one costing extra) for a typical 8-9
    subject CBC learner. A short summary that points to the portal for
    the real detail is both cheaper and reinforces actually using the
    portal, rather than trying to cram a whole report card into a text
    message."""
    # Plain ASCII only, deliberately — an em-dash or other non-GSM-7
    # character would silently force the whole message into UCS-2
    # encoding (70 chars/segment instead of 160), roughly doubling the
    # real cost of every single message sent through this function.
    return (
        f"{school_name}: {student_name}'s {cycle} results for {term} {year} - "
        f"average {avg_score:.0f}% across {subject_count} subject(s). "
        f"Log in to the Elimu Hub Learner Portal for the full breakdown."
    )


def _send_bulk_sms_task(recipients: list, message_fn, log_id: int):
    """Runs as a background task — sends one SMS per recipient (a
    (phone_number, format_kwargs) pair), then updates the log row with
    final counts once every message has been attempted. Kept as a plain
    function (not async) since send_sms itself is a synchronous, blocking
    HTTP call — FastAPI's BackgroundTasks runs this in a worker thread,
    so it doesn't block the response that already went back to the admin."""
    sent, failed = 0, 0
    for phone, kwargs in recipients:
        msg = message_fn(**kwargs) if callable(message_fn) else message_fn
        if send_sms(phone, msg):
            sent += 1
        else:
            failed += 1

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE notification_log SET sent_count = %s, failed_count = %s WHERE id = %s;", (sent, failed, log_id))
            conn.commit()


def _parent_phones_for_student(student: dict) -> list:
    """Both phone numbers if both are on file, deduplicated (a family
    that entered the same number for both mother and father shouldn't
    get charged twice for the same message)."""
    phones = {student.get('mother_phone'), student.get('father_phone')}
    return [p for p in phones if p]


# ============================================================
# Send Progress Reports — a personalized SMS per student,
# summarizing their current assessment cycle's results.
# ============================================================

@router.get("/admin/notifications/progress-reports/{school_id}", response_class=HTMLResponse)
def progress_reports_preview(school_id: int, request: Request, grade_name: str, education_level: str, stream: str):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error
    if not NOTIFICATIONS_FEATURE_ENABLED:
        return _coming_soon_page("Send Progress Reports")

    viewer = get_current_session_user(request)
    if viewer and viewer.get('role') == 'staff':
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                class_keys = get_teacher_class_keys(cur, school_id, viewer['id'])
        if not teacher_can_access_class(class_keys, grade_name, education_level, stream):
            raise HTTPException(status_code=403, detail="You're not connected to this class.")

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT name FROM schools WHERE id = %s;", (school_id,))
            school = cur.fetchone()

            cur.execute("SELECT active_term, active_year, active_cycle FROM school_settings WHERE school_id = %s;", (school_id,))
            settings = cur.fetchone() or {}
            active_term = settings.get('active_term') or 'Term 1'
            active_year = settings.get('active_year') or 2026
            active_cycle = settings.get('active_cycle') or 'Opener'

            cur.execute("""
                SELECT s.id, s.admission_number, s.first_name, s.middle_name, s.last_name, s.mother_phone, s.father_phone,
                       AVG(sc.raw_score) AS avg_score, COUNT(sc.id) AS subject_count
                FROM students s
                JOIN classes c ON s.class_id = c.id
                LEFT JOIN student_scores sc ON sc.student_id = s.id AND sc.term = %s AND sc.year = %s AND sc.cycle_name = %s
                WHERE s.school_id = %s AND c.grade_name = %s AND c.education_level = %s
                  AND (%s = 'SINGLE STREAM' OR s.stream = %s) AND (s.status IS NULL OR s.status != 'GRADUATED')
                GROUP BY s.id
                ORDER BY s.admission_number ASC;
            """, (active_term, active_year, active_cycle, school_id, grade_name, education_level, stream, stream))
            students = cur.fetchall()

    has_marks = [s for s in students if s['subject_count'] and s['subject_count'] > 0]
    recipient_phones = set()
    for s in has_marks:
        recipient_phones.update(_parent_phones_for_student(s))

    rows_html = "".join(f"""
        <tr class="border-b border-slate-100 text-sm">
            <td class="p-2.5 font-semibold text-slate-700">{esc(full_student_name(s))}</td>
            <td class="p-2.5 text-center">{f"{float(s['avg_score']):.0f}%" if s['avg_score'] is not None else "<span class='text-slate-300 italic text-xs'>No marks yet</span>"}</td>
            <td class="p-2.5 text-center">{len(_parent_phones_for_student(s))} phone(s)</td>
        </tr>
        """ for s in students)

    section_label = grade_name if (not stream or stream.upper() == "SINGLE STREAM") else f"{grade_name} {stream}"
    encoded_grade, encoded_level, encoded_stream = urllib.parse.quote(grade_name), urllib.parse.quote(education_level), urllib.parse.quote(stream)

    return f"""
    <!DOCTYPE html>
    <html>
    <head><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Elimu Hub | Send Progress Reports</title><script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script></head>
    <body class="bg-slate-100 min-h-screen p-4 sm:p-8">
        <div class="max-w-2xl mx-auto space-y-4">
            <div class="bg-white p-6 rounded-2xl border shadow-xs">
                <h2 class="text-lg font-black text-slate-800">📲 Send Progress Reports</h2>
                <p class="text-xs text-slate-400">{esc(section_label)} — {esc(active_cycle)}, {esc(active_term)} {active_year}</p>
            </div>

            <div class="bg-indigo-50 border border-indigo-200 rounded-2xl p-4">
                <p class="text-sm font-bold text-indigo-800">{len(recipient_phones)} SMS will be sent</p>
                <p class="text-xs text-indigo-600 mt-1">{len(has_marks)} of {len(students)} learner(s) have marks recorded for {esc(active_cycle)}. Only they'll get a message — the rest have nothing to report yet.</p>
            </div>

            <div class="bg-white rounded-2xl border shadow-xs overflow-hidden">
                <table class="w-full text-left border-collapse">
                    <thead><tr class="border-b-2 text-[11px] uppercase text-slate-400"><th class="p-2.5">Learner</th><th class="p-2.5 text-center">{esc(active_cycle)} Average</th><th class="p-2.5 text-center">Parent Contacts</th></tr></thead>
                    <tbody>{rows_html or "<tr><td colspan='3' class='p-4 text-center text-slate-400 italic text-xs'>No learners in this class.</td></tr>"}</tbody>
                </table>
            </div>

            <form action="/api/v1/notifications/progress-reports/{school_id}" method="post" onsubmit="return confirm('Send {len(recipient_phones)} SMS now? This cannot be undone once sent.');">
                <input type="hidden" name="grade_name" value="{esc(grade_name)}">
                <input type="hidden" name="education_level" value="{esc(education_level)}">
                <input type="hidden" name="stream" value="{esc(stream)}">
                <button type="submit" {"disabled" if not recipient_phones else ""} class="w-full bg-indigo-700 hover:bg-indigo-800 disabled:bg-slate-300 disabled:cursor-not-allowed text-white font-bold py-3 rounded-xl text-sm transition">Send {len(recipient_phones)} SMS Now</button>
            </form>
        </div>
    </body>
    </html>
    """


@router.post("/api/v1/notifications/progress-reports/{school_id}")
async def progress_reports_send(school_id: int, request: Request, background_tasks: BackgroundTasks):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error
    if not NOTIFICATIONS_FEATURE_ENABLED:
        raise HTTPException(status_code=503, detail="Sending progress reports is temporarily unavailable while we switch SMS providers. Coming back soon!")

    form = await request.form()
    grade_name = form.get("grade_name", "")
    education_level = form.get("education_level", "")
    stream = form.get("stream", "") or "SINGLE STREAM"

    viewer = get_current_session_user(request)
    if viewer and viewer.get('role') == 'staff':
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                class_keys = get_teacher_class_keys(cur, school_id, viewer['id'])
        if not teacher_can_access_class(class_keys, grade_name, education_level, stream):
            raise HTTPException(status_code=403, detail="You're not connected to this class.")

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT name FROM schools WHERE id = %s;", (school_id,))
            school = cur.fetchone()

            cur.execute("SELECT active_term, active_year, active_cycle FROM school_settings WHERE school_id = %s;", (school_id,))
            settings = cur.fetchone() or {}
            active_term = settings.get('active_term') or 'Term 1'
            active_year = settings.get('active_year') or 2026
            active_cycle = settings.get('active_cycle') or 'Opener'

            cur.execute("""
                SELECT s.id, s.first_name, s.middle_name, s.last_name, s.mother_phone, s.father_phone,
                       AVG(sc.raw_score) AS avg_score, COUNT(sc.id) AS subject_count
                FROM students s
                JOIN classes c ON s.class_id = c.id
                JOIN student_scores sc ON sc.student_id = s.id AND sc.term = %s AND sc.year = %s AND sc.cycle_name = %s
                WHERE s.school_id = %s AND c.grade_name = %s AND c.education_level = %s
                  AND (%s = 'SINGLE STREAM' OR s.stream = %s) AND (s.status IS NULL OR s.status != 'GRADUATED')
                GROUP BY s.id;
            """, (active_term, active_year, active_cycle, school_id, grade_name, education_level, stream, stream))
            students = cur.fetchall()

            recipients = []
            for s in students:
                student_name = full_student_name(s)
                for phone in _parent_phones_for_student(s):
                    recipients.append((phone, {
                        'school_name': school['name'] if school else 'Elimu Hub',
                        'student_name': student_name,
                        'cycle': active_cycle, 'term': active_term, 'year': active_year,
                        'avg_score': float(s['avg_score']), 'subject_count': s['subject_count'],
                    }))

            cur.execute("""
                INSERT INTO notification_log (school_id, kind, grade_name, education_level, stream, message_preview, recipient_count, sent_by_user_id)
                VALUES (%s, 'progress_report', %s, %s, %s, %s, %s, %s) RETURNING id;
            """, (school_id, grade_name, education_level, stream, f"Progress reports for {active_cycle} {active_term} {active_year}", len(recipients), viewer['id'] if viewer else None))
            log_id = cur.fetchone()['id']
            conn.commit()

    background_tasks.add_task(_send_bulk_sms_task, recipients, _build_progress_report_message, log_id)

    encoded_grade, encoded_level, encoded_stream = urllib.parse.quote(grade_name), urllib.parse.quote(education_level), urllib.parse.quote(stream)
    return RedirectResponse(url=f"/admin/notifications/progress-reports/{school_id}?grade_name={encoded_grade}&education_level={encoded_level}&stream={encoded_stream}&sending=1", status_code=303)


# ============================================================
# Send Custom Notification — one free-text message to a chosen
# audience: a specific class, or (admin only) the whole school.
# ============================================================

@router.get("/admin/notifications/custom/{school_id}", response_class=HTMLResponse)
def custom_notification_form(school_id: int, request: Request, sent: str = None):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error
    if not NOTIFICATIONS_FEATURE_ENABLED:
        return _coming_soon_page("Send Notification")

    viewer = get_current_session_user(request)
    is_admin = not (viewer and viewer.get('role') == 'staff')

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT name FROM schools WHERE id = %s;", (school_id,))
            school = cur.fetchone()

            if not is_admin:
                class_keys = get_teacher_class_keys(cur, school_id, viewer['id'])
            cur.execute("""
                SELECT DISTINCT c.grade_name, c.education_level, COALESCE(s.stream, 'SINGLE STREAM') AS stream
                FROM classes c LEFT JOIN students s ON s.class_id = c.id AND s.school_id = %s AND (s.status IS NULL OR s.status != 'GRADUATED')
                ORDER BY c.grade_name ASC, stream ASC;
            """, (school_id,))
            all_classes = cur.fetchall()

    if not is_admin:
        allowed_classes = [c for c in all_classes if teacher_can_access_class(class_keys, c['grade_name'], c['education_level'], c['stream'])]
    else:
        allowed_classes = all_classes

    class_options = "".join(
        f"<option value='{esc(c['grade_name'])}|{esc(c['education_level'])}|{esc(c['stream'])}'>{esc(c['grade_name'])}{' ' + esc(c['stream']) if c['stream'] != 'SINGLE STREAM' else ''} ({esc(c['education_level'])})</option>"
        for c in allowed_classes
    )

    return f"""
    <!DOCTYPE html>
    <html>
    <head><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Elimu Hub | Send Notification</title><script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script></head>
    <body class="bg-slate-100 min-h-screen p-4 sm:p-8">
        <div class="max-w-lg mx-auto space-y-4">
            <div class="bg-white p-6 rounded-2xl border shadow-xs">
                <h2 class="text-lg font-black text-slate-800">📢 Send Notification</h2>
                <p class="text-xs text-slate-400">{esc(school['name'] if school else '')}</p>
            </div>

            {"<div class='bg-emerald-50 border border-emerald-200 text-emerald-800 text-xs px-4 py-2.5 rounded-xl'>✅ Sending now in the background — check back shortly to confirm delivery counts.</div>" if sent else ""}

            <form action="/api/v1/notifications/custom/{school_id}" method="post" class="bg-white p-6 rounded-2xl border shadow-xs space-y-3" onsubmit="return confirm('Send this message now? SMS costs apply per recipient, and this cannot be undone once sent.');">
                <div>
                    <label class="text-xs font-bold text-slate-600">Audience</label>
                    <select name="audience" class="w-full border p-2.5 rounded-lg mt-1 text-sm bg-white" required>
                        {"<option value='WHOLE_SCHOOL'>Whole School</option>" if is_admin else ""}
                        {class_options}
                    </select>
                </div>
                <div>
                    <label class="text-xs font-bold text-slate-600">Message</label>
                    <textarea name="message" rows="4" maxlength="480" placeholder="e.g. School closes for half-term break this Friday, 12th June. Learners resume on Monday, 22nd June." class="w-full border p-2.5 rounded-lg mt-1 text-sm" required></textarea>
                    <p class="text-[10px] text-slate-400 mt-1">Kept under 480 characters (about 3 SMS segments) — longer messages cost more per recipient.</p>
                </div>
                <button type="submit" class="w-full bg-indigo-700 hover:bg-indigo-800 text-white font-bold py-2.5 rounded-lg text-sm transition">Send</button>
            </form>
        </div>
    </body>
    </html>
    """


@router.post("/api/v1/notifications/custom/{school_id}")
async def custom_notification_send(school_id: int, request: Request, background_tasks: BackgroundTasks):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error
    if not NOTIFICATIONS_FEATURE_ENABLED:
        raise HTTPException(status_code=503, detail="Sending notifications is temporarily unavailable while we switch SMS providers. Coming back soon!")

    form = await request.form()
    audience = (form.get("audience") or "").strip()
    message = (form.get("message") or "").strip()[:480]
    if not audience or not message:
        raise HTTPException(status_code=400, detail="Audience and message are both required.")

    viewer = get_current_session_user(request)
    is_admin = not (viewer and viewer.get('role') == 'staff')

    if audience == "WHOLE_SCHOOL" and not is_admin:
        raise HTTPException(status_code=403, detail="Only an admin can message the whole school.")

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if audience == "WHOLE_SCHOOL":
                grade_name, education_level, stream = None, None, None
                cur.execute("""
                    SELECT s.mother_phone, s.father_phone FROM students s
                    WHERE s.school_id = %s AND (s.status IS NULL OR s.status != 'GRADUATED');
                """, (school_id,))
            else:
                grade_name, education_level, stream = audience.split("|")
                if not is_admin:
                    class_keys = get_teacher_class_keys(cur, school_id, viewer['id'])
                    if not teacher_can_access_class(class_keys, grade_name, education_level, stream):
                        raise HTTPException(status_code=403, detail="You're not connected to this class.")
                cur.execute("""
                    SELECT s.mother_phone, s.father_phone FROM students s
                    JOIN classes c ON s.class_id = c.id
                    WHERE s.school_id = %s AND c.grade_name = %s AND c.education_level = %s
                      AND (%s = 'SINGLE STREAM' OR s.stream = %s) AND (s.status IS NULL OR s.status != 'GRADUATED');
                """, (school_id, grade_name, education_level, stream, stream))
            student_phone_rows = cur.fetchall()

            # Deduplicated across every student — unlike progress reports
            # (a personalized message per learner, where a shared phone
            # correctly gets one message per child), this is the exact
            # same message for everyone, so a parent with two children in
            # the audience should only be charged for it once.
            unique_phones = set()
            for r in student_phone_rows:
                unique_phones.update(p for p in (r['mother_phone'], r['father_phone']) if p)
            recipients = [(phone, {}) for phone in unique_phones]

            cur.execute("""
                INSERT INTO notification_log (school_id, kind, grade_name, education_level, stream, message_preview, recipient_count, sent_by_user_id)
                VALUES (%s, 'custom', %s, %s, %s, %s, %s, %s) RETURNING id;
            """, (school_id, grade_name, education_level, stream, message[:200], len(recipients), viewer['id'] if viewer else None))
            log_id = cur.fetchone()['id']
            conn.commit()

    background_tasks.add_task(_send_bulk_sms_task, recipients, message, log_id)

    return RedirectResponse(url=f"/admin/notifications/custom/{school_id}?sent=1", status_code=303)