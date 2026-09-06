"""
senior_school_routes.py — everything specific to CBC Senior School
(Grade 10-12), kept in its own dedicated file so it's reviewable on its
own rather than buried inside main.py or the general timetable module.

Kept separate for the same reason timetable_routes.py / finance_routes.py
/ schemes_routes.py / student_portal_routes.py / notifications_routes.py
are their own files: a distinct router, included into the main app, so
main.py itself doesn't grow with every new feature.

The core idea: unlike Grade 1-9, where every student in a grade studies
the same fixed subject list, Senior School students choose a specific
subject combination (e.g. "STEM - Medicine Track") and different
combinations within the SAME grade study different subjects entirely.
Modeled by repurposing `stream` as the combination name — exactly what
stream already exists for (subdividing a grade into differently-taught
groups) — so combination_subjects just says which subjects belong to a
given (school, grade, combination), reusing the existing class-teacher,
teacher-assignment, and timetable-slot machinery rather than building a
parallel system.

This is Phase 1: the data model and the admin UI to define combinations.
NOT yet done: wiring this into Teaching Assignments and generation so a
Senior School stream's subject list actually comes from its combination
(currently, those still show every subject tagged with the education
level, same as any other level). Marks entry and report cards for
Senior School are a separate, later phase entirely.
"""

import urllib.parse
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from psycopg2.extras import RealDictCursor

from shared import (
    esc, get_db_connection,
    require_school_session, require_admin_session,
)

router = APIRouter()

# Every Senior School combination shares these 4 exactly the same way —
# no school-to-school variation, so there's nothing for an admin to
# choose here. These get added to every combination automatically,
# always marked compulsory, without the admin re-ticking them each time.
AUTO_COMPULSORY_SUBJECTS = ["English", "Kiswahili", "Community Service Learning", "Physical Education"]

# Mathematics is compulsory too, but WHICH variant applies genuinely
# differs by combination (a STEM track might use Core, another might use
# Essential) — so this one still needs a choice, just a single required
# one instead of a generic checkbox, and it's always compulsory once
# picked.
MATHEMATICS_VARIANTS = ["Mathematics (Core)", "Mathematics (Essential)", "Advanced Mathematics"]


def bootstrap_senior_school_schema():
    """Creates/upgrades everything this module owns: the classes
    (Grade 10-12), the Senior School subject pool, and the
    combination_subjects table. Purely additive — safe to run against a
    fresh install or one with years of live school data already in it."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            # Grade 10-12 classes. Safe, unused ids (12-14) rather than
            # renumbering anything — every existing school already has
            # real students tied to ids 1-11 (Grade 1-9 plus PP1/PP2),
            # and those must never move.
            classes_payload = [
                (12, 'Grade 10', 'Senior School'),
                (13, 'Grade 11', 'Senior School'),
                (14, 'Grade 12', 'Senior School'),
            ]
            for class_id, grade, level in classes_payload:
                cur.execute("INSERT INTO classes (id, grade_name, education_level) VALUES (%s, %s, %s) ON CONFLICT (id) DO NOTHING;", (class_id, grade, level))

            # The full pool a school can pick from when defining its own
            # subject combinations below — compulsory subjects every
            # student takes regardless of combination, plus the broad
            # elective pool spanning STEM, Social Sciences, and Arts &
            # Sports Science. Not every school offers every one of
            # these — this is the full menu, not a fixed list.
            subjects_payload = [
                'English', 'Kenyan Sign Language', 'Kiswahili',
                'Mathematics (Core)', 'Mathematics (Essential)', 'Advanced Mathematics',
                'Community Service Learning', 'Physical Education',
                'Biology', 'Chemistry', 'Physics', 'General Science', 'Computer Studies',
                'Agriculture', 'Building and Construction', 'Drawing and Design',
                'Business Studies', 'Geography', 'History and Citizenship',
                'Literature in English', 'Fasihi ya Kiswahili',
                'Christian Religious Education', 'Islamic Religious Education', 'Hindu Religious Education',
                'Music', 'Theatre and Film', 'Fine Art', 'Art and Design', 'Sports and Recreation Science',
                'French', 'German', 'Arabic', 'Mandarin',
            ]
            for name in subjects_payload:
                cur.execute("INSERT INTO learning_areas (education_level, name) VALUES ('Senior School', %s) ON CONFLICT (education_level, name) DO NOTHING;", (name,))
            conn.commit()

            # --- Subject Combinations table ---
            # is_compulsory distinguishes the 4-5 subjects every
            # combination shares (English, Kiswahili, Mathematics, CSL,
            # PE) from the combination-specific electives, for display
            # purposes.
            cur.execute("""
                CREATE TABLE IF NOT EXISTS combination_subjects (
                    id SERIAL PRIMARY KEY,
                    school_id INTEGER REFERENCES schools(id) ON DELETE CASCADE,
                    grade_name VARCHAR(100) NOT NULL,
                    stream VARCHAR(150) NOT NULL,
                    learning_area_id INTEGER REFERENCES learning_areas(id) ON DELETE CASCADE,
                    is_compulsory BOOLEAN NOT NULL DEFAULT FALSE,
                    UNIQUE (school_id, grade_name, stream, learning_area_id)
                );
                CREATE INDEX IF NOT EXISTS idx_combination_subjects_lookup ON combination_subjects (school_id, grade_name, stream);
            """)
            conn.commit()


@router.get("/timetable/combinations/{school_id}", response_class=HTMLResponse)
def subject_combinations_view(school_id: int, request: Request, grade_name: str = "Grade 10"):
    """Lets a school define its own Subject Combinations for Senior
    School — each with a name (used as that combination's `stream`
    value) and a specific set of ELECTIVE subjects, since real schools
    each offer their own limited mix from the full Senior School
    pathway pool, not a fixed set of 3 pathways. The compulsory core
    (English, Kiswahili, Community Service Learning, Physical Education,
    plus a chosen Mathematics variant) is handled automatically — an
    admin only ever picks electives here, never re-ticks the same
    compulsory subjects for every combination."""
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT id, name, school_type FROM schools WHERE id = %s;", (school_id,))
            school = cur.fetchone()
            if not school:
                raise HTTPException(status_code=404, detail="School not found.")
            if school['school_type'] != 'senior_school':
                raise HTTPException(status_code=403, detail="Subject Combinations are only available for Senior School institutions. Change this school's type in its registration profile if this is incorrect.")

            # Electives only — the auto-compulsory subjects and the
            # Mathematics variants are handled separately below, never
            # shown as pickable checkboxes here.
            excluded_names = AUTO_COMPULSORY_SUBJECTS + MATHEMATICS_VARIANTS
            cur.execute(
                "SELECT id, name FROM learning_areas WHERE education_level = 'Senior School' AND name != ALL(%s) ORDER BY name ASC;",
                (excluded_names,)
            )
            elective_pool = cur.fetchall()

            cur.execute("""
                SELECT stream, la.id AS learning_area_id, la.name AS subject_name, cs.is_compulsory
                FROM combination_subjects cs JOIN learning_areas la ON cs.learning_area_id = la.id
                WHERE cs.school_id = %s AND cs.grade_name = %s
                ORDER BY stream ASC, la.name ASC;
            """, (school_id, grade_name))
            rows = cur.fetchall()

    combinations = {}
    for r in rows:
        combinations.setdefault(r['stream'], []).append(r)

    grade_tabs = "".join(
        f"""<a href="/timetable/combinations/{school_id}?grade_name={urllib.parse.quote(g)}"
               class="px-3 py-1.5 rounded-lg text-xs font-bold transition {'bg-indigo-700 text-white' if g == grade_name else 'bg-white border border-slate-200 text-slate-600 hover:bg-slate-50'}">{g}</a>"""
        for g in ["Grade 10", "Grade 11", "Grade 12"]
    )

    existing_html = ""
    for combo_name, subjects in combinations.items():
        subject_chips = "".join(
            f"<span class='text-[11px] font-semibold px-2 py-1 rounded-full {'bg-indigo-100 text-indigo-700' if s['is_compulsory'] else 'bg-slate-100 text-slate-600'} mr-1 mb-1 inline-block'>{esc(s['subject_name'])}{' *' if s['is_compulsory'] else ''}</span>"
            for s in subjects
        )
        existing_html += f"""
        <div class="bg-white p-4 rounded-2xl border shadow-xs mb-3">
            <div class="flex justify-between items-start">
                <h3 class="text-sm font-black text-slate-800">{esc(combo_name)}</h3>
                <form action="/api/v1/timetable/combinations/delete/{school_id}" method="post" onsubmit="return confirm('Delete the combination \\'{esc(combo_name)}\\' for {esc(grade_name)}? This does NOT affect students already assigned this stream — reassign them first if needed.');">
                    <input type="hidden" name="grade_name" value="{esc(grade_name)}">
                    <input type="hidden" name="stream" value="{esc(combo_name)}">
                    <button type="submit" class="text-[11px] font-bold text-rose-600 hover:underline">Delete</button>
                </form>
            </div>
            <div class="mt-2">{subject_chips}</div>
        </div>
        """

    auto_compulsory_chips = "".join(
        f"<span class='text-[11px] font-semibold px-2 py-1 rounded-full bg-indigo-100 text-indigo-700 mr-1 mb-1 inline-block'>{esc(name)}</span>"
        for name in AUTO_COMPULSORY_SUBJECTS
    )
    math_variant_radios = "".join(
        f"""<label class="flex items-center gap-2 text-xs p-1.5 hover:bg-slate-50 rounded-lg cursor-pointer">
            <input type="radio" name="math_variant" value="{esc(v)}" {"required" if i == 0 else ""} class="rounded">
            <span>{esc(v)}</span>
        </label>"""
        for i, v in enumerate(MATHEMATICS_VARIANTS)
    )
    elective_checkboxes = "".join(
        f"""<label class="flex items-center gap-2 text-xs p-1.5 hover:bg-slate-50 rounded-lg cursor-pointer">
            <input type="checkbox" name="elective_ids" value="{s['id']}" class="rounded">
            <span>{esc(s['name'])}</span>
        </label>"""
        for s in elective_pool
    )

    return f"""
    <!DOCTYPE html>
    <html>
    <head><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Elimu Hub | Subject Combinations</title><script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script></head>
    <body class="bg-slate-100 min-h-screen p-4 sm:p-8">
        <div class="max-w-3xl mx-auto space-y-4">
            <div class="bg-white p-6 rounded-2xl border shadow-xs">
                <h2 class="text-lg font-black text-slate-800">🎓 Subject Combinations</h2>
                <p class="text-xs text-slate-400 mb-3">{esc(school['name'])} — pick the electives your school offers; the compulsory core is added automatically. Each combination becomes a schedulable stream, e.g. "{esc(grade_name)}" + "STEM - Medicine Track".</p>
                <div class="flex gap-2">{grade_tabs}</div>
            </div>

            <div>
                <h3 class="text-xs font-bold uppercase tracking-wider text-slate-400 mb-2">Existing combinations for {esc(grade_name)}</h3>
                {existing_html or "<p class='text-xs text-slate-400 italic px-1'>None defined yet — add your first one below.</p>"}
            </div>

            <div class="bg-white p-6 rounded-2xl border shadow-xs">
                <h3 class="text-sm font-black text-slate-800 mb-3">+ Add a new combination</h3>
                <form action="/api/v1/timetable/combinations/{school_id}" method="post" class="space-y-3">
                    <input type="hidden" name="grade_name" value="{esc(grade_name)}">
                    <div>
                        <label class="text-xs font-bold text-slate-600">Combination Name</label>
                        <input type="text" name="stream" placeholder="e.g. STEM - Medicine Track" class="w-full border p-2.5 rounded-lg mt-1 text-sm" required>
                    </div>
                    <div>
                        <label class="text-xs font-bold text-slate-600">Compulsory (every combination automatically includes these)</label>
                        <div class="mt-1">{auto_compulsory_chips}</div>
                    </div>
                    <div>
                        <label class="text-xs font-bold text-slate-600">Mathematics — choose the variant this combination uses</label>
                        <div class="mt-1 border rounded-lg p-2">{math_variant_radios}</div>
                    </div>
                    <div>
                        <label class="text-xs font-bold text-slate-600">Electives — tick the ones this combination offers</label>
                        <div class="mt-1 border rounded-lg p-2 max-h-72 overflow-y-auto">{elective_checkboxes}</div>
                    </div>
                    <button type="submit" class="w-full bg-indigo-700 hover:bg-indigo-800 text-white font-bold py-2.5 rounded-lg text-sm transition">Save Combination</button>
                </form>
            </div>
        </div>
    </body>
    </html>
    """


@router.post("/api/v1/timetable/combinations/{school_id}")
async def save_subject_combination(school_id: int, request: Request):
    auth_error = require_admin_session(request, school_id)
    if auth_error:
        return auth_error

    form = await request.form()
    grade_name = form.get("grade_name", "").strip()
    stream = form.get("stream", "").strip()
    math_variant = form.get("math_variant", "").strip()
    elective_ids = form.getlist("elective_ids")

    if not grade_name or not stream:
        raise HTTPException(status_code=400, detail="Grade and combination name are both required.")
    if not math_variant or math_variant not in MATHEMATICS_VARIANTS:
        raise HTTPException(status_code=400, detail="Choose which Mathematics variant this combination uses.")

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT school_type FROM schools WHERE id = %s;", (school_id,))
            school = cur.fetchone()
            if not school or school['school_type'] != 'senior_school':
                raise HTTPException(status_code=403, detail="Subject Combinations are only available for Senior School institutions.")

            # Resolve the auto-compulsory subject names (fixed, same for
            # every combination) plus the chosen Mathematics variant into
            # their learning_area ids — these are never shown as
            # checkboxes, so there's nothing for the admin to submit for
            # them; they're always compulsory=True whenever a
            # combination is saved.
            compulsory_names = AUTO_COMPULSORY_SUBJECTS + [math_variant]
            cur.execute(
                "SELECT id FROM learning_areas WHERE education_level = 'Senior School' AND name = ANY(%s);",
                (compulsory_names,)
            )
            compulsory_ids = [r['id'] for r in cur.fetchall()]

            for lid in compulsory_ids:
                cur.execute("""
                    INSERT INTO combination_subjects (school_id, grade_name, stream, learning_area_id, is_compulsory)
                    VALUES (%s, %s, %s, %s, TRUE)
                    ON CONFLICT (school_id, grade_name, stream, learning_area_id)
                    DO UPDATE SET is_compulsory = TRUE;
                """, (school_id, grade_name, stream, lid))

            for eid in elective_ids:
                cur.execute("""
                    INSERT INTO combination_subjects (school_id, grade_name, stream, learning_area_id, is_compulsory)
                    VALUES (%s, %s, %s, %s, FALSE)
                    ON CONFLICT (school_id, grade_name, stream, learning_area_id)
                    DO UPDATE SET is_compulsory = FALSE;
                """, (school_id, grade_name, stream, int(eid)))
            conn.commit()

    return RedirectResponse(url=f"/timetable/combinations/{school_id}?grade_name={urllib.parse.quote(grade_name)}", status_code=303)


@router.post("/api/v1/timetable/combinations/delete/{school_id}")
async def delete_subject_combination(school_id: int, request: Request):
    auth_error = require_admin_session(request, school_id)
    if auth_error:
        return auth_error

    form = await request.form()
    grade_name = form.get("grade_name", "").strip()
    stream = form.get("stream", "").strip()

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT school_type FROM schools WHERE id = %s;", (school_id,))
            school = cur.fetchone()
            if not school or school['school_type'] != 'senior_school':
                raise HTTPException(status_code=403, detail="Subject Combinations are only available for Senior School institutions.")

            cur.execute("DELETE FROM combination_subjects WHERE school_id = %s AND grade_name = %s AND stream = %s;", (school_id, grade_name, stream))
            conn.commit()

    return RedirectResponse(url=f"/timetable/combinations/{school_id}?grade_name={urllib.parse.quote(grade_name)}", status_code=303)
