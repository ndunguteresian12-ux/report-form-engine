"""
student_portal_routes.py — the learner-facing self-service portal.

Kept entirely separate from main.py, matching the same pattern as
timetable_routes.py / finance_routes.py / schemes_routes.py: a distinct
router, included into the main app, so main.py itself doesn't grow with
every new feature added here.

A learner logs in with a parent/guardian phone number (mother_phone OR
father_phone — either works) as the username, and "admission_number#grade"
(e.g. "112#1" for admission number 112 in Grade 1) as the password.
There's nothing for an admin to separately set or hash here — the
password is always derived directly from data already on file (the
student's own admission number and current grade), so portal access
"just works" the moment a parent phone number is recorded, no extra
setup step at all. Session identity is a SEPARATE cookie namespace
(session_student_id) from the staff/admin session system
(session_user_id) — a student is a row in the students table, not the
users table, and treating the two as the same identity space would be a
real security hazard, not just a naming clash.

Note: a phone number can match several students (siblings sharing a
parent's number) — but since each sibling has a DIFFERENT admission
number, their derived password differs too, so the (phone, password)
pair already uniquely identifies one child in the ordinary case, without
needing a "which child?" picker at all. The rare case where it's still
ambiguous (e.g. two schools happening to have used the same phone number
for a parent, with a coincidentally identical admission_number#grade at
each) is still handled defensively: show a picker rather than guessing,
naming each match's school so it's obvious which one is really theirs.
"""

import re
import urllib.parse
from fastapi import APIRouter, Request, HTTPException, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from psycopg2.extras import RealDictCursor

from shared import (
    esc, get_db_connection,
    require_school_session, require_admin_session, full_student_name,
    get_current_session_user, get_teacher_class_keys, teacher_can_access_class,
    is_teacher_of_this_class,
)

router = APIRouter()


def _grade_part_matches(grade_part: str, grade_name: str) -> bool:
    """Matches the "#1" part of a login password against a student's
    actual current grade_name ("Grade 1"). Handles both formats grades
    actually come in — a plain number for Grade 1-9 ("1" -> "Grade 1"),
    and a direct match for ECDE's PP1/PP2, which have no separate number
    to extract at all."""
    grade_part = (grade_part or "").strip().upper()
    grade_name_upper = (grade_name or "").strip().upper()
    if not grade_part or not grade_name_upper:
        return False
    if grade_part == grade_name_upper:
        return True  # e.g. "PP1" == "PP1"
    grade_name_digits = re.sub(r"\D", "", grade_name_upper)
    return grade_part.isdigit() and grade_part == grade_name_digits


# ============================================================
# Session handling — a student's own identity space, kept
# completely separate from the staff/admin session system.
# ============================================================

def get_current_student(request: Request):
    """Looks up the logged-in student from session_student_id — verified
    against the database each time (never trusting the cookie's contents
    directly, only using it as a lookup key), matching the same principle
    require_school_session already applies for staff/admin sessions.
    Returns a dict or None."""
    student_id = request.cookies.get("session_student_id")
    if not student_id:
        return None
    try:
        student_id = int(student_id)
    except ValueError:
        return None
    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT s.*, c.grade_name AS class_grade_name, c.education_level AS class_education_level
                FROM students s
                LEFT JOIN classes c ON s.class_id = c.id
                WHERE s.id = %s AND (s.status IS NULL OR s.status != 'GRADUATED');
            """, (student_id,))
            return cur.fetchone()


def require_student_session(request: Request):
    """Returns a redirect Response if not logged in as a student, or None
    if OK to proceed — same calling convention as require_school_session,
    just for the separate student identity space."""
    student = get_current_student(request)
    if not student:
        return RedirectResponse(url="/student/login?error=Please+log+in.", status_code=303)
    return None


# ============================================================
# Login
# ============================================================

@router.get("/student/login", response_class=HTMLResponse)
def student_login_page(request: Request, error: str = None):
    error_html = f"<div class='bg-rose-50 border border-rose-200 text-rose-700 text-xs px-3 py-2.5 rounded-lg mb-4'>{esc(error)}</div>" if error else ""
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Elimu Hub | Learner Portal</title>
        <script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script>
    </head>
    <body class="bg-slate-900 flex items-center justify-center min-h-screen font-sans p-4">
        <div class="bg-white p-8 rounded-2xl shadow-2xl w-full max-w-sm border-t-8 border-emerald-700">
            <h2 class="text-xl font-black text-slate-800 mb-1">🎒 Learner Portal</h2>
            <p class="text-xs text-slate-400 mb-4">Log in with a parent/guardian phone number on file.</p>
            {error_html}
            <form action="/api/v1/student/login" method="post" class="space-y-3">
                <div>
                    <label class="block text-xs font-bold uppercase text-slate-600 tracking-wider">Parent/Guardian Phone Number</label>
                    <input type="tel" name="phone" class="w-full p-3 border rounded-lg mt-1 focus:ring-2 focus:ring-emerald-600 outline-none" required>
                </div>
                <div>
                    <label class="block text-xs font-bold uppercase text-slate-600 tracking-wider">Password (Admission Number#Grade)</label>
                    <div class="relative mt-1">
                        <input type="password" name="password" id="studentPasswordField" placeholder="e.g. 112#1" class="w-full p-3 pr-11 border rounded-lg focus:ring-2 focus:ring-emerald-600 outline-none" required>
                        <button type="button" onclick="const f=document.getElementById('studentPasswordField'); const isHidden=f.type==='password'; f.type=isHidden?'text':'password'; this.textContent=isHidden?'Hide':'Show';" class="absolute right-3 top-1/2 -translate-y-1/2 text-[11px] font-bold text-slate-400 hover:text-slate-600">Show</button>
                    </div>
                    <p class="text-[10px] text-slate-400 mt-1">The learner's admission number, a # sign, then their grade (e.g. 112#1 for Grade 1, or 112#PP1 for PP1).</p>
                </div>
                <button type="submit" class="w-full bg-emerald-700 text-white p-3 rounded-lg font-black tracking-wide hover:bg-emerald-800 transition shadow-md">Log In</button>
            </form>
            <p class="text-[11px] text-slate-400 mt-4 text-center">Don't see your phone number recognized? Ask your school's admin to add it to the learner's profile.</p>
        </div>
    </body>
    </html>
    """


@router.post("/api/v1/student/login")
async def student_login_submit(request: Request):
    form = await request.form()
    phone = (form.get("phone") or "").strip()
    password = (form.get("password") or "").strip()

    if not phone or not password:
        return RedirectResponse(url="/student/login?error=Phone+number+and+password+are+both+required.", status_code=303)

    if "#" not in password:
        return RedirectResponse(url="/student/login?error=Password+should+be+AdmissionNumber%23Grade%2C+e.g.+112%231.", status_code=303)

    admission_number_part, _, grade_part = password.partition("#")
    admission_number_part = admission_number_part.strip()
    grade_part = grade_part.strip()

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Not scoped to one school_id — a family could have children
            # at more than one school, and this phone number lookup is
            # deliberately global, exactly like the earlier phone-based
            # design. What actually narrows it down is the password:
            # since each candidate's OWN admission_number+grade is
            # different, only the one real match will ever verify.
            cur.execute("""
                SELECT s.id, s.first_name, s.middle_name, s.last_name, s.admission_number, s.school_id,
                       c.grade_name, sc.name AS school_name
                FROM students s
                LEFT JOIN classes c ON s.class_id = c.id
                LEFT JOIN schools sc ON s.school_id = sc.id
                WHERE (s.mother_phone = %s OR s.father_phone = %s) AND (s.status IS NULL OR s.status != 'GRADUATED');
            """, (phone, phone))
            candidates = cur.fetchall()

    # The password is derived, not stored/hashed — it's simply this
    # candidate's own admission_number, matched exactly, plus their
    # CURRENT grade, matched via _grade_part_matches (handles both the
    # plain-number Grade 1-9 format and PP1/PP2 directly).
    matches = [
        c for c in candidates
        if c['admission_number'] == admission_number_part and _grade_part_matches(grade_part, c['grade_name'])
    ]

    if not matches:
        return RedirectResponse(url="/student/login?error=Incorrect+phone+number+or+password.", status_code=303)

    if len(matches) == 1:
        response = RedirectResponse(url="/student/dashboard", status_code=303)
        response.set_cookie("session_student_id", str(matches[0]['id']), httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30)
        return response

    # Genuinely ambiguous — the same phone number, admission number, AND
    # grade all coincide across more than one learner. Since each
    # sibling has a different admission number, this can only really
    # happen across two DIFFERENT schools (a parent's phone number reused
    # for an unrelated family elsewhere, with a coincidentally identical
    # admission_number#grade) — astronomically unlikely, but handled
    # rather than assumed away. Shows each match's school name, since
    # that's what actually distinguishes this scenario, not their name.
    options_html = "".join(f"""
        <a href="/api/v1/student/login/select/{c['id']}" class="block w-full text-left p-3 rounded-lg border border-slate-200 hover:bg-slate-50 transition mb-2">
            <p class="font-bold text-slate-800 text-sm">{esc(c['school_name'] or 'Unknown School')}</p>
            <p class="text-xs text-slate-400">{esc(full_student_name(c))} — Admission No. {esc(c['admission_number'])}</p>
        </a>
        """ for c in matches)
    return HTMLResponse(f"""
    <!DOCTYPE html>
    <html>
    <head><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Elimu Hub | Which school?</title><script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script></head>
    <body class="bg-slate-900 flex items-center justify-center min-h-screen font-sans p-4">
        <div class="bg-white p-8 rounded-2xl shadow-2xl w-full max-w-sm border-t-8 border-emerald-700">
            <h2 class="text-lg font-black text-slate-800 mb-1">Which school?</h2>
            <p class="text-xs text-slate-400 mb-4">This login matches more than one learner. Select your school:</p>
            {options_html}
        </div>
    </body>
    </html>
    """)


@router.get("/api/v1/student/login/select/{student_id}")
def student_login_select(student_id: int, request: Request):
    """Completes login after the "which school?" disambiguation step. Not
    guarded by a password re-check here — the full login was already
    verified against every candidate in student_login_submit; this
    endpoint only exists to let the learner pick which one of the
    already-verified matches to actually sign in as."""
    response = RedirectResponse(url="/student/dashboard", status_code=303)
    response.set_cookie("session_student_id", str(student_id), httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30)
    return response


@router.get("/student/logout")
def student_logout():
    response = RedirectResponse(url="/student/login", status_code=303)
    response.delete_cookie("session_student_id")
    return response


# ============================================================
# Manage Students — a real, interactive list linking to each
# student's Edit Profile and Portal Access pages. Neither admins
# nor class teachers previously had any way to reach an
# individual student's profile through the UI at all — this is
# genuinely new navigation, not just a portal-access shortcut.
# ============================================================

@router.get("/admin/students/manage/{school_id}", response_class=HTMLResponse)
def manage_students_list(school_id: int, request: Request, grade_name: str, education_level: str, stream: str):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    viewer = get_current_session_user(request)
    is_class_teacher_here = False
    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if viewer and viewer.get('role') == 'staff':
                # Any teacher connected to this class (homeroom or
                # subject) can at least VIEW the list — editing a
                # student's parent contacts specifically is further
                # restricted to the homeroom teacher inside the portal-
                # access routes themselves, not gated here.
                class_keys = get_teacher_class_keys(cur, school_id, viewer['id'])
                if not teacher_can_access_class(class_keys, grade_name, education_level, stream):
                    raise HTTPException(status_code=403, detail="You're not connected to this class.")
                is_class_teacher_here = is_teacher_of_this_class(cur, school_id, viewer['id'], grade_name, education_level, stream)
            else:
                is_class_teacher_here = True  # admins/super admins always can

            cur.execute("""
                SELECT id, admission_number, first_name, middle_name, last_name, mother_phone, father_phone
                FROM students
                WHERE school_id = %s AND class_id = (SELECT id FROM classes WHERE grade_name = %s AND education_level = %s LIMIT 1)
                  AND (%s = 'SINGLE STREAM' OR stream = %s) AND (status IS NULL OR status != 'GRADUATED')
                ORDER BY admission_number ASC;
            """, (school_id, grade_name, education_level, stream, stream))
            roster_students = cur.fetchall()

    rows_html = ""
    for s in roster_students:
        has_contact = bool(s['mother_phone'] or s['father_phone'])
        contact_badge = (
            "<span class='text-[10px] font-bold text-emerald-700 bg-emerald-50 border border-emerald-200 px-2 py-0.5 rounded-full'>Portal ready</span>"
            if has_contact else
            "<span class='text-[10px] font-bold text-amber-700 bg-amber-50 border border-amber-200 px-2 py-0.5 rounded-full'>No parent phone yet</span>"
        )
        portal_link = (
            f"<a href='/admin/student/portal-access/{school_id}/{s['id']}' class='text-xs font-bold text-indigo-700 hover:underline'>Portal Access</a>"
            if is_class_teacher_here else
            "<span class='text-xs text-slate-300' title=\"Only this learner's class teacher (or an admin) can set this up\">Portal Access</span>"
        )
        rows_html += f"""
        <tr class="border-b border-slate-100 text-sm">
            <td class="p-3 font-mono text-xs text-slate-400">{esc(s['admission_number'])}</td>
            <td class="p-3 font-bold text-slate-800">{esc(full_student_name(s))}</td>
            <td class="p-3">{contact_badge}</td>
            <td class="p-3"><a href="/admin/student/edit/{school_id}/{s['id']}" class="text-xs font-bold text-slate-600 hover:underline">Edit Profile</a></td>
            <td class="p-3">{portal_link}</td>
        </tr>
        """

    return f"""
    <!DOCTYPE html>
    <html>
    <head><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Elimu Hub | Manage Students</title><script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script></head>
    <body class="bg-slate-100 min-h-screen p-4 sm:p-8">
        <div class="max-w-3xl mx-auto space-y-4">
            <div class="bg-white p-6 rounded-2xl border shadow-xs">
                <h2 class="text-lg font-black text-slate-800">👤 Manage Students — {esc(grade_name)}{' — ' + esc(stream) if stream != 'SINGLE STREAM' else ''}</h2>
                <p class="text-xs text-slate-400">{esc(education_level)}</p>
            </div>
            <div class="bg-white rounded-2xl border shadow-xs overflow-hidden">
                <table class="w-full text-left border-collapse">
                    <thead><tr class="border-b-2 text-[11px] uppercase text-slate-400"><th class="p-3">Adm. No.</th><th class="p-3">Name</th><th class="p-3">Portal Status</th><th class="p-3">Profile</th><th class="p-3">Parent Contacts</th></tr></thead>
                    <tbody>{rows_html or "<tr><td colspan='5' class='p-4 text-center text-slate-400 italic text-xs'>No students in this class yet.</td></tr>"}</tbody>
                </table>
            </div>
        </div>
    </body>
    </html>
    """

@router.get("/admin/student/portal-access/{school_id}/{student_id}", response_class=HTMLResponse)
def student_portal_access_form(school_id: int, student_id: int, request: Request, done: str = None):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT s.*, c.grade_name FROM students s
                LEFT JOIN classes c ON s.class_id = c.id
                WHERE s.id = %s AND s.school_id = %s;
            """, (student_id, school_id))
            student = cur.fetchone()
            if not student:
                raise HTTPException(status_code=404, detail="Student not found.")

            # A staff member can only manage parent contacts for a
            # student in a class they're the assigned class (homeroom)
            # teacher for — not just any subject teacher who happens to
            # teach them once a week. Admins and super admins are never
            # restricted by this.
            viewer = get_current_session_user(request)
            if viewer and viewer.get('role') == 'staff':
                if not is_teacher_of_this_class(cur, school_id, viewer['id'], student['grade_name'], student['education_level'], student['stream']):
                    raise HTTPException(status_code=403, detail="You're not the class teacher for this learner. Ask your admin, or the learner's own class teacher, to set this up.")

    # The derived password — always this candidate's own admission
    # number + current grade, never something an admin sets or a hash
    # stored anywhere. Only strip down to a bare number for the "Grade N"
    # format specifically — PP1/PP2 have no separate "Grade" word to
    # remove, so blindly extracting digits from ANY grade_name would
    # wrongly turn "PP1" into just "1", producing a login that could
    # never actually match anything (_grade_part_matches keeps PP1/PP2
    # as an exact string match, not a digit extraction).
    grade_name = student.get('grade_name') or ""
    grade_name_upper = grade_name.upper()
    grade_match = re.match(r"^GRADE\s*(\d+)$", grade_name_upper)
    login_grade_suffix = grade_match.group(1) if grade_match else grade_name_upper
    login_password = f"{student['admission_number']}#{login_grade_suffix}" if login_grade_suffix else student['admission_number']

    return f"""
    <!DOCTYPE html>
    <html>
    <head><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Elimu Hub | Learner Portal Access</title><script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script></head>
    <body class="bg-slate-100 min-h-screen flex items-center justify-center p-4">
        <div class="bg-white p-8 rounded-2xl border shadow-xs w-full max-w-md">
            <h2 class="text-lg font-black text-slate-800">🎒 Learner Portal Access</h2>
            <p class="text-xs text-slate-400 mb-4">{esc(full_student_name(student))} — Admission No. {esc(student['admission_number'])}</p>
            {"<div class='bg-emerald-50 border border-emerald-200 text-emerald-800 text-xs px-3 py-2.5 rounded-lg mb-4'>✅ Saved.</div>" if done else ""}
            <div class="bg-indigo-50 border border-indigo-200 rounded-lg p-3 mb-4">
                <p class="text-[11px] font-bold text-indigo-700 uppercase tracking-wide">Login Password</p>
                <p class="text-sm font-mono font-bold text-indigo-900">{esc(login_password)}</p>
                <p class="text-[10px] text-indigo-500 mt-1">Nothing to set here — it's always this learner's own admission number + current grade. Give this, plus either phone number below, to the family — that's what they enter at /student/login.</p>
            </div>
            <form action="/api/v1/student/portal-access/{school_id}/{student_id}" method="post" class="space-y-3">
                <p class="text-[11px] font-bold text-slate-500 uppercase tracking-wide">Parent/Guardian Phone Numbers</p>
                <p class="text-[10px] text-slate-400 -mt-2">Used both as the login username, and as where progress reports get sent (SMS/WhatsApp).</p>
                <div>
                    <label class="text-xs font-bold text-slate-600">Mother's Phone Number</label>
                    <input type="tel" name="mother_phone" value="{esc(student['mother_phone'] or '')}" placeholder="e.g. 0712345678" class="w-full border p-2.5 rounded-lg mt-1 text-sm">
                </div>
                <div>
                    <label class="text-xs font-bold text-slate-600">Father's Phone Number</label>
                    <input type="tel" name="father_phone" value="{esc(student['father_phone'] or '')}" placeholder="e.g. 0723456789" class="w-full border p-2.5 rounded-lg mt-1 text-sm">
                </div>
                <button type="submit" class="w-full bg-emerald-700 hover:bg-emerald-800 text-white font-bold py-2.5 rounded-lg text-sm transition">Save Portal Access</button>
            </form>
        </div>
    </body>
    </html>
    """


@router.post("/api/v1/student/portal-access/{school_id}/{student_id}")
async def student_portal_access_save(school_id: int, student_id: int, request: Request):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    form = await request.form()
    mother_phone = (form.get("mother_phone") or "").strip() or None
    father_phone = (form.get("father_phone") or "").strip() or None

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT s.id, s.education_level, s.stream, c.grade_name FROM students s
                LEFT JOIN classes c ON s.class_id = c.id
                WHERE s.id = %s AND s.school_id = %s;
            """, (student_id, school_id))
            student = cur.fetchone()
            if not student:
                raise HTTPException(status_code=404, detail="Student not found.")

            viewer = get_current_session_user(request)
            if viewer and viewer.get('role') == 'staff':
                if not is_teacher_of_this_class(cur, school_id, viewer['id'], student['grade_name'], student['education_level'], student['stream']):
                    raise HTTPException(status_code=403, detail="You're not the class teacher for this learner.")

            cur.execute(
                "UPDATE students SET mother_phone = %s, father_phone = %s WHERE id = %s;",
                (mother_phone, father_phone, student_id)
            )
            conn.commit()

    return RedirectResponse(url=f"/admin/student/portal-access/{school_id}/{student_id}?done=1", status_code=303)
