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
    full_student_name,
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
                    education_level VARCHAR(100) NOT NULL DEFAULT 'ALL',
                    period_order INTEGER NOT NULL,
                    label VARCHAR(50) NOT NULL,
                    short_label VARCHAR(20),
                    start_time VARCHAR(20),
                    end_time VARCHAR(20),
                    is_teaching_period BOOLEAN DEFAULT TRUE,
                    period_type VARCHAR(20) NOT NULL DEFAULT 'teaching',
                    UNIQUE(school_id, education_level, period_order)
                );
            """)
            cur.execute("ALTER TABLE timetable_periods ADD COLUMN IF NOT EXISTS short_label VARCHAR(20);")
            # Adds a third period type — 'prep' — alongside the existing
            # teaching/break split. is_teaching_period stays the single
            # source of truth for "can a lesson ever go here?": prep periods
            # keep it FALSE, so they're structurally excluded from the
            # generator's candidate list the same way breaks already are —
            # not a soft rule that could be overridden, but simply never in
            # the pool of fillable periods at all.
            cur.execute("ALTER TABLE timetable_periods ADD COLUMN IF NOT EXISTS period_type VARCHAR(20) NOT NULL DEFAULT 'teaching';")
            cur.execute("UPDATE timetable_periods SET period_type = 'break' WHERE is_teaching_period = FALSE AND period_type = 'teaching';")
            # Schools with periods already configured before per-level bell
            # schedules existed get 'ALL' — a single shared schedule used as
            # a fallback for any level that hasn't been given its own yet.
            cur.execute("ALTER TABLE timetable_periods ADD COLUMN IF NOT EXISTS education_level VARCHAR(100) NOT NULL DEFAULT 'ALL';")

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
                    lessons_per_week INTEGER NOT NULL DEFAULT 1,
                    requires_double BOOLEAN NOT NULL DEFAULT FALSE,
                    UNIQUE(school_id, learning_area_id, grade_name, education_level, stream)
                );
            """)
            cur.execute("ALTER TABLE teacher_subject_assignments ADD COLUMN IF NOT EXISTS lessons_per_week INTEGER NOT NULL DEFAULT 1;")
            cur.execute("ALTER TABLE teacher_subject_assignments ADD COLUMN IF NOT EXISTS requires_double BOOLEAN NOT NULL DEFAULT FALSE;")
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

            # Timetable-only subjects — school-defined items (e.g. "Library",
            # "Study Skills", "Guidance & Counselling") that can be scheduled
            # into the grid alongside real graded subjects, without ever
            # touching learning_areas or the report-card/grading system.
            cur.execute("""
                CREATE TABLE IF NOT EXISTS timetable_custom_subjects (
                    id SERIAL PRIMARY KEY,
                    school_id INTEGER REFERENCES schools(id) ON DELETE CASCADE,
                    education_level VARCHAR(100) NOT NULL,
                    name VARCHAR(150) NOT NULL,
                    UNIQUE(school_id, education_level, name)
                );
            """)

            # A slot can hold exactly one of: a graded subject, a custom
            # (non-graded) subject, or a co-curricular activity. Additive,
            # nullable columns — existing behavior around learning_area_id
            # is completely untouched.
            cur.execute("ALTER TABLE timetable_slots ADD COLUMN IF NOT EXISTS custom_subject_id INTEGER REFERENCES timetable_custom_subjects(id) ON DELETE SET NULL;")

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
            _ensure_unique_constraint(
                cur, "timetable_periods",
                ["school_id", "education_level", "period_order"],
                "uq_timetable_periods_level_order",
            )

            # Co-curricular activities (clubs, societies, sports, etc.) —
            # separate from timed lesson slots, since most of these run
            # outside the regular teaching day.
            cur.execute("""
                CREATE TABLE IF NOT EXISTS co_curricular_activities (
                    id SERIAL PRIMARY KEY,
                    school_id INTEGER REFERENCES schools(id) ON DELETE CASCADE,
                    name VARCHAR(150) NOT NULL,
                    category VARCHAR(50) NOT NULL DEFAULT 'Club',
                    schedule_note VARCHAR(200),
                    staff_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    description TEXT
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS co_curricular_participants (
                    id SERIAL PRIMARY KEY,
                    activity_id INTEGER REFERENCES co_curricular_activities(id) ON DELETE CASCADE,
                    student_id INTEGER REFERENCES students(id) ON DELETE CASCADE,
                    UNIQUE(activity_id, student_id)
                );
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_co_curricular_participants_activity ON co_curricular_participants (activity_id);")

            # Now that co_curricular_activities exists, a timetable slot can
            # also hold a co-curricular activity instead of an academic or
            # custom subject.
            cur.execute("ALTER TABLE timetable_slots ADD COLUMN IF NOT EXISTS co_curricular_activity_id INTEGER REFERENCES co_curricular_activities(id) ON DELETE SET NULL;")

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

# The three CBC education levels, matching main.py's classes/learning_areas
# data exactly. Each can have its own independent bell schedule — e.g.
# Lower Primary's shorter 35-minute lessons vs Junior School's 40-minute ones.
EDUCATION_LEVELS = ["Lower Primary", "Upper Primary", "Junior School"]

# A curated, print-friendly palette (soft background + readable dark text)
# for color-coding subjects on printed timetables. Assignment is by a stable
# hash of the subject name, not Python's built-in hash() (which is
# randomized per-process and would give different colors on every restart).
SUBJECT_COLOR_PALETTE = [
    ("#FEF3C7", "#92400E"),  # amber
    ("#D1FAE5", "#065F46"),  # emerald
    ("#FCE7F3", "#9D174D"),  # pink
    ("#DBEAFE", "#1E40AF"),  # blue
    ("#FFEDD5", "#9A3412"),  # orange
    ("#EDE9FE", "#5B21B6"),  # violet
    ("#CCFBF1", "#115E59"),  # teal
    ("#FEE2E2", "#991B1B"),  # red
    ("#E0E7FF", "#3730A3"),  # indigo
    ("#ECFCCB", "#3F6212"),  # lime
    ("#CFFAFE", "#155E75"),  # cyan
    ("#FFE4E6", "#9F1239"),  # rose
]

def get_subject_color(name: str):
    """Returns (background_hex, text_hex) for a subject/activity name,
    consistent across every report and every server restart."""
    if not name:
        return ("#F1F5F9", "#475569")
    stable_index = sum(ord(c) for c in name.strip().lower()) % len(SUBJECT_COLOR_PALETTE)
    return SUBJECT_COLOR_PALETTE[stable_index]


def get_school_days(cur, school_id: int):
    """Returns this school's configured list of teaching days (e.g. Mon-Fri
    or Mon-Sat), defaulting to a 5-day week only until the school sets its
    own value on the Periods & Days page. Works whether the caller's cursor
    is a RealDictCursor (dict-style rows) or a plain cursor (tuple rows) —
    some call sites use a plain cursor for other queries in the same block."""
    cur.execute("SELECT days_per_week FROM timetable_settings WHERE school_id = %s;", (school_id,))
    row = cur.fetchone()
    if row is None:
        days_per_week = 5
    else:
        try:
            days_per_week = row['days_per_week']
        except (TypeError, KeyError):
            days_per_week = row[0]
    return ALL_POSSIBLE_DAYS[:days_per_week]


def get_periods_for_level(cur, school_id: int, education_level: str):
    """Returns this school's periods for a specific education level (e.g.
    Lower Primary's 35-minute lessons vs Junior School's 40-minute ones).
    Falls back to the shared 'ALL' schedule if that level hasn't been given
    its own periods yet, so schools that don't need the distinction — or
    haven't configured it yet — keep working exactly as before."""
    cur.execute(
        "SELECT * FROM timetable_periods WHERE school_id = %s AND education_level = %s ORDER BY period_order ASC;",
        (school_id, education_level)
    )
    rows = cur.fetchall()
    if rows:
        return rows
    cur.execute(
        "SELECT * FROM timetable_periods WHERE school_id = %s AND education_level = 'ALL' ORDER BY period_order ASC;",
        (school_id,)
    )
    return cur.fetchall()


def validate_timetable_setup(cur, school_id: int, grade_name: str, education_level: str, stream: str):
    """Runs every check the generator itself implicitly relies on, but
    surfaces problems as clear, specific messages *before* generating
    anything — rather than silently producing a timetable with gaps, or
    failing deep inside the generator with no indication of why.

    Returns (errors, warnings) — errors are blocking (generation should not
    proceed), warnings are informational (generation can proceed, but the
    result may have gaps or a tight schedule)."""
    errors, warnings = [], []

    days = get_school_days(cur, school_id)
    all_periods = get_periods_for_level(cur, school_id, education_level)
    teaching_periods = [p for p in all_periods if p['is_teaching_period']]

    if not all_periods:
        errors.append(f"No periods are configured for {education_level} at all. Go to Periods & Days and set up the bell schedule first.")
        return errors, warnings  # nothing else can be meaningfully checked without periods
    if not teaching_periods:
        errors.append(f"{education_level} has periods configured, but none of them are marked as teaching periods (they're all breaks/prep/co-curricular). At least one real teaching period is needed.")
        return errors, warnings

    total_available_slots = len(days) * len(teaching_periods)

    cur.execute("SELECT id, name FROM learning_areas WHERE education_level = %s;", (education_level,))
    subjects = sort_subjects_for_display(cur.fetchall(), education_level)
    if not subjects:
        errors.append(f"No subjects exist for {education_level}. This shouldn't normally happen — contact support if you see this.")
        return errors, warnings

    cur.execute("""
        SELECT learning_area_id, staff_user_id, lessons_per_week, requires_double FROM teacher_subject_assignments
        WHERE school_id = %s AND grade_name = %s AND education_level = %s AND stream = %s;
    """, (school_id, grade_name, education_level, stream))
    assignments = {r['learning_area_id']: r for r in cur.fetchall()}

    total_required_slots = 0
    sorted_teaching = sorted(teaching_periods, key=lambda p: p['period_order'])
    has_consecutive_pair = any(
        sorted_teaching[i + 1]['period_order'] - sorted_teaching[i]['period_order'] == 1
        for i in range(len(sorted_teaching) - 1)
    )

    for sub in subjects:
        a = assignments.get(sub['id'])
        if not a or not a['staff_user_id']:
            warnings.append(f"'{sub['name']}' has no teacher assigned for this class — it will be left blank in the generated timetable. Assign one under Teaching Assignments if you want it scheduled.")
            continue
        lessons = a['lessons_per_week'] or 0
        if lessons <= 0:
            warnings.append(f"'{sub['name']}' has a teacher assigned but 0 lessons per week set — it won't be scheduled. Set a lessons-per-week value under Teaching Assignments.")
            continue
        total_required_slots += lessons
        if a['requires_double'] and not has_consecutive_pair:
            errors.append(f"'{sub['name']}' is set to require a double lesson, but {education_level} has no two consecutive teaching periods anywhere in the day — a double lesson literally cannot be placed. Either add consecutive periods or turn off 'requires double' for this subject.")

    if total_required_slots > total_available_slots:
        errors.append(
            f"The subjects for this class need {total_required_slots} lesson-slots per week in total, but only {total_available_slots} teaching periods actually exist "
            f"({len(days)} days × {len(teaching_periods)} periods). Reduce some subjects' lessons-per-week, or add more teaching periods, before generating."
        )

    # Cross-class teacher capacity — a teacher already booked elsewhere in
    # the school for most of the week may not have enough free slots left
    # to cover everything this class needs from them too.
    cur.execute("""
        SELECT staff_user_id, COUNT(*) AS booked_elsewhere
        FROM timetable_slots
        WHERE school_id = %s AND staff_user_id IS NOT NULL
          AND NOT (grade_name = %s AND education_level = %s AND stream = %s)
        GROUP BY staff_user_id;
    """, (school_id, grade_name, education_level, stream))
    booked_elsewhere = {r['staff_user_id']: r['booked_elsewhere'] for r in cur.fetchall()}

    teacher_ids_needed = {r['staff_user_id']: r for r in assignments.values() if r['staff_user_id']}
    if teacher_ids_needed:
        cur.execute("SELECT id, full_name, email FROM users WHERE id = ANY(%s);", (list(teacher_ids_needed.keys()),))
        teacher_names = {r['id']: (r['full_name'] or r['email']) for r in cur.fetchall()}
        for teacher_id, a in teacher_ids_needed.items():
            already_booked = booked_elsewhere.get(teacher_id, 0)
            needed = a['lessons_per_week'] or 0
            if already_booked + needed > total_available_slots:
                warnings.append(
                    f"{teacher_names.get(teacher_id, 'A teacher')} is already booked in {already_booked} slot(s) in other classes this week, "
                    f"and this class needs {needed} more from them — that's more than the {total_available_slots} periods available in the week. "
                    f"Some lessons for them here may not find a free slot."
                )

    return errors, warnings


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

    sections_by_level = {}
    for sec in sections:
        sections_by_level.setdefault(sec['education_level'], []).append(sec)

    level_accent = {"Lower Primary": "#0d9488", "Upper Primary": "#0891b2", "Junior School": "#7c3aed"}
    level_groups_html = ""
    for level_name, level_sections in sections_by_level.items():
        accent = level_accent.get(level_name, "#0d9488")
        cards_html = ""
        for sec in level_sections:
            encoded_grade = urllib.parse.quote(sec['grade_name'])
            encoded_level = urllib.parse.quote(sec['education_level'])
            encoded_stream = urllib.parse.quote(sec['stream'])
            has_timetable = slot_counts.get((sec['grade_name'], sec['education_level'], sec['stream']), 0) > 0
            status_badge = (
                "<span class='text-[10px] font-bold px-2 py-0.5 rounded-full bg-teal-50 text-teal-700 border border-teal-200'>Timetable set</span>"
                if has_timetable else
                "<span class='text-[10px] font-bold px-2 py-0.5 rounded-full bg-amber-50 text-amber-700 border border-amber-200'>Not yet created</span>"
            )
            cards_html += f"""
            <div class='bg-white border border-slate-200/80 p-5 rounded-2xl shadow-xs hover:shadow-md transition-shadow flex flex-col justify-between gap-3 border-l-4' style='border-left-color:{accent};'>
                <div>
                    <span class='text-[10px] px-2.5 py-1 rounded-md font-bold uppercase tracking-wider' style='background:{accent}1a;color:{accent};'>{esc(sec['education_level'])}</span>
                    <h3 class='text-base font-black text-slate-800 mt-2.5'>{esc(_section_label(sec['grade_name'], sec['stream']))}</h3>
                    <div class="mt-2">{status_badge}</div>
                </div>
                <div class='grid grid-cols-3 gap-2'>
                    <a href='/timetable/assignments/{school_id}?grade_name={encoded_grade}&education_level={encoded_level}&stream={encoded_stream}' class='bg-slate-100 hover:bg-slate-200 text-slate-700 text-center text-xs py-2 rounded-xl font-semibold transition'>Assign Teachers</a>
                    <a href='/timetable/constraints/{school_id}?grade_name={encoded_grade}&education_level={encoded_level}&stream={encoded_stream}' class='bg-slate-100 hover:bg-slate-200 text-slate-700 text-center text-xs py-2 rounded-xl font-semibold transition'>Constraints</a>
                    <a href='/timetable/grade/{school_id}?grade_name={encoded_grade}&education_level={encoded_level}&stream={encoded_stream}' class='bg-teal-700 hover:bg-teal-800 text-white text-center text-xs py-2 rounded-xl font-semibold transition'>Open Timetable</a>
                </div>
            </div>
            """
        level_groups_html += f"""
        <div class="mb-6">
            <div class="flex items-center justify-between mb-3">
                <h2 class="text-sm font-black text-slate-700">{esc(level_name)}</h2>
                <form action="/api/v1/timetable/test-and-generate-level/{school_id}" method="post" onsubmit="return confirm('Test and generate every class in {esc(level_name)}? This replaces existing entries for every class in this level that passes validation.');">
                    <input type="hidden" name="education_level" value="{esc(level_name)}">
                    <button type="submit" class="bg-amber-500 hover:bg-amber-600 text-white px-4 py-2 rounded-xl text-xs font-bold transition shadow-sm">🧪 Test &amp; Generate Whole Level</button>
                </form>
            </div>
            <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">{cards_html}</div>
        </div>
        """

    return HTMLResponse(f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Elimu Hub | Timetabling — {esc(school['name'])}</title>
        <script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script>
        <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap" rel="stylesheet">
        <style>body {{ font-family: 'Plus Jakarta Sans', sans-serif; }}</style>
    </head>
    <body class="bg-[#F7F9F8] min-h-screen">
        <header class="bg-white border-b border-slate-200/80 px-6 sm:px-8 py-4 flex flex-col sm:flex-row sm:justify-between sm:items-center gap-2">
            <div>
                <h1 class="text-base font-bold text-slate-900">📅 Timetabling — {esc(school['name'])}</h1>
                <p class="text-xs text-slate-400">Each stream has its own independent timetable.</p>
            </div>
            <div class="flex items-center gap-2 flex-wrap">
                <a href="/timetable/periods/{school_id}" class="bg-white hover:bg-slate-50 text-slate-600 border border-slate-200 px-4 py-2 rounded-xl text-xs font-bold text-center transition">⏱ Periods & Days</a>
                <a href="/timetable/availability/{school_id}" class="bg-white hover:bg-slate-50 text-slate-600 border border-slate-200 px-4 py-2 rounded-xl text-xs font-bold text-center transition">👩‍🏫 Teacher Availability</a>
                <a href="/timetable/subject-availability/{school_id}" class="bg-white hover:bg-slate-50 text-slate-600 border border-slate-200 px-4 py-2 rounded-xl text-xs font-bold text-center transition">📚 Subject Time-Off</a>
                <a href="/timetable/sync-rules/{school_id}" class="bg-white hover:bg-slate-50 text-slate-600 border border-slate-200 px-4 py-2 rounded-xl text-xs font-bold text-center transition">🔗 Same-Time Rules</a>
                <a href="/timetable/teachers/{school_id}" class="bg-white hover:bg-slate-50 text-slate-600 border border-slate-200 px-4 py-2 rounded-xl text-xs font-bold text-center transition">🖨 Teacher Timetables</a>
                <a href="/timetable/collision-check/{school_id}" class="bg-rose-600 hover:bg-rose-700 text-white px-4 py-2 rounded-xl text-xs font-bold text-center transition shadow-sm">🔍 Check for Collisions</a>
                <a href="/timetable/teacher-workload/{school_id}" class="bg-white hover:bg-slate-50 text-slate-600 border border-slate-200 px-4 py-2 rounded-xl text-xs font-bold text-center transition">📊 Teacher Workload</a>
                <a href="/timetable/co-curricular/{school_id}" class="bg-white hover:bg-slate-50 text-slate-600 border border-slate-200 px-4 py-2 rounded-xl text-xs font-bold text-center transition">🎭 Co-Curricular</a>
                <a href="/timetable/custom-subjects/{school_id}" class="bg-white hover:bg-slate-50 text-slate-600 border border-slate-200 px-4 py-2 rounded-xl text-xs font-bold text-center transition">➕ Custom Subjects</a>
                <a href="/timetable/master/{school_id}" class="bg-amber-500 hover:bg-amber-600 text-white px-4 py-2 rounded-xl text-xs font-bold text-center transition shadow-sm">🗓 Whole School View</a>
                <a href="{get_dashboard_url(request, school_id)}" class="bg-slate-800 hover:bg-slate-900 text-white px-4 py-2 rounded-xl text-xs font-bold text-center transition">← Back to Dashboard</a>
            </div>
        </header>
        <div class="p-6 sm:p-8 max-w-6xl mx-auto">
            {"<div class='bg-amber-50 border border-amber-200 text-amber-800 text-sm px-4 py-3 rounded-xl mb-6'>⏱ <b>Set up your periods and bell times first</b> — go to <a href='/timetable/periods/" + str(school_id) + "' class='underline font-bold'>Periods &amp; Days</a> before generating any timetable.</div>" if not has_periods else ""}
            {level_groups_html or "<p class='text-slate-400 text-xs italic col-span-full text-center py-8 bg-white border border-dashed rounded-2xl'>No classes with students yet — add students first.</p>"}
        </div>
    </body>
    </html>
    """)


@router.get("/timetable/periods/{school_id}", response_class=HTMLResponse)
def timetable_periods_view(school_id: int, request: Request, education_level: str = "Lower Primary"):
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

            cur.execute(
                "SELECT * FROM timetable_periods WHERE school_id = %s AND education_level = %s ORDER BY period_order ASC;",
                (school_id, education_level)
            )
            periods = cur.fetchall()

    days_options = "".join(
        f"<option value='{n}' {'selected' if n == days_per_week else ''}>{n} days ({', '.join(ALL_POSSIBLE_DAYS[:n])})</option>"
        for n in range(1, 7)
    )

    level_tabs = "".join(
        f"""<a href="/timetable/periods/{school_id}?education_level={urllib.parse.quote(lvl)}"
               class="px-4 py-2 rounded-xl text-xs font-bold transition {'bg-indigo-700 text-white' if lvl == education_level else 'bg-white border border-slate-200 text-slate-600 hover:bg-slate-50'}">{lvl}</a>"""
        for lvl in EDUCATION_LEVELS
    )

    period_rows = ""
    type_badges = {
        "teaching": ("Teaching", "bg-emerald-50 text-emerald-700 border-emerald-200"),
        "break": ("Break", "bg-slate-100 text-slate-500 border-slate-200"),
        "prep": ("Prep Time", "bg-violet-50 text-violet-700 border-violet-200"),
        "co_curricular": ("Co-Curricular", "bg-amber-50 text-amber-700 border-amber-200"),
    }
    row_bg = {"teaching": "", "break": "background:#f8fafc;", "prep": "background:#f5f3ff;", "co_curricular": "background:#fffbeb;"}
    for p in periods:
        p_type = p.get('period_type') or ('teaching' if p['is_teaching_period'] else 'break')
        row_type, badge_class = type_badges.get(p_type, type_badges['teaching'])
        row_style = row_bg.get(p_type, "")
        period_rows += f"""
        <tr style="{row_style}" class="border-b text-sm">
            <td class="p-2.5 text-center text-slate-400 font-mono text-xs">{p['period_order']}</td>
            <td class="p-2.5 font-bold text-slate-800">{esc(p['label'])}</td>
            <td class="p-2.5 text-slate-500">{esc(p['short_label'] or '')}</td>
            <td class="p-2.5 text-slate-500">{esc(p['start_time'] or '')}</td>
            <td class="p-2.5 text-slate-500">{esc(p['end_time'] or '')}</td>
            <td class="p-2.5 text-center">
                <span class="text-[10px] font-bold px-2 py-0.5 rounded-full border {badge_class}">{row_type}</span>
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
        <title>Elimu Hub | Periods & Days — {esc(school['name'])}</title>
        <script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script>
    </head>
    <body class="bg-[#F8FAFC] min-h-screen">
        <header class="bg-white border-b border-slate-200/80 px-6 sm:px-8 py-4 flex flex-col sm:flex-row sm:justify-between sm:items-center gap-2">
            <div>
                <h1 class="text-base font-bold text-slate-900">⏱ Periods & Days — {esc(school['name'])}</h1>
                <p class="text-xs text-slate-400">Every school sets its own bell times — there's no shared default, since boarding and day schools (and different levels within the same school) start and run lessons at different times.</p>
            </div>
            <a href="/timetable/dashboard/{school_id}" class="bg-slate-200 hover:bg-slate-300 text-slate-700 px-4 py-2 rounded-xl text-xs font-bold text-center transition">← Back</a>
        </header>

        <div class="p-4 sm:p-8 max-w-4xl mx-auto space-y-6">
            <div class="bg-white p-5 sm:p-6 rounded-2xl border shadow-xs">
                <h2 class="text-sm font-bold text-slate-800 mb-3">Number of Teaching Days</h2>
                <p class="text-xs text-slate-400 mb-3">This applies school-wide, across every level.</p>
                <form action="/api/v1/timetable/periods/days/{school_id}" method="post" class="flex flex-col sm:flex-row gap-3">
                    <select name="days_per_week" class="flex-1 border p-2.5 rounded-xl text-sm font-medium">{days_options}</select>
                    <button type="submit" class="bg-indigo-700 hover:bg-indigo-800 text-white font-bold px-5 py-2.5 rounded-xl text-sm transition">Save</button>
                </form>
            </div>

            <div>
                <h2 class="text-sm font-bold text-slate-800 mb-2">Bell Times for:</h2>
                <div class="flex gap-2 flex-wrap">{level_tabs}</div>
                <p class="text-xs text-slate-400 mt-2">Each level below has its own independent set of periods — set Lower Primary's shorter lessons separately from Junior School's longer ones, for example.</p>
            </div>

            <div class="bg-white rounded-2xl border shadow-xs overflow-hidden">
                <div class="p-5 sm:p-6 border-b bg-slate-50/50">
                    <h2 class="text-sm font-bold text-slate-800">{esc(education_level)} — Periods & Bell Times</h2>
                    <p class="text-xs text-slate-400 mt-0.5">Add every period and break in order, with its actual start/end time for this level.</p>
                </div>
                <div class="overflow-x-auto">
                    <table class="w-full text-left">
                        <thead>
                            <tr class="bg-slate-50 text-slate-500 text-xs font-semibold border-b">
                                <th class="p-2.5">#</th><th class="p-2.5">Name</th><th class="p-2.5">Short</th>
                                <th class="p-2.5">Start</th><th class="p-2.5">End</th><th class="p-2.5 text-center">Type</th><th class="p-2.5"></th>
                            </tr>
                        </thead>
                        <tbody>{period_rows or "<tr><td colspan='7' class='text-center p-6 text-slate-400 italic text-xs'>No periods configured yet for this level — add the first one below.</td></tr>"}</tbody>
                    </table>
                </div>
                <form action="/api/v1/timetable/periods/add/{school_id}" method="post" class="p-5 sm:p-6 bg-slate-50/50 border-t grid grid-cols-1 sm:grid-cols-6 gap-3">
                    <input type="hidden" name="education_level" value="{esc(education_level)}">
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
                    <div>
                        <label class="text-[11px] font-bold text-slate-500">Type</label>
                        <select name="period_type" class="w-full border p-2 rounded-lg mt-1 text-sm bg-white">
                            <option value="teaching">Teaching</option>
                            <option value="break">Break</option>
                            <option value="prep">Prep Time (protected)</option>
                            <option value="co_curricular">Co-Curricular (reserved)</option>
                        </select>
                    </div>
                    <div class="flex flex-col justify-end">
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
    education_level: str = Form(...),
    label: str = Form(...),
    short_label: str = Form(""),
    start_time: str = Form(...),
    end_time: str = Form(...),
    period_type: str = Form("teaching"),
):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    label = label.strip()
    short_label = short_label.strip() or label[:3].upper()
    if not label:
        raise HTTPException(status_code=400, detail="A name for this period is required.")
    if period_type not in ("teaching", "break", "prep", "co_curricular"):
        period_type = "teaching"
    is_teaching_period = period_type == "teaching"

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(MAX(period_order), 0) + 1 AS next_order FROM timetable_periods WHERE school_id = %s AND education_level = %s;",
                (school_id, education_level)
            )
            next_order = cur.fetchone()[0]
            cur.execute("""
                INSERT INTO timetable_periods (school_id, education_level, period_order, label, short_label, start_time, end_time, is_teaching_period, period_type)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s);
            """, (school_id, education_level, next_order, label, short_label, start_time, end_time, is_teaching_period, period_type))
            conn.commit()

    return RedirectResponse(url=f"/timetable/periods/{school_id}?education_level={urllib.parse.quote(education_level)}", status_code=303)


@router.post("/api/v1/timetable/periods/delete/{school_id}/{period_id}")
def delete_timetable_period(school_id: int, period_id: int, request: Request):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT education_level FROM timetable_periods WHERE id = %s AND school_id = %s;", (period_id, school_id))
            row = cur.fetchone()
            level = row['education_level'] if row else "Lower Primary"
            # Cascades to timetable_slots and teacher_availability rows using this period.
            cur.execute("DELETE FROM timetable_periods WHERE id = %s AND school_id = %s;", (period_id, school_id))
            conn.commit()

    return RedirectResponse(url=f"/timetable/periods/{school_id}?education_level={urllib.parse.quote(level)}", status_code=303)


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
                SELECT learning_area_id, staff_user_id, lessons_per_week, requires_double FROM teacher_subject_assignments
                WHERE school_id = %s AND grade_name = %s AND education_level = %s AND stream = %s;
            """, (school_id, grade_name, education_level, stream))
            current_assignments = {r['learning_area_id']: r for r in cur.fetchall()}

    rows_html = ""
    for sub in subjects:
        existing = current_assignments.get(sub['id'], {})
        assigned_id = existing.get('staff_user_id')
        lessons_per_week = existing.get('lessons_per_week', 1)
        requires_double = existing.get('requires_double', False)
        options = "<option value=''>— Unassigned —</option>" + "".join(
            f"<option value='{m['id']}' {'selected' if m['id'] == assigned_id else ''}>{esc(m['full_name'] or m['email'])}</option>"
            for m in staff_members
        )
        rows_html += f"""
        <div class="flex flex-col sm:flex-row sm:items-center gap-2 sm:gap-3 py-2.5 border-b border-slate-50 last:border-0">
            <span class="text-sm font-semibold text-slate-700 sm:w-40 shrink-0">{esc(sub['name'])}</span>
            <select name="teacher_{sub['id']}" class="border p-2 rounded-lg text-xs font-semibold bg-white flex-1 min-w-0">{options}</select>
            <div class="flex items-center gap-2 shrink-0">
                <label class="text-[10px] font-bold text-slate-500">Lessons/wk</label>
                <input type="number" name="lessons_{sub['id']}" value="{lessons_per_week}" min="0" max="20" class="border p-1.5 rounded-lg text-xs w-14 text-center">
                <label class="text-[10px] font-bold text-slate-500 flex items-center gap-1">
                    <input type="checkbox" name="double_{sub['id']}" value="1" {'checked' if requires_double else ''} class="w-3.5 h-3.5"> Needs double
                </label>
            </div>
        </div>
        """

    section_label = _section_label(grade_name, stream)
    return f"""
    <!DOCTYPE html>
    <html>
    <head><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Elimu Hub | Assign Teachers</title><script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script></head>
    <body class="bg-slate-100 min-h-screen p-4 sm:p-8">
        <div class="max-w-xl mx-auto bg-white p-6 rounded-2xl border shadow-xs">
            <h2 class="text-lg font-black text-slate-800">Assign Teachers</h2>
            <p class="text-xs text-slate-400 mb-4">{esc(section_label)} ({esc(education_level)}) — who teaches each subject, how many lessons per week, and whether it needs a double lesson (e.g. for practicals).</p>
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

                try:
                    lessons_per_week = max(0, min(20, int(form.get(f"lessons_{learning_area_id}", 1) or 1)))
                except ValueError:
                    lessons_per_week = 1
                requires_double = form.get(f"double_{learning_area_id}") is not None

                cur.execute("""
                    INSERT INTO teacher_subject_assignments (school_id, staff_user_id, learning_area_id, grade_name, education_level, stream, lessons_per_week, requires_double)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (school_id, learning_area_id, grade_name, education_level, stream)
                    DO UPDATE SET staff_user_id = EXCLUDED.staff_user_id, lessons_per_week = EXCLUDED.lessons_per_week, requires_double = EXCLUDED.requires_double;
                """, (school_id, staff_user_id, learning_area_id, grade_name, education_level, stream, lessons_per_week, requires_double))
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
    <head><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Elimu Hub | Teacher Availability</title><script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script></head>
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
    <head><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Elimu Hub | Teacher Timetables</title><script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script></head>
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


@router.get("/timetable/teacher-workload/{school_id}", response_class=HTMLResponse)
def teacher_workload_report(school_id: int, request: Request):
    """For every teacher, shows their total assigned lessons/week — broken
    down by exactly which class+subject each commitment comes from — versus
    how many teaching periods actually exist per week for each education
    level they teach in. A teacher whose total exceeds what's physically
    possible in the week is flagged clearly, since that's a real staffing
    fact the software can't invent more hours to solve — only a human
    decision (reduce lessons/week for a subject, or reassign a class to a
    different teacher) actually fixes it. Purely read-only."""
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
                SELECT tsa.staff_user_id, u.full_name, u.email,
                       tsa.grade_name, tsa.education_level, tsa.stream, tsa.lessons_per_week,
                       la.name AS subject_name
                FROM teacher_subject_assignments tsa
                JOIN users u ON tsa.staff_user_id = u.id
                JOIN learning_areas la ON tsa.learning_area_id = la.id
                WHERE tsa.school_id = %s AND tsa.staff_user_id IS NOT NULL
                ORDER BY u.full_name NULLS LAST, u.email;
            """, (school_id,))
            assignment_rows = cur.fetchall()

            days = get_school_days(cur, school_id)
            capacity_by_level = {}
            for level in EDUCATION_LEVELS:
                teaching_periods = [p for p in get_periods_for_level(cur, school_id, level) if p['is_teaching_period']]
                capacity_by_level[level] = len(teaching_periods) * len(days)

    teachers = {}
    for r in assignment_rows:
        tid = r['staff_user_id']
        teachers.setdefault(tid, {'name': r['full_name'] or r['email'], 'assignments': [], 'by_level': {}})
        teachers[tid]['assignments'].append(r)
        teachers[tid]['by_level'].setdefault(r['education_level'], 0)
        teachers[tid]['by_level'][r['education_level']] += r['lessons_per_week'] or 0

    teacher_cards_html = ""
    for tid, info in sorted(teachers.items(), key=lambda kv: kv[1]['name'] or ''):
        is_overloaded = any(info['by_level'].get(lvl, 0) > capacity_by_level.get(lvl, 0) for lvl in info['by_level'])

        level_summary_html = "".join(
            f"""<span class="text-xs font-bold px-2 py-1 rounded-lg {'bg-rose-50 text-rose-700 border border-rose-200' if load > capacity_by_level.get(lvl, 0) else 'bg-slate-50 text-slate-600 border border-slate-200'}">
                {esc(lvl)}: {load}/{capacity_by_level.get(lvl, 0)} periods
            </span>"""
            for lvl, load in info['by_level'].items()
        )

        assignment_rows_html = "".join(f"""
            <tr class="border-b border-slate-50 last:border-0">
                <td class="p-2 text-xs">{esc(_section_label(a['grade_name'], a['stream']))} <span class="text-slate-400">({esc(a['education_level'])})</span></td>
                <td class="p-2 text-xs font-semibold">{esc(a['subject_name'])}</td>
                <td class="p-2 text-xs text-center font-bold">{a['lessons_per_week'] or 0}/wk</td>
                <td class="p-2 text-right">
                    <a href="/timetable/assignments/{school_id}?grade_name={urllib.parse.quote(a['grade_name'])}&education_level={urllib.parse.quote(a['education_level'])}&stream={urllib.parse.quote(a['stream'])}" class="text-indigo-700 hover:underline text-xs font-bold">Adjust →</a>
                </td>
            </tr>
        """ for a in info['assignments'])

        teacher_cards_html += f"""
        <div class="bg-white rounded-2xl border {'border-rose-300' if is_overloaded else 'border-slate-200/80'} shadow-xs p-5 mb-4">
            <div class="flex items-center justify-between flex-wrap gap-2 mb-3">
                <h3 class="text-sm font-bold text-slate-800">{'⚠️ ' if is_overloaded else ''}{esc(info['name'])}</h3>
                <div class="flex gap-2 flex-wrap">{level_summary_html}</div>
            </div>
            <table class="w-full">
                <thead><tr class="text-[10px] uppercase text-slate-400 border-b"><th class="p-2 text-left">Class</th><th class="p-2 text-left">Subject</th><th class="p-2 text-center">Load</th><th class="p-2"></th></tr></thead>
                <tbody>{assignment_rows_html}</tbody>
            </table>
        </div>
        """

    overloaded_count = sum(1 for info in teachers.values() if any(info['by_level'].get(lvl, 0) > capacity_by_level.get(lvl, 0) for lvl in info['by_level']))

    return f"""
    <!DOCTYPE html>
    <html>
    <head><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Elimu Hub | Teacher Workload</title><script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script></head>
    <body class="bg-[#F7F9F8] min-h-screen">
        <header class="bg-white border-b px-6 sm:px-8 py-4 flex flex-col sm:flex-row sm:justify-between sm:items-center gap-2">
            <div>
                <h1 class="text-base font-bold text-slate-900">📊 Teacher Workload — {esc(school['name'])}</h1>
                <p class="text-xs text-slate-400">Total lessons/week assigned per teacher, versus periods actually available — {overloaded_count} teacher(s) currently over capacity.</p>
            </div>
            <a href="/timetable/dashboard/{school_id}" class="bg-slate-800 hover:bg-slate-900 text-white px-4 py-2 rounded-xl text-xs font-bold text-center transition">← Back to Timetables</a>
        </header>
        <div class="p-4 sm:p-8 max-w-4xl mx-auto">
            {"<div class='bg-amber-50 border border-amber-200 text-amber-800 text-sm px-4 py-3 rounded-xl mb-4'>⚠️ A teacher over capacity means they've been assigned more lessons across all their classes than there are periods in the week to teach them — that's a real staffing fact, not something generation can work around. Reduce lessons/week for one of their subjects below, or reassign one of their classes to a different teacher.</div>" if overloaded_count else ""}
            {teacher_cards_html or "<p class='text-slate-400 text-xs italic text-center py-8'>No teacher assignments configured yet.</p>"}
        </div>
    </body>
    </html>
    """


@router.get("/timetable/collision-check/{school_id}", response_class=HTMLResponse)
def _find_timetable_collisions(cur, school_id: int, education_level: str = None):
    """Returns a list of collision groups — each group is a list of slot
    rows for the same teacher, same day/period, across 2+ different
    classes. Pure read, no side effects. Shared by the collision-check
    display page and the whole-level Test & Generate flow."""
    query = """
        SELECT ts.day_of_week, ts.period_id, tp.label AS period_label, tp.start_time, tp.end_time,
               ts.staff_user_id, u.full_name, u.email,
               ts.grade_name, ts.stream, ts.education_level,
               COALESCE(la.name, cs.name, ca.name) AS subject_name
        FROM timetable_slots ts
        JOIN timetable_periods tp ON ts.period_id = tp.id
        LEFT JOIN users u ON ts.staff_user_id = u.id
        LEFT JOIN learning_areas la ON ts.learning_area_id = la.id
        LEFT JOIN timetable_custom_subjects cs ON ts.custom_subject_id = cs.id
        LEFT JOIN co_curricular_activities ca ON ts.co_curricular_activity_id = ca.id
        WHERE ts.school_id = %s AND ts.staff_user_id IS NOT NULL
    """
    params = [school_id]
    if education_level:
        query += " AND ts.education_level = %s"
        params.append(education_level)
    cur.execute(query, tuple(params))
    all_slots = cur.fetchall()

    groups = {}
    for slot in all_slots:
        key = (slot['day_of_week'], slot['period_id'], slot['staff_user_id'])
        groups.setdefault(key, []).append(slot)

    return [slots for slots in groups.values() if len({(s['grade_name'], s['stream']) for s in slots}) > 1]


def timetable_collision_check(school_id: int, request: Request, education_level: str = None):
    """Scans every slot currently in the timetable (optionally scoped to one
    education level) for a teacher booked into two different classes at the
    exact same day/period — a real double-booking, not a hypothetical one.
    Purely read-only; only reports what it finds, changes nothing. This is
    the direct equivalent of ASC Timetables' clash/conflict report."""
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT name FROM schools WHERE id = %s;", (school_id,))
            school = cur.fetchone()
            if not school:
                raise HTTPException(status_code=404, detail="School not found.")

            collisions = _find_timetable_collisions(cur, school_id, education_level)

    # Sort collisions for a stable, readable report: by day, then period, then teacher.
    day_order = {d: i for i, d in enumerate(["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"])}
    collisions.sort(key=lambda slots: (day_order.get(slots[0]['day_of_week'], 99), slots[0]['period_id'], slots[0]['full_name'] or ''))

    collision_rows_html = ""
    for slots in collisions:
        teacher_name = slots[0]['full_name'] or slots[0]['email'] or 'Unknown teacher'
        period_label = slots[0]['period_label']
        time_range = f"{slots[0]['start_time']}–{slots[0]['end_time']}"
        classes_html = "".join(
            f"<li><b>{esc(_section_label(s['grade_name'], s['stream']))}</b> ({esc(s['education_level'])}) — {esc(s['subject_name'] or 'Unknown subject')}"
            f" <a href='/timetable/grade/{school_id}?grade_name={urllib.parse.quote(s['grade_name'])}&education_level={urllib.parse.quote(s['education_level'])}&stream={urllib.parse.quote(s['stream'])}' class='text-indigo-700 hover:underline text-xs font-bold ml-2'>Fix →</a></li>"
            for s in slots
        )
        collision_rows_html += f"""
        <div class="bg-white rounded-2xl border border-rose-200 shadow-xs p-5 mb-4">
            <div class="flex items-center justify-between mb-2">
                <h3 class="text-sm font-bold text-rose-700">⚠️ {esc(teacher_name)} — double-booked</h3>
                <span class="text-xs text-slate-400">{esc(slots[0]['day_of_week'])}, {esc(period_label)} ({esc(time_range)})</span>
            </div>
            <ul class="text-sm text-slate-700 space-y-1 list-disc list-inside">{classes_html}</ul>
        </div>
        """

    level_tabs = "".join(
        f"""<a href="/timetable/collision-check/{school_id}?education_level={urllib.parse.quote(lvl)}"
               class="px-4 py-2 rounded-xl text-xs font-bold transition {'bg-rose-700 text-white' if lvl == education_level else 'bg-white border border-slate-200 text-slate-600 hover:bg-slate-50'}">{lvl}</a>"""
        for lvl in EDUCATION_LEVELS
    )

    return f"""
    <!DOCTYPE html>
    <html>
    <head><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Elimu Hub | Timetable Collision Check</title><script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script></head>
    <body class="bg-[#F7F9F8] min-h-screen">
        <header class="bg-white border-b px-6 sm:px-8 py-4 flex flex-col sm:flex-row sm:justify-between sm:items-center gap-2">
            <div>
                <h1 class="text-base font-bold text-slate-900">🔍 Timetable Collision Check — {esc(school['name'])}</h1>
                <p class="text-xs text-slate-400">Scans every currently scheduled slot for a teacher double-booked across two different classes at the same time.</p>
            </div>
            <a href="/timetable/dashboard/{school_id}" class="bg-slate-800 hover:bg-slate-900 text-white px-4 py-2 rounded-xl text-xs font-bold text-center transition">← Back to Timetables</a>
        </header>
        <div class="p-4 sm:p-8 max-w-3xl mx-auto space-y-4">
            <div class="flex gap-2 flex-wrap">
                <a href="/timetable/collision-check/{school_id}" class="px-4 py-2 rounded-xl text-xs font-bold transition {'bg-rose-700 text-white' if not education_level else 'bg-white border border-slate-200 text-slate-600 hover:bg-slate-50'}">All Levels</a>
                {level_tabs}
            </div>

            {f"<div class='bg-emerald-50 border border-emerald-200 text-emerald-800 text-sm px-4 py-3 rounded-xl font-semibold'>✅ No collisions found{' for ' + esc(education_level) if education_level else ' across the whole school'} — every teacher is scheduled in exactly one place at a time.</div>" if not collisions else f"<div class='bg-rose-50 border border-rose-200 text-rose-800 text-sm px-4 py-3 rounded-xl font-semibold'>⚠️ Found {len(collisions)} collision(s){' in ' + esc(education_level) if education_level else ''} — see below.</div>"}

            {f'''<form action="/api/v1/timetable/test-and-generate-level/{school_id}" method="post" onsubmit="return confirm('Regenerate every class in {esc(education_level)}? Many of these collisions are likely leftover from timetables generated before whole-level generation existed — regenerating everything together, in order, lets the conflict-avoidance logic see every other class as it goes.');">
                <input type="hidden" name="education_level" value="{esc(education_level)}">
                <button type="submit" class="bg-amber-500 hover:bg-amber-600 text-white px-4 py-2 rounded-xl text-xs font-bold transition shadow-sm">🔧 Regenerate {esc(education_level)} to Try Fixing This</button>
            </form>''' if collisions and education_level else ""}

            {collision_rows_html}
        </div>
    </body>
    </html>
    """


@router.get("/timetable/custom-subjects/{school_id}", response_class=HTMLResponse)
def custom_subjects_view(school_id: int, request: Request, education_level: str = "Lower Primary"):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT name FROM schools WHERE id = %s;", (school_id,))
            school = cur.fetchone()
            if not school:
                raise HTTPException(status_code=404, detail="School not found.")

            cur.execute(
                "SELECT * FROM timetable_custom_subjects WHERE school_id = %s AND education_level = %s ORDER BY name ASC;",
                (school_id, education_level)
            )
            custom_subjects = cur.fetchall()

    level_tabs = "".join(
        f"""<a href="/timetable/custom-subjects/{school_id}?education_level={urllib.parse.quote(lvl)}"
               class="px-4 py-2 rounded-xl text-xs font-bold transition {'bg-teal-700 text-white' if lvl == education_level else 'bg-white border border-slate-200 text-slate-600 hover:bg-slate-50'}">{lvl}</a>"""
        for lvl in EDUCATION_LEVELS
    )

    rows_html = "".join(f"""
        <div class="flex items-center justify-between py-2.5 border-b border-slate-50 last:border-0">
            <span class="text-sm font-semibold text-slate-700">{esc(s['name'])}</span>
            <form action="/api/v1/timetable/custom-subjects/delete/{school_id}/{s['id']}" method="post" onsubmit="return confirm('Delete \\'{esc(s['name'])}\\'? Any timetable slots using it will be cleared too.');">
                <button type="submit" class="text-rose-600 hover:text-rose-800 text-xs font-bold">Delete</button>
            </form>
        </div>
    """ for s in custom_subjects)

    return f"""
    <!DOCTYPE html>
    <html>
    <head><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Elimu Hub | Custom Subjects — {esc(school['name'])}</title><script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script></head>
    <body class="bg-[#F7F9F8] min-h-screen p-4 sm:p-8">
        <div class="max-w-2xl mx-auto space-y-4">
            <div class="bg-white p-6 rounded-2xl border shadow-xs">
                <h2 class="text-lg font-black text-slate-800">➕ Custom Timetable Subjects</h2>
                <p class="text-xs text-slate-400 mt-1">Add items that can be scheduled into the timetable but aren't graded subjects — e.g. Library, Study Skills, Guidance &amp; Counselling. These never appear in report cards or the marks-entry system; they're for scheduling only.</p>
            </div>

            <div>
                <p class="text-xs font-bold text-slate-500 mb-2">Level:</p>
                <div class="flex gap-2 flex-wrap">{level_tabs}</div>
            </div>

            <div class="bg-white p-6 rounded-2xl border shadow-xs">
                <h3 class="text-sm font-bold text-slate-800 mb-3">{esc(education_level)}</h3>
                <div class="mb-4">{rows_html or "<p class='text-slate-400 text-xs italic p-2'>No custom subjects added yet for this level.</p>"}</div>
                <form action="/api/v1/timetable/custom-subjects/add/{school_id}" method="post" class="flex gap-2">
                    <input type="hidden" name="education_level" value="{esc(education_level)}">
                    <input type="text" name="name" placeholder="e.g. Library, Study Skills" class="flex-1 border p-2.5 rounded-lg text-sm" required>
                    <button type="submit" class="bg-emerald-700 hover:bg-emerald-800 text-white font-bold px-4 py-2.5 rounded-lg text-sm transition">+ Add</button>
                </form>
            </div>

            <a href="/timetable/dashboard/{school_id}" class="bg-slate-200 hover:bg-slate-300 text-slate-700 font-bold py-2.5 px-5 rounded-xl text-sm transition inline-block">← Back</a>
        </div>
    </body>
    </html>
    """


@router.post("/api/v1/timetable/custom-subjects/add/{school_id}")
def add_custom_subject(school_id: int, request: Request, education_level: str = Form(...), name: str = Form(...)):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    name = name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="A name is required.")

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO timetable_custom_subjects (school_id, education_level, name) VALUES (%s, %s, %s)
                ON CONFLICT (school_id, education_level, name) DO NOTHING;
            """, (school_id, education_level, name))
            conn.commit()

    return RedirectResponse(url=f"/timetable/custom-subjects/{school_id}?education_level={urllib.parse.quote(education_level)}", status_code=303)


@router.post("/api/v1/timetable/custom-subjects/delete/{school_id}/{subject_id}")
def delete_custom_subject(school_id: int, subject_id: int, request: Request):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT education_level FROM timetable_custom_subjects WHERE id = %s AND school_id = %s;", (subject_id, school_id))
            row = cur.fetchone()
            level = row['education_level'] if row else "Lower Primary"
            cur.execute("DELETE FROM timetable_custom_subjects WHERE id = %s AND school_id = %s;", (subject_id, school_id))
            conn.commit()

    return RedirectResponse(url=f"/timetable/custom-subjects/{school_id}?education_level={urllib.parse.quote(level)}", status_code=303)


@router.get("/timetable/co-curricular/{school_id}", response_class=HTMLResponse)
def co_curricular_list(school_id: int, request: Request):
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
                SELECT a.*, u.full_name AS supervisor_name, u.email AS supervisor_email,
                       (SELECT COUNT(*) FROM co_curricular_participants p WHERE p.activity_id = a.id) AS participant_count
                FROM co_curricular_activities a
                LEFT JOIN users u ON a.staff_user_id = u.id
                WHERE a.school_id = %s
                ORDER BY a.category ASC, a.name ASC;
            """, (school_id,))
            activities = cur.fetchall()

            cur.execute("SELECT id, email, full_name FROM users WHERE school_id = %s AND role = 'staff' AND is_verified = TRUE ORDER BY full_name NULLS LAST, email ASC;", (school_id,))
            staff_members = cur.fetchall()

    staff_options = "<option value=''>— No supervisor assigned —</option>" + "".join(
        f"<option value='{m['id']}'>{esc(m['full_name'] or m['email'])}</option>" for m in staff_members
    )

    cards_html = ""
    for a in activities:
        supervisor_label = esc(a['supervisor_name'] or a['supervisor_email']) if a['supervisor_email'] else "<span class='text-slate-400 italic'>Unassigned</span>"
        cards_html += f"""
        <div class="bg-white border border-slate-200/80 p-5 rounded-2xl shadow-xs hover:shadow-md transition-all">
            <div class="flex items-start justify-between gap-2">
                <div>
                    <span class="text-[10px] bg-violet-50 text-violet-700 border border-violet-200 px-2.5 py-1 rounded-md font-bold uppercase tracking-wider">{esc(a['category'])}</span>
                    <h3 class="text-base font-black text-slate-800 mt-2">{esc(a['name'])}</h3>
                </div>
                <form action="/api/v1/timetable/co-curricular/delete/{school_id}/{a['id']}" method="post" onsubmit="return confirm('Delete {esc(a['name'])}? This removes all enrolled participants too.');">
                    <button type="submit" class="text-rose-500 hover:text-rose-700 text-xs font-bold">✕</button>
                </form>
            </div>
            <p class="text-xs text-slate-500 mt-1">{esc(a['schedule_note'] or 'No schedule set')}</p>
            <p class="text-xs text-slate-500 mt-1">Supervisor: {supervisor_label}</p>
            {f"<p class='text-xs text-slate-400 mt-2 italic'>{esc(a['description'])}</p>" if a['description'] else ""}
            <div class="flex items-center justify-between mt-4 pt-3 border-t border-slate-100">
                <span class="text-xs font-bold text-slate-600">{a['participant_count']} student{'s' if a['participant_count'] != 1 else ''} enrolled</span>
                <a href="/timetable/co-curricular/{school_id}/{a['id']}/roster" class="bg-indigo-700 hover:bg-indigo-800 text-white text-xs font-bold px-3 py-1.5 rounded-lg transition">Manage Roster →</a>
            </div>
        </div>
        """

    return f"""
    <!DOCTYPE html>
    <html>
    <head><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Elimu Hub | Co-Curricular Activities — {esc(school['name'])}</title><script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script></head>
    <body class="bg-[#F8FAFC] min-h-screen">
        <header class="bg-white border-b border-slate-200/80 px-6 sm:px-8 py-4 flex flex-col sm:flex-row sm:justify-between sm:items-center gap-2">
            <div>
                <h1 class="text-base font-bold text-slate-900">🎭 Co-Curricular Activities — {esc(school['name'])}</h1>
                <p class="text-xs text-slate-400">Clubs, societies, sports, and other extracurricular programs — separate from the academic timetable.</p>
            </div>
            <a href="/timetable/dashboard/{school_id}" class="bg-slate-200 hover:bg-slate-300 text-slate-700 px-4 py-2 rounded-xl text-xs font-bold text-center transition">← Back</a>
        </header>

        <div class="p-4 sm:p-8 max-w-5xl mx-auto space-y-6">
            <div class="bg-white p-5 sm:p-6 rounded-2xl border shadow-xs">
                <h2 class="text-sm font-bold text-slate-800 mb-3">+ Add New Activity</h2>
                <form action="/api/v1/timetable/co-curricular/add/{school_id}" method="post" class="grid grid-cols-1 sm:grid-cols-2 gap-3">
                    <div>
                        <label class="text-[11px] font-bold text-slate-500">Activity Name</label>
                        <input type="text" name="name" placeholder="e.g. Drama Club, Football, Debate Society" class="w-full border p-2.5 rounded-lg mt-1 text-sm" required>
                    </div>
                    <div>
                        <label class="text-[11px] font-bold text-slate-500">Category</label>
                        <select name="category" class="w-full border p-2.5 rounded-lg mt-1 text-sm bg-white">
                            <option value="Club">Club</option>
                            <option value="Society">Society</option>
                            <option value="Sport">Sport</option>
                            <option value="Other">Other</option>
                        </select>
                    </div>
                    <div>
                        <label class="text-[11px] font-bold text-slate-500">Schedule</label>
                        <input type="text" name="schedule_note" placeholder="e.g. Tue & Thu, 4:00–5:00pm" class="w-full border p-2.5 rounded-lg mt-1 text-sm">
                    </div>
                    <div>
                        <label class="text-[11px] font-bold text-slate-500">Supervisor</label>
                        <select name="staff_user_id" class="w-full border p-2.5 rounded-lg mt-1 text-sm bg-white">{staff_options}</select>
                    </div>
                    <div class="sm:col-span-2">
                        <label class="text-[11px] font-bold text-slate-500">Description (optional)</label>
                        <input type="text" name="description" placeholder="Brief note about this activity" class="w-full border p-2.5 rounded-lg mt-1 text-sm">
                    </div>
                    <div class="sm:col-span-2">
                        <button type="submit" class="bg-emerald-700 hover:bg-emerald-800 text-white font-bold py-2.5 px-5 rounded-xl text-sm transition">+ Add Activity</button>
                    </div>
                </form>
            </div>

            <div class="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 gap-4">
                {cards_html or "<p class='text-slate-400 text-xs italic col-span-full text-center py-8 bg-white border border-dashed rounded-2xl'>No co-curricular activities added yet.</p>"}
            </div>
        </div>
    </body>
    </html>
    """


@router.post("/api/v1/timetable/co-curricular/add/{school_id}")
def add_co_curricular_activity(
    school_id: int,
    request: Request,
    name: str = Form(...),
    category: str = Form("Club"),
    schedule_note: str = Form(""),
    staff_user_id: str = Form(""),
    description: str = Form(""),
):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    name = name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="An activity name is required.")

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO co_curricular_activities (school_id, name, category, schedule_note, staff_user_id, description)
                VALUES (%s, %s, %s, %s, %s, %s);
            """, (school_id, name, category, schedule_note.strip() or None, int(staff_user_id) if staff_user_id else None, description.strip() or None))
            conn.commit()

    return RedirectResponse(url=f"/timetable/co-curricular/{school_id}", status_code=303)


@router.post("/api/v1/timetable/co-curricular/delete/{school_id}/{activity_id}")
def delete_co_curricular_activity(school_id: int, activity_id: int, request: Request):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM co_curricular_activities WHERE id = %s AND school_id = %s;", (activity_id, school_id))
            conn.commit()

    return RedirectResponse(url=f"/timetable/co-curricular/{school_id}", status_code=303)


@router.get("/timetable/co-curricular/{school_id}/{activity_id}/roster", response_class=HTMLResponse)
def co_curricular_roster(school_id: int, activity_id: int, request: Request, search: str = ""):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM co_curricular_activities WHERE id = %s AND school_id = %s;", (activity_id, school_id))
            activity = cur.fetchone()
            if not activity:
                raise HTTPException(status_code=404, detail="Activity not found.")

            cur.execute("""
                SELECT s.id, s.first_name, s.middle_name, s.last_name, s.admission_number
                FROM co_curricular_participants p
                JOIN students s ON p.student_id = s.id
                WHERE p.activity_id = %s
                ORDER BY s.first_name ASC;
            """, (activity_id,))
            enrolled = cur.fetchall()
            enrolled_ids = {s['id'] for s in enrolled}

            search = search.strip()
            candidates = []
            if search:
                cur.execute("""
                    SELECT id, first_name, middle_name, last_name, admission_number FROM students
                    WHERE school_id = %s AND (status IS NULL OR status != 'GRADUATED')
                      AND (LOWER(first_name || ' ' || COALESCE(middle_name, '') || ' ' || last_name) LIKE LOWER(%s) OR admission_number LIKE %s)
                    ORDER BY first_name ASC LIMIT 20;
                """, (school_id, f"%{search}%", f"%{search}%"))
                candidates = [s for s in cur.fetchall() if s['id'] not in enrolled_ids]

    enrolled_html = "".join(f"""
        <div class="flex items-center justify-between py-2 border-b last:border-0">
            <span class="text-sm text-slate-700">{esc(full_student_name(s))} <span class="text-slate-400 font-mono text-xs">#{esc(s['admission_number'])}</span></span>
            <form action="/api/v1/timetable/co-curricular/roster/remove/{school_id}/{activity_id}/{s['id']}" method="post">
                <button type="submit" class="text-rose-600 hover:text-rose-800 text-xs font-bold">Remove</button>
            </form>
        </div>
    """ for s in enrolled)

    candidates_html = "".join(f"""
        <div class="flex items-center justify-between py-2 border-b last:border-0">
            <span class="text-sm text-slate-700">{esc(full_student_name(s))} <span class="text-slate-400 font-mono text-xs">#{esc(s['admission_number'])}</span></span>
            <form action="/api/v1/timetable/co-curricular/roster/add/{school_id}/{activity_id}" method="post">
                <input type="hidden" name="student_id" value="{s['id']}">
                <input type="hidden" name="search" value="{esc(search)}">
                <button type="submit" class="bg-emerald-700 hover:bg-emerald-800 text-white text-xs font-bold px-3 py-1 rounded-lg transition">+ Add</button>
            </form>
        </div>
    """ for s in candidates)

    return f"""
    <!DOCTYPE html>
    <html>
    <head><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Elimu Hub | Roster — {esc(activity['name'])}</title><script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script></head>
    <body class="bg-slate-100 min-h-screen p-4 sm:p-8">
        <div class="max-w-2xl mx-auto space-y-4">
            <div class="bg-white p-6 rounded-2xl border shadow-xs">
                <h2 class="text-lg font-black text-slate-800">{esc(activity['name'])} — Roster</h2>
                <p class="text-xs text-slate-400">{esc(activity['category'])} · {len(enrolled)} enrolled</p>
            </div>

            <div class="bg-white p-6 rounded-2xl border shadow-xs">
                <h3 class="text-sm font-bold text-slate-800 mb-3">Find a Student to Add</h3>
                <form method="get" class="flex gap-2 mb-3">
                    <input type="text" name="search" value="{esc(search)}" placeholder="Search by name or admission number..." class="flex-1 border p-2.5 rounded-lg text-sm" autocomplete="off">
                    <button type="submit" class="bg-indigo-700 hover:bg-indigo-800 text-white font-bold px-4 py-2.5 rounded-lg text-sm transition">Search</button>
                </form>
                <div>{candidates_html or ("<p class='text-slate-400 text-xs italic p-2'>No matches found.</p>" if search else "<p class='text-slate-400 text-xs italic p-2'>Search above to find students to add.</p>")}</div>
            </div>

            <div class="bg-white p-6 rounded-2xl border shadow-xs">
                <h3 class="text-sm font-bold text-slate-800 mb-3">Enrolled Students</h3>
                <div>{enrolled_html or "<p class='text-slate-400 text-xs italic p-2'>No students enrolled yet.</p>"}</div>
            </div>

            <a href="/timetable/co-curricular/{school_id}" class="bg-slate-200 hover:bg-slate-300 text-slate-700 font-bold py-2.5 px-5 rounded-xl text-sm transition inline-block">← Back to Activities</a>
        </div>
    </body>
    </html>
    """


@router.post("/api/v1/timetable/co-curricular/roster/add/{school_id}/{activity_id}")
def add_co_curricular_participant(school_id: int, activity_id: int, request: Request, student_id: int = Form(...), search: str = Form("")):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO co_curricular_participants (activity_id, student_id) VALUES (%s, %s)
                ON CONFLICT (activity_id, student_id) DO NOTHING;
            """, (activity_id, student_id))
            conn.commit()

    return RedirectResponse(url=f"/timetable/co-curricular/{school_id}/{activity_id}/roster?search={urllib.parse.quote(search)}", status_code=303)


@router.post("/api/v1/timetable/co-curricular/roster/remove/{school_id}/{activity_id}/{student_id}")
def remove_co_curricular_participant(school_id: int, activity_id: int, student_id: int, request: Request):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM co_curricular_participants WHERE activity_id = %s AND student_id = %s;", (activity_id, student_id))
            conn.commit()

    return RedirectResponse(url=f"/timetable/co-curricular/{school_id}/{activity_id}/roster", status_code=303)


@router.get("/timetable/availability/{school_id}/{teacher_id}", response_class=HTMLResponse)
def teacher_availability_grid(school_id: int, teacher_id: int, request: Request, education_level: str = "Lower Primary"):
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

            periods = [p for p in get_periods_for_level(cur, school_id, education_level) if p['is_teaching_period']]

            cur.execute("""
                SELECT day_of_week, period_id, status FROM teacher_availability
                WHERE school_id = %s AND staff_user_id = %s;
            """, (school_id, teacher_id))
            current = {(r['day_of_week'], r['period_id']): r['status'] for r in cur.fetchall()}

    status_options = [("available", "✅ Available"), ("conditional", "❔ Conditional"), ("not_available", "❌ Not Available")]

    level_tabs = "".join(
        f"""<a href="/timetable/availability/{school_id}/{teacher_id}?education_level={urllib.parse.quote(lvl)}"
               class="px-3 py-1.5 rounded-lg text-xs font-bold transition {'bg-indigo-700 text-white' if lvl == education_level else 'bg-white border border-slate-200 text-slate-600 hover:bg-slate-50'}">{lvl}</a>"""
        for lvl in EDUCATION_LEVELS
    )

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
    <head><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Elimu Hub | Availability — {esc(teacher['full_name'] or teacher['email'])}</title><script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script></head>
    <body class="bg-slate-100 min-h-screen p-4 sm:p-8">
        <div class="max-w-4xl mx-auto bg-white p-6 rounded-2xl border shadow-xs">
            <h2 class="text-lg font-black text-slate-800">👩‍🏫 {esc(teacher['full_name'] or teacher['email'])} — Availability</h2>
            <p class="text-xs text-slate-400 mb-3">Mark when this teacher is unavailable (e.g. part-time, other commitments). The timetable generator and manual editor will both respect this.</p>
            <div class="flex gap-2 flex-wrap mb-4">
                <span class="text-xs font-bold text-slate-500 self-center mr-1">Level:</span>{level_tabs}
            </div>
            <form action="/api/v1/timetable/availability/update/{school_id}/{teacher_id}?education_level={urllib.parse.quote(education_level)}" method="post">
                <div class="overflow-x-auto">
                    <table class="w-full border-collapse text-xs">
                        <thead><tr><th class="p-2 sticky left-0 bg-white"></th>{header_cells}</tr></thead>
                        <tbody>{body_rows or "<tr><td class='p-4 text-slate-400 italic text-xs' colspan='99'>No periods configured for this level yet.</td></tr>"}</tbody>
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
async def save_teacher_availability(school_id: int, teacher_id: int, request: Request, education_level: str = "Lower Primary"):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    form = await request.form()

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE id = %s AND school_id = %s AND role = 'staff';", (teacher_id, school_id))
            if not cur.fetchone():
                raise HTTPException(status_code=404, detail="Teacher not found.")

            cur.execute("""
                SELECT id FROM timetable_periods WHERE school_id = %s AND is_teaching_period = TRUE
                AND education_level = (
                    CASE WHEN EXISTS (SELECT 1 FROM timetable_periods WHERE school_id = %s AND education_level = %s)
                         THEN %s ELSE 'ALL' END
                );
            """, (school_id, school_id, education_level, education_level))
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

    return RedirectResponse(url=f"/timetable/availability/{school_id}/{teacher_id}?education_level={urllib.parse.quote(education_level)}", status_code=303)


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
    <head><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Elimu Hub | Subject Time Off</title><script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script></head>
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
            cur.execute("SELECT id, name, education_level FROM learning_areas WHERE id = %s;", (learning_area_id,))
            subject = cur.fetchone()
            if not subject:
                raise HTTPException(status_code=404, detail="Subject not found.")

            days = get_school_days(cur, school_id)
            conn.commit()

            periods = [p for p in get_periods_for_level(cur, school_id, subject['education_level']) if p['is_teaching_period']]

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
    <head><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Elimu Hub | Time Off — {esc(subject['name'])}</title><script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script></head>
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
            cur.execute("SELECT education_level FROM learning_areas WHERE id = %s;", (learning_area_id,))
            subject_row = cur.fetchone()
            if not subject_row:
                raise HTTPException(status_code=404, detail="Subject not found.")
            subject_level = subject_row[0]

            cur.execute("""
                SELECT id FROM timetable_periods WHERE school_id = %s AND is_teaching_period = TRUE
                AND education_level = (
                    CASE WHEN EXISTS (SELECT 1 FROM timetable_periods WHERE school_id = %s AND education_level = %s)
                         THEN %s ELSE 'ALL' END
                );
            """, (school_id, school_id, subject_level, subject_level))
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

            periods_by_level = {
                lvl: [p for p in get_periods_for_level(cur, school_id, lvl) if p['is_teaching_period']]
                for lvl in EDUCATION_LEVELS
            }

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
        periods = periods_by_level.get(s['education_level'], [])
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
    <head><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Elimu Hub | Same-Time Subject Rules</title><script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script></head>
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
    <head><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Elimu Hub | Subject Constraints</title><script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script></head>
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
def timetable_grade_view(school_id: int, request: Request, grade_name: str, education_level: str, stream: str, test_issues: str = None):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT name FROM schools WHERE id = %s;", (school_id,))
            school = cur.fetchone()

            days = get_school_days(cur, school_id)
            conn.commit()

            periods = get_periods_for_level(cur, school_id, education_level)

            cur.execute("SELECT id, name FROM learning_areas WHERE education_level = %s;", (education_level,))
            subjects = sort_subjects_for_display(cur.fetchall(), education_level)

            cur.execute(
                "SELECT id, name FROM timetable_custom_subjects WHERE school_id = %s AND education_level = %s ORDER BY name ASC;",
                (school_id, education_level)
            )
            custom_subjects = cur.fetchall()

            cur.execute(
                "SELECT id, name FROM co_curricular_activities WHERE school_id = %s ORDER BY name ASC;",
                (school_id,)
            )
            activities = cur.fetchall()

            cur.execute("""
                SELECT ts.day_of_week, ts.period_id, ts.learning_area_id, ts.custom_subject_id, ts.co_curricular_activity_id,
                       la.name AS academic_name, cs.name AS custom_name, ca.name AS activity_name,
                       u.full_name, u.email
                FROM timetable_slots ts
                LEFT JOIN learning_areas la ON ts.learning_area_id = la.id
                LEFT JOIN timetable_custom_subjects cs ON ts.custom_subject_id = cs.id
                LEFT JOIN co_curricular_activities ca ON ts.co_curricular_activity_id = ca.id
                LEFT JOIN users u ON ts.staff_user_id = u.id
                WHERE ts.school_id = %s AND ts.grade_name = %s AND ts.education_level = %s AND ts.stream = %s;
            """, (school_id, grade_name, education_level, stream))
            slot_map = {(r['day_of_week'], r['period_id']): r for r in cur.fetchall()}

    encoded_grade = urllib.parse.quote(grade_name)
    encoded_level = urllib.parse.quote(education_level)
    encoded_stream = urllib.parse.quote(stream)
    section_label = _section_label(grade_name, stream)
    header_cells = "".join(f"<th class='p-2 text-center'>{d}</th>" for d in days)

    subject_optgroup = "".join(f"<option value='subject:{s['id']}'>{esc(s['name'])}</option>" for s in subjects)
    custom_optgroup = "".join(f"<option value='custom:{s['id']}'>{esc(s['name'])}</option>" for s in custom_subjects)
    activity_optgroup = "".join(f"<option value='activity:{a['id']}'>{esc(a['name'])}</option>" for a in activities)

    body_rows = ""
    for p in periods:
        p_type = p.get('period_type') or ('teaching' if p['is_teaching_period'] else 'break')
        if p_type in ('break', 'prep'):
            label_note = "Prep Time (protected)" if p_type == 'prep' else p['label']
            body_rows += f"""
            <tr class="bg-slate-50">
                <td class="p-2 text-xs font-bold text-slate-500 whitespace-nowrap">{esc(p['label'])}<br><span class="font-normal text-slate-400">{esc(p['start_time'] or '')}–{esc(p['end_time'] or '')}</span></td>
                <td colspan="{len(days)}" class="p-2 text-center text-xs italic text-slate-400">{esc(label_note)}</td>
            </tr>
            """
            continue

        row_cells = ""
        for day in days:
            slot = slot_map.get((day, p['id']))
            if slot and slot['learning_area_id']:
                current_value = f"subject:{slot['learning_area_id']}"
            elif slot and slot['custom_subject_id']:
                current_value = f"custom:{slot['custom_subject_id']}"
            elif slot and slot['co_curricular_activity_id']:
                current_value = f"activity:{slot['co_curricular_activity_id']}"
            else:
                current_value = ""
            teacher_label = (slot['full_name'] or slot['email']) if slot and (slot['full_name'] or slot['email']) else None
            slot_name = (slot['academic_name'] or slot['custom_name'] or slot['activity_name']) if slot else None
            cell_bg = f"background:{get_subject_color(slot_name)[0]};" if slot_name else ""
            options = f"""<option value=''>— Free —</option>
                <optgroup label="Academic Subjects">{subject_optgroup}</optgroup>
                {"<optgroup label='Custom Subjects'>" + custom_optgroup + "</optgroup>" if custom_optgroup else ""}
                {"<optgroup label='Co-Curricular'>" + activity_optgroup + "</optgroup>" if activity_optgroup else ""}
            """
            # Mark the currently-selected option, whichever group it's in.
            options = options.replace(f"value='{current_value}'", f"value='{current_value}' selected", 1) if current_value else options
            row_cells += f"""
            <td class="p-1.5 align-top" style="{cell_bg}">
                <form action="/api/v1/timetable/slot/update/{school_id}" method="post" class="space-y-1">
                    <input type="hidden" name="grade_name" value="{esc(grade_name)}">
                    <input type="hidden" name="education_level" value="{esc(education_level)}">
                    <input type="hidden" name="stream" value="{esc(stream)}">
                    <input type="hidden" name="day_of_week" value="{day}">
                    <input type="hidden" name="period_id" value="{p['id']}">
                    <select name="subject_choice" onchange="this.form.submit()" class="w-full border p-1.5 rounded-lg text-[11px] font-semibold bg-white">{options}</select>
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

    test_issues_html = ""
    if test_issues:
        try:
            import base64, json
            decoded = json.loads(base64.b64decode(test_issues).decode("utf-8"))
            errors_list = decoded.get("errors", [])
            warnings_list = decoded.get("warnings", [])
        except Exception:
            errors_list, warnings_list = [], []

        if errors_list:
            error_items = "".join(f"<li>{esc(e)}</li>" for e in errors_list)
            test_issues_html += f"""
            <div class="bg-rose-50 border border-rose-200 text-rose-800 text-sm px-4 py-3 rounded-xl mb-3 mx-6 mt-4">
                <p class="font-bold mb-1">🧪 Test found {len(errors_list)} problem(s) — generation was NOT run:</p>
                <ul class="list-disc list-inside space-y-1 text-xs">{error_items}</ul>
            </div>
            """
        if warnings_list:
            warning_items = "".join(f"<li>{esc(w)}</li>" for w in warnings_list)
            test_issues_html += f"""
            <div class="bg-amber-50 border border-amber-200 text-amber-800 text-sm px-4 py-3 rounded-xl mb-3 mx-6 mt-4">
                <p class="font-bold mb-1">⚠️ {len(warnings_list)} warning(s){' (generation still ran)' if not errors_list else ''}:</p>
                <ul class="list-disc list-inside space-y-1 text-xs">{warning_items}</ul>
            </div>
            """

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Elimu Hub | Timetable — {esc(section_label)}</title>
        <script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script>
        <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap" rel="stylesheet">
        <style>body {{ font-family: 'Plus Jakarta Sans', sans-serif; }}</style>
    </head>
    <body class="bg-[#F7F9F8] min-h-screen">
        <header class="bg-white border-b px-6 py-4 flex flex-col sm:flex-row sm:justify-between sm:items-center gap-3">
            <div>
                <h1 class="text-base font-bold text-slate-900">📅 {esc(section_label)} Timetable</h1>
                <p class="text-xs text-slate-400">{esc(school['name'] if school else '')} — {esc(education_level)}</p>
            </div>
            <div class="flex flex-wrap gap-2">
                <form action="/api/v1/timetable/new/{school_id}" method="post" onsubmit="return confirm('Start a brand-new BLANK timetable for {esc(section_label)}? This clears every period currently scheduled — you\\'ll build it up from scratch by hand. This cannot be undone.');">
                    <input type="hidden" name="grade_name" value="{esc(grade_name)}">
                    <input type="hidden" name="education_level" value="{esc(education_level)}">
                    <input type="hidden" name="stream" value="{esc(stream)}">
                    <button type="submit" class="bg-white border border-slate-200 hover:bg-slate-50 text-slate-600 px-4 py-2 rounded-xl text-xs font-bold transition">＋ New</button>
                </form>
                <form action="/api/v1/timetable/test-and-generate/{school_id}" method="post" onsubmit="return confirm('Test the setup and generate a fresh draft timetable for {esc(section_label)}? This replaces any existing entries for this class.');">
                    <input type="hidden" name="grade_name" value="{esc(grade_name)}">
                    <input type="hidden" name="education_level" value="{esc(education_level)}">
                    <input type="hidden" name="stream" value="{esc(stream)}">
                    <button type="submit" class="bg-amber-500 hover:bg-amber-600 text-white px-4 py-2 rounded-xl text-xs font-bold transition shadow-sm">🧪 Test &amp; Generate</button>
                </form>
                <a href="/timetable/print/{school_id}?grade_name={encoded_grade}&education_level={encoded_level}&stream={encoded_stream}" target="_blank" class="bg-teal-700 hover:bg-teal-800 text-white px-4 py-2 rounded-xl text-xs font-bold transition">🖨 Print</a>
                <a href="/timetable/assignments/{school_id}?grade_name={encoded_grade}&education_level={encoded_level}&stream={encoded_stream}" class="bg-white border border-slate-200 hover:bg-slate-50 text-slate-600 px-4 py-2 rounded-xl text-xs font-bold transition">Teachers</a>
                <a href="/timetable/dashboard/{school_id}" class="bg-white border border-slate-200 hover:bg-slate-50 text-slate-600 px-4 py-2 rounded-xl text-xs font-bold transition">← Back</a>
            </div>
        </header>
        {test_issues_html}
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


@router.post("/api/v1/timetable/test-and-generate/{school_id}")
def test_and_generate_timetable(school_id: int, request: Request, grade_name: str = Form(...), education_level: str = Form(...), stream: str = Form(...)):
    """The 'Test & Generate' entry point: runs the existing
    validate_timetable_setup check first. If it finds any hard errors,
    generation is skipped entirely and the errors are shown, each naming
    the exact subject/teacher/reason. Only proceeds to actually generate if
    there are zero hard errors — warnings alone don't block it, but are
    shown after generating too."""
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            errors, warnings = validate_timetable_setup(cur, school_id, grade_name, education_level, stream)

    if errors:
        import base64, json
        payload = base64.b64encode(json.dumps({"errors": errors, "warnings": warnings}).encode("utf-8")).decode("ascii")
        return RedirectResponse(
            url=f"/timetable/grade/{school_id}?grade_name={urllib.parse.quote(grade_name)}&education_level={urllib.parse.quote(education_level)}&stream={urllib.parse.quote(stream)}&test_issues={payload}",
            status_code=303,
        )

    # No hard errors — proceed to the actual generator, reusing its route
    # directly so the real generation logic lives in exactly one place.
    # Any warnings get passed through as a query param so they still show
    # up alongside the newly-generated timetable.
    response = generate_draft_timetable(school_id, request, grade_name, education_level, stream)
    if warnings and isinstance(response, RedirectResponse):
        import base64, json
        warn_payload = base64.b64encode(json.dumps({"errors": [], "warnings": warnings}).encode("utf-8")).decode("ascii")
        separator = "&" if "?" in response.headers["location"] else "?"
        response.headers["location"] = response.headers["location"] + f"{separator}test_issues={warn_payload}"
    return response


@router.post("/api/v1/timetable/test-and-generate-level/{school_id}")
def test_and_generate_whole_level(school_id: int, request: Request, education_level: str = Form(...)):
    """Runs Test & Generate across EVERY class in one education level, not
    just a single class — this is the real fix for the cross-class
    double-booking problem, since generating one class at a time in
    isolation is exactly what let those collisions slip through before.
    A class with hard errors is skipped (not generated) but doesn't block
    the rest of the level from being processed. After every class has been
    attempted, the collision checker runs automatically as a final safety
    net, and everything is shown in one combined report."""
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT DISTINCT c.grade_name, s.stream
                FROM students s
                JOIN classes c ON s.class_id = c.id
                WHERE s.school_id = %s AND c.education_level = %s AND (s.status IS NULL OR s.status != 'GRADUATED')
                ORDER BY c.grade_name ASC, s.stream ASC;
            """, (school_id, education_level))
            sections = cur.fetchall()

    class_results = []
    for sec in sections:
        grade_name, stream = sec['grade_name'], sec['stream']
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                errors, warnings = validate_timetable_setup(cur, school_id, grade_name, education_level, stream)

        if errors:
            class_results.append({'grade_name': grade_name, 'stream': stream, 'status': 'skipped', 'errors': errors, 'warnings': warnings})
            continue

        generate_draft_timetable(school_id, request, grade_name, education_level, stream)
        class_results.append({'grade_name': grade_name, 'stream': stream, 'status': 'generated', 'errors': [], 'warnings': warnings})

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            collisions = _find_timetable_collisions(cur, school_id, education_level)

    import base64, json
    payload = base64.b64encode(json.dumps({
        'education_level': education_level,
        'class_results': class_results,
        'collision_count': len(collisions),
        'collisions': [
            {
                'teacher': (slots[0]['full_name'] or slots[0]['email'] or 'Unknown teacher'),
                'day': slots[0]['day_of_week'],
                'period': slots[0]['period_label'],
                'classes': [f"{s['grade_name']} — {s['stream']} ({s['subject_name'] or 'Unknown subject'})" for s in slots],
            }
            for slots in collisions
        ],
    }).encode("utf-8")).decode("ascii")

    return RedirectResponse(url=f"/timetable/level-report/{school_id}?report={payload}", status_code=303)


@router.get("/timetable/level-report/{school_id}", response_class=HTMLResponse)
def timetable_level_report(school_id: int, request: Request, report: str):
    """Displays the combined report from a whole-level Test & Generate run:
    per-class outcome (generated / skipped with reasons) plus the automatic
    post-generation collision check across the whole level."""
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    import base64, json
    try:
        data = json.loads(base64.b64decode(report).decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid or expired report data.")

    education_level = data.get('education_level', '')
    class_results = data.get('class_results', [])
    collisions = data.get('collisions', [])

    generated_count = sum(1 for r in class_results if r['status'] == 'generated')
    skipped_count = sum(1 for r in class_results if r['status'] == 'skipped')

    class_rows_html = ""
    for r in class_results:
        section_label = r['grade_name'] if r['stream'] == 'SINGLE STREAM' else f"{r['grade_name']} — {r['stream']}"
        encoded_grade = urllib.parse.quote(r['grade_name'])
        encoded_stream = urllib.parse.quote(r['stream'])
        encoded_level = urllib.parse.quote(education_level)
        if r['status'] == 'generated':
            status_badge = "<span class='text-[10px] font-bold px-2 py-0.5 rounded-full bg-emerald-50 text-emerald-700 border border-emerald-200'>✅ Generated</span>"
            detail_html = "".join(f"<li class='text-amber-700'>⚠️ {esc(w)}</li>" for w in r['warnings'])
        else:
            status_badge = "<span class='text-[10px] font-bold px-2 py-0.5 rounded-full bg-rose-50 text-rose-700 border border-rose-200'>❌ Skipped</span>"
            detail_html = "".join(f"<li class='text-rose-700'>{esc(e)}</li>" for e in r['errors'])
        class_rows_html += f"""
        <div class="bg-white rounded-2xl border shadow-xs p-4 mb-3">
            <div class="flex items-center justify-between">
                <a href="/timetable/grade/{school_id}?grade_name={encoded_grade}&education_level={encoded_level}&stream={encoded_stream}" class="text-sm font-bold text-slate-800 hover:underline">{esc(section_label)}</a>
                {status_badge}
            </div>
            {f"<ul class='text-xs mt-2 space-y-1 list-disc list-inside'>{detail_html}</ul>" if detail_html else ""}
        </div>
        """

    collision_html = ""
    if collisions:
        for c in collisions:
            classes_html = "".join(f"<li>{esc(cl)}</li>" for cl in c['classes'])
            collision_html += f"""
            <div class="bg-white rounded-2xl border border-rose-200 shadow-xs p-4 mb-3">
                <p class="text-sm font-bold text-rose-700">⚠️ {esc(c['teacher'])} — double-booked</p>
                <p class="text-xs text-slate-400 mb-2">{esc(c['day'])}, {esc(c['period'])}</p>
                <ul class="text-xs text-slate-700 list-disc list-inside">{classes_html}</ul>
            </div>
            """

    return f"""
    <!DOCTYPE html>
    <html>
    <head><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Elimu Hub | Level Test &amp; Generate Report</title><script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script></head>
    <body class="bg-[#F7F9F8] min-h-screen">
        <header class="bg-white border-b px-6 sm:px-8 py-4 flex flex-col sm:flex-row sm:justify-between sm:items-center gap-2">
            <div>
                <h1 class="text-base font-bold text-slate-900">🧪 Test &amp; Generate Report — {esc(education_level)}</h1>
                <p class="text-xs text-slate-400">{generated_count} class(es) generated, {skipped_count} skipped, {len(collisions)} collision(s) found after generation.</p>
            </div>
            <a href="/timetable/dashboard/{school_id}" class="bg-slate-800 hover:bg-slate-900 text-white px-4 py-2 rounded-xl text-xs font-bold transition">← Back to Timetables</a>
        </header>
        <div class="p-4 sm:p-8 max-w-3xl mx-auto space-y-6">
            <div>
                <h2 class="text-xs font-bold uppercase tracking-wider text-slate-400 mb-2">Per-Class Results</h2>
                {class_rows_html or "<p class='text-slate-400 text-xs italic'>No classes found for this level.</p>"}
            </div>
            <div>
                <h2 class="text-xs font-bold uppercase tracking-wider text-slate-400 mb-2">Collision Check (After Generation)</h2>
                {collision_html or "<div class='bg-emerald-50 border border-emerald-200 text-emerald-800 text-sm px-4 py-3 rounded-xl font-semibold'>✅ No collisions found — every teacher is scheduled in exactly one place at a time across this level.</div>"}
            </div>
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

            teaching_periods = [p for p in get_periods_for_level(cur, school_id, education_level) if p['is_teaching_period']]

            cur.execute("SELECT id, name FROM learning_areas WHERE education_level = %s;", (education_level,))
            subjects = sort_subjects_for_display(cur.fetchall(), education_level)

            cur.execute("""
                SELECT learning_area_id, staff_user_id, lessons_per_week, requires_double FROM teacher_subject_assignments
                WHERE school_id = %s AND grade_name = %s AND education_level = %s AND stream = %s;
            """, (school_id, grade_name, education_level, stream))
            assignment_rows = cur.fetchall()
            teacher_for_subject = {r['learning_area_id']: r['staff_user_id'] for r in assignment_rows}
            lessons_per_week_for_subject = {r['learning_area_id']: r['lessons_per_week'] for r in assignment_rows}
            requires_double_for_subject = {r['learning_area_id']: r['requires_double'] for r in assignment_rows}

            if not subjects or not teaching_periods:
                raise HTTPException(status_code=400, detail="No subjects or teaching periods configured — nothing to generate.")

            teaching_period_ids = {p['id'] for p in teaching_periods}

            # True consecutive pairs for double lessons — two teaching periods
            # are only "back to back" if their period_order differs by
            # exactly 1 with nothing between them. Using the position in the
            # teaching-only list would wrongly treat two periods separated by
            # a filtered-out break as consecutive.
            sorted_teaching = sorted(teaching_periods, key=lambda p: p['period_order'])
            consecutive_period_pairs = [
                (sorted_teaching[i], sorted_teaching[i + 1])
                for i in range(len(sorted_teaching) - 1)
                if sorted_teaching[i + 1]['period_order'] - sorted_teaching[i]['period_order'] == 1
            ]

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

            # How many lessons each subject still needs this week — defaults
            # to 1 for any subject without an explicit assignment configured.
            remaining = {subj['id']: max(1, lessons_per_week_for_subject.get(subj['id'], 1)) for subj in free_subjects}
            filled = {}       # (day, period_id) -> subject already placed there
            used_today_by_day = {day: set() for day in days}
            last_subject_by_day = {day: None for day in days}

            def _place(day, period_id, subject, teacher):
                cur.execute("""
                    INSERT INTO timetable_slots (school_id, grade_name, education_level, stream, day_of_week, period_id, learning_area_id, staff_user_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s);
                """, (school_id, grade_name, education_level, stream, day, period_id, subject['id'], teacher))
                filled[(day, period_id)] = subject
                if teacher:
                    booked[(day, period_id)] = teacher
                used_today_by_day[day].add(subject['id'])
                remaining[subject['id']] -= 1

            # --- Phase 1: place locked "same time" subjects first — these
            # always win their slot outright, not drawn from the queue. ---
            for (day, period_id), locked_subject in locked_placements.items():
                if day not in days or period_id not in teaching_period_ids:
                    continue
                lcid = locked_subject['id']
                chosen_teacher = teacher_for_subject.get(lcid)
                if chosen_teacher and booked.get((day, period_id)) == chosen_teacher:
                    chosen_teacher = None  # conflict — place subject anyway, flagged for manual fix
                _place(day, period_id, locked_subject, chosen_teacher)
                last_subject_by_day[day] = lcid
                if lcid in remaining:
                    remaining[lcid] = max(0, remaining[lcid] - 1)

            # --- Phase 2: subjects needing at least one double lesson (e.g.
            # for practicals) get a genuine back-to-back pair placed first,
            # before anything else competes for those slots. ---
            for subj in free_subjects:
                sid = subj['id']
                if not requires_double_for_subject.get(sid) or remaining.get(sid, 0) < 2:
                    continue
                cand_teacher = teacher_for_subject.get(sid)
                placed = False
                for day in days:
                    if placed:
                        break
                    if sid in used_today_by_day[day]:
                        continue  # already has a lesson today — keep doubles on their own day
                    for p1, p2 in consecutive_period_pairs:
                        if (day, p1['id']) in filled or (day, p2['id']) in filled:
                            continue
                        if (sid, day, p1['id']) in subject_unavailable or (sid, day, p2['id']) in subject_unavailable:
                            continue
                        if cand_teacher and (
                            (cand_teacher, day, p1['id']) in unavailable or (cand_teacher, day, p2['id']) in unavailable
                            or booked.get((day, p1['id'])) == cand_teacher or booked.get((day, p2['id'])) == cand_teacher
                        ):
                            continue
                        _place(day, p1['id'], subj, cand_teacher)
                        _place(day, p2['id'], subj, cand_teacher)
                        last_subject_by_day[day] = sid
                        placed = True
                        break
                # If no clean double slot exists anywhere, the subject simply
                # falls through to Phase 3 as ordinary single lessons instead
                # of forcing a bad placement.

            # --- Phase 3: fill remaining empty slots with each subject's
            # remaining single lessons, up to its weekly quota — once a
            # subject's quota is used up it drops out, and once every
            # subject's quota is used up, any leftover slots simply stay
            # free rather than being force-filled. ---
            queue = [subj for subj in free_subjects for _ in range(remaining.get(subj['id'], 0))]
            qi = 0
            for day in days:
                for period in teaching_periods:
                    if (day, period['id']) in filled:
                        continue
                    if not queue:
                        break  # every subject's quota is met — this slot stays free

                    used_today = used_today_by_day[day]
                    last_subject_id = last_subject_by_day[day]

                    chosen_idx, chosen_subject, chosen_teacher = None, None, None
                    # Priority order: (avoid_conditional, avoid_teacher_conflict) —
                    # try hardest for a fully clean pick first (no conditional
                    # clashes, no teacher conflict), and only relax one
                    # constraint at a time. This matters specifically because
                    # the previous version accepted the FIRST subject that
                    # passed the non-teacher rules and only THEN checked for a
                    # teacher conflict (nulling the teacher if so) — meaning a
                    # later candidate in the same queue that had no conflict
                    # at all was never even considered.
                    for avoid_conditional, avoid_teacher_conflict in ((True, True), (True, False), (False, True), (False, False)):
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
                                continue
                            if avoid_conditional and (cid, day, period['id']) in subject_conditional:
                                continue

                            cand_teacher = teacher_for_subject.get(cid)
                            if cand_teacher and (cand_teacher, day, period['id']) in unavailable:
                                continue
                            if cand_teacher and avoid_conditional and (cand_teacher, day, period['id']) in conditional:
                                continue
                            if cand_teacher and booked.get((day, period['id'])) == cand_teacher:
                                if avoid_teacher_conflict:
                                    continue  # try a different candidate first rather than nulling this one's teacher
                                cand_teacher = None

                            chosen_idx, chosen_subject, chosen_teacher = idx, candidate, cand_teacher
                            break

                    if chosen_subject is None:
                        # Nothing satisfied every rule — fall back to the
                        # plain round-robin pick rather than leave a gap,
                        # as long as some subject still has quota left.
                        chosen_idx = qi % len(queue)
                        chosen_subject = queue[chosen_idx]
                        chosen_teacher = teacher_for_subject.get(chosen_subject['id'])
                        if chosen_teacher and (
                            booked.get((day, period['id'])) == chosen_teacher
                            or (chosen_teacher, day, period['id']) in unavailable
                        ):
                            chosen_teacher = None

                    qi = chosen_idx + 1
                    _place(day, period['id'], chosen_subject, chosen_teacher)
                    last_subject_by_day[day] = chosen_subject['id']
                    # Remove exactly one used-up occurrence from the queue so
                    # a satisfied subject can't be picked again.
                    queue = [s for s in queue if s['id'] != chosen_subject['id']] + (
                        [chosen_subject] * remaining.get(chosen_subject['id'], 0)
                    )

            conn.commit()

    encoded_grade = urllib.parse.quote(grade_name)
    encoded_level = urllib.parse.quote(education_level)
    encoded_stream = urllib.parse.quote(stream)
    return RedirectResponse(url=f"/timetable/grade/{school_id}?grade_name={encoded_grade}&education_level={encoded_level}&stream={encoded_stream}", status_code=303)


@router.post("/api/v1/timetable/new/{school_id}")
def create_blank_timetable(school_id: int, request: Request, grade_name: str = Form(...), education_level: str = Form(...), stream: str = Form(...)):
    """Wipes this section's timetable to a completely blank slate — every
    period free — as a starting point for building one by hand, entirely
    separate from Generate Draft (which auto-fills subjects)."""
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM timetable_slots WHERE school_id = %s AND grade_name = %s AND education_level = %s AND stream = %s;",
                (school_id, grade_name, education_level, stream)
            )
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
    subject_choice: str = Form(""),
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
            cur.execute("SELECT period_type, is_teaching_period, label FROM timetable_periods WHERE id = %s;", (period_id,))
            period_row = cur.fetchone()
            period_type = (period_row.get('period_type') if period_row else None) or ('teaching' if (period_row and period_row['is_teaching_period']) else 'break')
            if period_type in ('break', 'prep'):
                raise HTTPException(
                    status_code=400,
                    detail=f"'{period_row['label'] if period_row else 'This period'}' is a {('protected prep-time' if period_type == 'prep' else 'break')} period and can never have a lesson or activity scheduled into it."
                )

            if not subject_choice:
                cur.execute("""
                    DELETE FROM timetable_slots
                    WHERE school_id = %s AND grade_name = %s AND education_level = %s AND stream = %s AND day_of_week = %s AND period_id = %s;
                """, (school_id, grade_name, education_level, stream, day_of_week, period_id))
                conn.commit()
                return RedirectResponse(url=redirect_url, status_code=303)

            kind, _, raw_id = subject_choice.partition(":")
            item_id = int(raw_id) if raw_id else None
            learning_area_id = item_id if kind == "subject" else None
            custom_subject_id = item_id if kind == "custom" else None
            co_curricular_activity_id = item_id if kind == "activity" else None
            teacher_id = None

            if kind == "subject":
                cur.execute("""
                    SELECT staff_user_id FROM teacher_subject_assignments
                    WHERE school_id = %s AND learning_area_id = %s AND grade_name = %s AND education_level = %s AND stream = %s;
                """, (school_id, learning_area_id, grade_name, education_level, stream))
                assignment = cur.fetchone()
                teacher_id = assignment['staff_user_id'] if assignment else None
            elif kind == "activity":
                # A co-curricular activity's supervisor doubles as the
                # "teacher" for this slot, for conflict-checking purposes.
                cur.execute("SELECT staff_user_id FROM co_curricular_activities WHERE id = %s AND school_id = %s;", (co_curricular_activity_id, school_id))
                activity_row = cur.fetchone()
                teacher_id = activity_row['staff_user_id'] if activity_row else None

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

            # The rest of these checks (subject placement constraints, time
            # off, "same time" locks) only make sense for real academic
            # subjects — custom subjects and co-curricular activities aren't
            # tracked in those tables, so they're skipped for those cases.
            if kind == "subject":
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
                INSERT INTO timetable_slots (school_id, grade_name, education_level, stream, day_of_week, period_id, learning_area_id, custom_subject_id, co_curricular_activity_id, staff_user_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (school_id, grade_name, education_level, stream, day_of_week, period_id)
                DO UPDATE SET learning_area_id = EXCLUDED.learning_area_id, custom_subject_id = EXCLUDED.custom_subject_id,
                              co_curricular_activity_id = EXCLUDED.co_curricular_activity_id, staff_user_id = EXCLUDED.staff_user_id;
            """, (school_id, grade_name, education_level, stream, day_of_week, period_id, learning_area_id, custom_subject_id, co_curricular_activity_id, teacher_id))
            conn.commit()

    return RedirectResponse(url=redirect_url, status_code=303)


def _build_timetable_grid_html(days, periods, cell_lookup_fn):
    """Builds the <table> for a printable timetable laid out exactly like a
    physical school timetable: rows = days, columns = periods in order.
    Break periods (short break, lunch, etc.) render as one column spanning
    every day row, with the label rotated vertically. Prep-time periods
    render as an ordinary per-day cell showing a fixed "PREP" label — they
    still take up a column per day (unlike breaks), but are never eligible
    to be assigned a subject; cell_lookup_fn is never even consulted for
    them. cell_lookup_fn(day, period) returns the inner HTML for a teaching-
    period cell (or None/'' for a free slot) — or a (html, bg_color_hex)
    tuple if the cell should be color-coded (e.g. by subject)."""
    header_cells = "".join(
        f"<th style='padding:8px 10px;font-size:13px;{'background:#eef2f7;' if not p['is_teaching_period'] else ''}'>{esc(p['short_label'] or p['label'])}</th>"
        for p in periods
    )
    time_cells = "".join(
        f"<th style='font-weight:normal;font-size:11px;color:#64748b;padding-bottom:6px;'>{esc(p['start_time'] or '')}-{esc(p['end_time'] or '')}</th>"
        for p in periods
    )

    body_rows = ""
    for day_i, day in enumerate(days):
        row = f"<td style='padding:12px 14px;font-weight:bold;font-size:14px;white-space:nowrap;border:1px solid #cbd5e1;'>{esc(day[:2].upper())}</td>"
        for p in periods:
            p_type = p.get('period_type') or ('teaching' if p['is_teaching_period'] else 'break')
            if p_type == 'break':
                if day_i == 0:
                    row += (
                        f"<td rowspan='{len(days)}' style='border:1px solid #cbd5e1;text-align:center;background:#f1f5f9;'>"
                        f"<div style='writing-mode:vertical-rl;transform:rotate(180deg);font-size:13px;font-weight:bold;"
                        f"color:#475569;white-space:nowrap;margin:0 auto;'>{esc(p['label'])}</div></td>"
                    )
                continue  # subsequent days: cell already covered by row 1's rowspan
            if p_type == 'prep':
                row += (
                    "<td style='padding:12px 10px;text-align:center;border:1px solid #e2e8f0;background:#f5f3ff;'>"
                    "<span style='font-size:12px;font-weight:bold;color:#6d28d9;'>PREP</span></td>"
                )
                continue

            result = cell_lookup_fn(day, p)
            cell_bg = ""
            if isinstance(result, tuple):
                content, bg_color = result
                cell_bg = f"background:{bg_color};" if bg_color else ""
            else:
                content = result
            content = content or "<span style='color:#cbd5e1;'>-</span>"
            row += f"<td style='padding:12px 10px;text-align:center;border:1px solid #e2e8f0;{cell_bg}'>{content}</td>"
        body_rows += f"<tr>{row}</tr>"

    return f"""
    <table style="width:100%;border-collapse:collapse;font-size:14px;margin-top:18px;">
        <thead>
            <tr style="background:#f8fafc;"><th style="padding:8px 10px;"></th>{header_cells}</tr>
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

            periods = get_periods_for_level(cur, school_id, education_level)

            cur.execute("""
                SELECT ts.day_of_week, ts.period_id,
                       COALESCE(la.name, cs.name, ca.name) AS subject_name, u.full_name, u.email
                FROM timetable_slots ts
                LEFT JOIN learning_areas la ON ts.learning_area_id = la.id
                LEFT JOIN timetable_custom_subjects cs ON ts.custom_subject_id = cs.id
                LEFT JOIN co_curricular_activities ca ON ts.co_curricular_activity_id = ca.id
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
        bg_color, text_color = get_subject_color(slot['subject_name'])
        content = f"<b style='color:{text_color};'>{esc(abbreviate_subject(slot['subject_name']))}</b>{teacher_line}"
        return (content, bg_color)

    grid_html = _build_timetable_grid_html(days, periods, _class_cell)

    if not periods:
        grid_html = "<p style='padding:24px;text-align:center;color:#94a3b8;font-style:italic;'>No periods configured yet for this school. Set them up on the Periods &amp; Days page first.</p>"

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Elimu Hub | Timetable — {esc(section_label)}</title>
        <style>
            @page {{ size: A4 landscape; margin: 12mm; }}
            body {{ font-family: Arial, sans-serif; padding: 20px; color: #1e293b; background:#f1f5f9; }}
            @media print {{ .no-print {{ display: none !important; }} body {{ background: white; padding: 0; }} }}
            th {{ background:#f8fafc; border-bottom:2px solid #cbd5e1; font-size:10px; text-transform:uppercase; color:#64748b; }}
            .print-page {{ max-width: 267mm; margin: 0 auto; background: white; padding: 14mm; border-radius: 10px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }}
            @media print {{ .print-page {{ box-shadow: none; border-radius: 0; padding: 0; max-width: 100%; }} }}
        </style>
    </head>
    <body>
        <div class="no-print" style="text-align:right; margin-bottom:16px; max-width:267mm; margin-left:auto; margin-right:auto;">
            <button onclick="window.print()" style="background:#4f46e5;color:white;border:none;padding:10px 18px;border-radius:8px;font-weight:bold;cursor:pointer;">🖨 Print / Save as PDF</button><p style="font-size:10px;color:#94a3b8;margin:6px 0 0;">Tip: in the print dialog, choose "Save as PDF" as the destination to download a file instead of printing on paper.</p>
        </div>
        <div class="print-page">
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
                <span>{esc(school['name'] if school else '')} — Powered by Elimu Hub</span>
            </div>
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

            cur.execute("""
                SELECT ts.day_of_week, ts.period_id, ts.grade_name, ts.stream, ts.education_level,
                       COALESCE(la.name, cs.name, ca.name) AS subject_name
                FROM timetable_slots ts
                LEFT JOIN learning_areas la ON ts.learning_area_id = la.id
                LEFT JOIN timetable_custom_subjects cs ON ts.custom_subject_id = cs.id
                LEFT JOIN co_curricular_activities ca ON ts.co_curricular_activity_id = ca.id
                WHERE ts.school_id = %s AND ts.staff_user_id = %s;
            """, (school_id, teacher_id))
            teacher_slots = cur.fetchall()

            # A teacher can teach across more than one level (e.g. Upper
            # Primary and Junior School), and each level can have its own
            # bell schedule — so render one grid per level they actually
            # have lessons in, each using that level's own periods.
            levels_taught = sorted({s['education_level'] for s in teacher_slots if s['education_level']}) or ["Lower Primary"]

            level_sections = []
            for level in levels_taught:
                level_periods = get_periods_for_level(cur, school_id, level)
                slot_map = {(r['day_of_week'], r['period_id']): r for r in teacher_slots if r['education_level'] == level}

                def _teacher_cell(day, p, slot_map=slot_map):
                    slot = slot_map.get((day, p['id']))
                    if not slot or not slot['subject_name']:
                        return None
                    class_label = _section_label(slot['grade_name'], slot['stream'])
                    bg_color, text_color = get_subject_color(slot['subject_name'])
                    content = f"<b style='color:{text_color};'>{esc(abbreviate_subject(slot['subject_name']))}</b><br><span style='font-size:9px;color:#64748b;'>{esc(class_label)}</span>"
                    return (content, bg_color)

                grid = _build_timetable_grid_html(days, level_periods, _teacher_cell)
                if not level_periods:
                    grid = "<p style='padding:16px;text-align:center;color:#94a3b8;font-style:italic;'>No periods configured yet for this level.</p>"
                level_sections.append(f"""
                    <h2 style="margin:20px 0 6px;font-size:13px;font-weight:bold;color:#4f46e5;">{esc(level)}</h2>
                    {grid}
                """)

    teacher_name = teacher['full_name'] or teacher['email']
    logo_html = ""
    if school and school.get('logo_url'):
        logo_src = school['logo_url']
        final_src = logo_src if logo_src.startswith("http") else f"/{logo_src.lstrip('/')}"
        logo_html = f"<img src='{final_src}' style='width:56px;height:56px;object-fit:contain;' />"

    grid_html = "".join(level_sections) if level_sections else "<p style='padding:24px;text-align:center;color:#94a3b8;font-style:italic;'>No timetable generated yet for this teacher.</p>"

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Elimu Hub | Timetable — {esc(teacher_name)}</title>
        <style>
            @page {{ size: A4 landscape; margin: 12mm; }}
            body {{ font-family: Arial, sans-serif; padding: 20px; color: #1e293b; background:#f1f5f9; }}
            @media print {{ .no-print {{ display: none !important; }} body {{ background: white; padding: 0; }} }}
            th {{ background:#f8fafc; border-bottom:2px solid #cbd5e1; font-size:10px; text-transform:uppercase; color:#64748b; }}
            .print-page {{ max-width: 267mm; margin: 0 auto; background: white; padding: 14mm; border-radius: 10px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }}
            @media print {{ .print-page {{ box-shadow: none; border-radius: 0; padding: 0; max-width: 100%; }} }}
        </style>
    </head>
    <body>
        <div class="no-print" style="text-align:right; margin-bottom:16px; max-width:267mm; margin-left:auto; margin-right:auto;">
            <button onclick="window.print()" style="background:#4f46e5;color:white;border:none;padding:10px 18px;border-radius:8px;font-weight:bold;cursor:pointer;">🖨 Print / Save as PDF</button><p style="font-size:10px;color:#94a3b8;margin:6px 0 0;">Tip: in the print dialog, choose "Save as PDF" as the destination to download a file instead of printing on paper.</p>
        </div>
        <div class="print-page">
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
                <span>{esc(school['name'] if school else '')} — Powered by Elimu Hub</span>
            </div>
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

            cur.execute("""
                SELECT ts.grade_name, ts.education_level, ts.stream, ts.day_of_week, ts.period_id,
                       COALESCE(la.name, cs.name, ca.name) AS subject_name, u.full_name AS teacher_name
                FROM timetable_slots ts
                LEFT JOIN learning_areas la ON ts.learning_area_id = la.id
                LEFT JOIN timetable_custom_subjects cs ON ts.custom_subject_id = cs.id
                LEFT JOIN co_curricular_activities ca ON ts.co_curricular_activity_id = ca.id
                LEFT JOIN users u ON ts.staff_user_id = u.id
                WHERE ts.school_id = %s;
            """, (school_id,))
            slot_map = {}
            for row in cur.fetchall():
                key = (row['grade_name'], row['education_level'], row['stream'], row['day_of_week'], row['period_id'])
                slot_map[key] = row

            # Different levels can have entirely different bell schedules
            # (e.g. Lower Primary's 35-minute lessons vs Junior School's
            # 40-minute ones) — a single merged period list would misalign
            # columns across levels, so each level gets its own table below.
            # Only true breaks are excluded here — prep and co-curricular
            # periods still show as real columns since they can carry actual
            # scheduled content worth seeing at a glance.
            periods_by_level = {
                level: [p for p in get_periods_for_level(cur, school_id, level) if (p.get('period_type') or ('teaching' if p['is_teaching_period'] else 'break')) != 'break']
                for level in EDUCATION_LEVELS
            }

    if not sections:
        body_html = "<p class='text-slate-400 text-sm italic text-center py-16'>Nothing to show yet — add students to at least one class first.</p>"
    else:
        level_tables = []
        for level in EDUCATION_LEVELS:
            level_sections = [s for s in sections if s['education_level'] == level]
            periods = periods_by_level.get(level, [])
            if not level_sections:
                continue
            if not periods:
                level_tables.append(f"""
                    <h2 style="margin:20px 0 8px;font-size:14px;font-weight:bold;color:#4f46e5;">{esc(level)}</h2>
                    <p class='text-slate-400 text-xs italic'>No periods configured yet for this level.</p>
                """)
                continue

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
            for sec in level_sections:
                row_cells = ""
                for day in days:
                    for p_i, p in enumerate(periods):
                        entry = slot_map.get((sec['grade_name'], sec['education_level'], sec['stream'], day, p['id']))
                        border = "border-left:2px solid #cbd5e1;" if p_i == 0 else ""
                        if entry and entry['subject_name']:
                            label = abbreviate_subject(entry['subject_name'])
                            teacher_title = f" title='{esc(entry['teacher_name'])}'" if entry.get('teacher_name') else ""
                            bg_color, text_color = get_subject_color(entry['subject_name'])
                            row_cells += f"<td{teacher_title} style='{border}text-align:center;font-size:10px;padding:4px 2px;border-bottom:1px solid #f1f5f9;background:{bg_color};color:{text_color};font-weight:bold;'>{esc(label)}</td>"
                        else:
                            row_cells += f"<td style='{border}background:#f8fafc;border-bottom:1px solid #f1f5f9;'></td>"
                body_rows += f"""
                <tr>
                    <td style='padding:6px 8px;font-weight:bold;background:white;position:sticky;left:0;border-right:2px solid #cbd5e1;white-space:nowrap;'>{esc(_section_label(sec['grade_name'], sec['stream']))}</td>
                    {row_cells}
                </tr>
                """

            level_tables.append(f"""
            <h2 style="margin:20px 0 8px;font-size:14px;font-weight:bold;color:#4f46e5;">{esc(level)}</h2>
            <div style="overflow-x:auto; border:1px solid #e2e8f0; border-radius:12px;">
                <table style="border-collapse:collapse; font-size:11px; min-width:100%;">
                    <thead>
                        <tr style="background:#f8fafc;"><th style="padding:6px 8px; text-align:left; position:sticky; left:0; background:#f8fafc; border-right:2px solid #cbd5e1;">Class</th>{day_header_cells}</tr>
                        <tr style="background:#f8fafc;"><th style="position:sticky; left:0; background:#f8fafc; border-right:2px solid #cbd5e1;"></th>{period_header_cells}</tr>
                    </thead>
                    <tbody>{body_rows}</tbody>
                </table>
            </div>
            """)
        body_html = "".join(level_tables) or "<p class='text-slate-400 text-sm italic text-center py-16'>Configure periods for at least one level first.</p>"

    return HTMLResponse(f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Elimu Hub | Whole School Timetable — {esc(school['name'])}</title>
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

            cur.execute("""
                SELECT ts.grade_name, ts.education_level, ts.stream, ts.day_of_week, ts.period_id,
                       COALESCE(la.name, cs.name, ca.name) AS subject_name
                FROM timetable_slots ts
                LEFT JOIN learning_areas la ON ts.learning_area_id = la.id
                LEFT JOIN timetable_custom_subjects cs ON ts.custom_subject_id = cs.id
                LEFT JOIN co_curricular_activities ca ON ts.co_curricular_activity_id = ca.id
                WHERE ts.school_id = %s;
            """, (school_id,))
            slot_map = {}
            for row in cur.fetchall():
                key = (row['grade_name'], row['education_level'], row['stream'], row['day_of_week'], row['period_id'])
                slot_map[key] = row

            periods_by_level = {
                level: [p for p in get_periods_for_level(cur, school_id, level) if (p.get('period_type') or ('teaching' if p['is_teaching_period'] else 'break')) != 'break']
                for level in EDUCATION_LEVELS
            }

    level_tables = []
    for level in EDUCATION_LEVELS:
        level_sections = [s for s in sections if s['education_level'] == level]
        periods = periods_by_level.get(level, [])
        if not level_sections or not periods:
            continue

        day_header_cells = "".join(f"<th colspan='{len(periods)}' style='text-align:center;'>{day}</th>" for day in days)
        period_header_cells = "".join("".join(f"<th style='font-weight:normal;'>{p['period_order']}</th>" for p in periods) for _ in days)

        body_rows = ""
        for sec in level_sections:
            row_cells = ""
            for day in days:
                for p in periods:
                    entry = slot_map.get((sec['grade_name'], sec['education_level'], sec['stream'], day, p['id']))
                    label = abbreviate_subject(entry['subject_name']) if (entry and entry['subject_name']) else ""
                    cell_style = "text-align:center;padding:3px;"
                    if entry and entry['subject_name']:
                        bg_color, text_color = get_subject_color(entry['subject_name'])
                        cell_style += f"background:{bg_color};color:{text_color};font-weight:bold;"
                    row_cells += f"<td style='{cell_style}'>{esc(label)}</td>"
            body_rows += f"<tr><td style='font-weight:bold;padding:4px 6px;white-space:nowrap;'>{esc(_section_label(sec['grade_name'], sec['stream']))}</td>{row_cells}</tr>"

        level_tables.append(f"""
        <h2 style="margin:16px 0 4px;font-size:13px;">{esc(level)}</h2>
        <table>
            <thead>
                <tr><th>Class</th>{day_header_cells}</tr>
                <tr><th></th>{period_header_cells}</tr>
            </thead>
            <tbody>{body_rows}</tbody>
        </table>
        """)
    body_html = "".join(level_tables) or "<p style='color:#94a3b8;font-style:italic;'>Nothing to show yet.</p>"

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Elimu Hub | Whole School Timetable — {esc(school['name'])}</title>
        <style>
            @page {{ size: A4 landscape; margin: 10mm; }}
            body {{ font-family: Arial, sans-serif; padding: 12px; color: #1e293b; background:#f1f5f9; }}
            @media print {{ .no-print {{ display: none !important; }} body {{ background: white; padding: 0; }} }}
            table {{ width: 100%; border-collapse: collapse; margin-top: 12px; font-size: 9px; }}
            th, td {{ border: 1px solid #cbd5e1; }}
            th {{ background:#f8fafc; }}
            .print-page {{ max-width: 267mm; margin: 0 auto; background: white; padding: 12mm; border-radius: 10px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); overflow-x:auto; }}
            @media print {{ .print-page {{ box-shadow: none; border-radius: 0; padding: 0; max-width: 100%; }} }}
        </style>
    </head>
    <body>
        <div class="no-print" style="text-align:right; margin-bottom:12px; max-width:267mm; margin-left:auto; margin-right:auto;">
            <button onclick="window.print()" style="background:#4f46e5;color:white;border:none;padding:10px 18px;border-radius:8px;font-weight:bold;cursor:pointer;">🖨 Print / Save as PDF</button><p style="font-size:10px;color:#94a3b8;margin:6px 0 0;">Tip: in the print dialog, choose "Save as PDF" as the destination to download a file instead of printing on paper.</p>
        </div>
        <div class="print-page">
            <h1 style="margin:0;font-size:16px;">{esc(school['name'])} — Whole School Timetable</h1>
            {body_html}
        </div>
    </body>
    </html>
    """