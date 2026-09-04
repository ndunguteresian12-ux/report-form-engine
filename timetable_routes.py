"""
timetable_routes.py — Elimu Hub's automatic timetabling engine (v2)

A full rewrite of the timetabling module around an ASC Timetables-inspired
workspace: a central hub with quick entity launchers, a single solid
generation engine (test-then-generate, no complexity/strictness dials by
design choice), a Whole/Teachers/Subjects view switcher, and an expanded
print/report suite.

Deliberately scoped OUT, by explicit decision: a Classrooms/Facilities
entity (not relevant — each class uses its own fixed room) and
generation complexity/strictness modes (one solid mode is enough).

Deliberately KEPT from the previous version, unchanged in behavior: the
hardened generation algorithm (Phase 1 locked placements, Phase 2 double
lessons, Phase 3 fill-remaining with conflict-avoidance-first candidate
selection), teacher/subject availability, the collision checker, and the
teacher workload report — these were fixed and verified working earlier
in this same project; a fresh UI doesn't require re-deriving proven
scheduling logic from zero.

Classes, teachers, and students are never re-entered here — they're
fetched live from the same `students`, `classes`, and `users` tables the
rest of Elimu Hub already uses, exactly as requested: manually configure
periods and subjects, fetch everything else.
"""

import urllib.parse
import json
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
    get_current_session_user,
)

router = APIRouter()

EDUCATION_LEVELS = ["ECDE", "Lower Primary", "Upper Primary", "Junior School"]


def _parse_time_to_minutes_shared(time_str):
    """Module-level version of the time-parsing helper duplicated locally
    in a few places in this file (generate_draft_timetable,
    _find_timetable_collisions) — added here specifically for
    update_timetable_slot's conflict check, which previously compared
    exact period_id only. That missed a genuine cross-education-level
    collision: two different levels' periods are separate rows with
    different ids even when their real clock times are identical, so an
    exact-id match let a teacher be manually double-booked across two
    levels whose periods happened to overlap. Given the existing local
    copies are already tested and working, this is added as a new,
    separate function rather than risk refactoring them."""
    if not time_str:
        return None
    cleaned = time_str.strip().upper().replace(".", "")
    is_pm = "PM" in cleaned
    is_am = "AM" in cleaned
    cleaned = cleaned.replace("AM", "").replace("PM", "").strip()
    parts = cleaned.replace(".", ":").split(":")
    try:
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
    except (ValueError, IndexError):
        return None
    if is_pm and hour < 12:
        hour += 12
    if is_am and hour == 12:
        hour = 0
    return hour * 60 + minute


def _time_ranges_overlap_shared(a_start, a_end, b_start, b_end):
    if a_start is None or a_end is None or b_start is None or b_end is None:
        return False
    return a_start < b_end and b_start < a_end


# Custom subjects (Music/Art/PE splits, PPI, etc.) share the scheduling
# engine, teacher assignments, and time-off logic with regular subjects by
# using this large offset on their id — guarantees zero collision with a
# real learning_area id (a school will never have a million subjects), so
# every dict/set keyed by subject id works unchanged for both. Defined
# once here rather than locally in each function that needs it, so it
# can never drift out of sync between them.
CUSTOM_SUBJECT_ID_OFFSET = 1_000_000
ALL_POSSIBLE_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]

# A small, pleasant default palette assigned round-robin to subjects that
# haven't been given an explicit color yet — matches ASC's "each subject
# gets a color for at-a-glance reading" idea without requiring every
# school to configure colors before their first timetable looks right.
DEFAULT_SUBJECT_COLORS = [
    "#6366f1", "#0d9488", "#d97706", "#db2777", "#7c3aed",
    "#059669", "#dc2626", "#0891b2", "#ca8a04", "#4f46e5",
    "#be123c", "#15803d",
]

# The proven (background, text) hex-pair palette used for cell shading
# across every timetable print/view page — a stable hash of the subject
# name always lands on the same pair, so a subject is visually consistent
# across every report without needing to be pre-configured.
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


def _widen_unique_constraint(cur, table_name, old_columns, new_columns):
    """Finds whatever unique constraint currently covers exactly
    old_columns on table_name — regardless of its auto-generated name,
    which Postgres decides and isn't safe to guess — and replaces it with
    one covering new_columns instead (adding plan_id). Wrapped by the
    caller in a try/except: this touches constraints on live tables, and
    a startup migration must never be able to crash the whole app if
    something about a specific school's data doesn't match what this
    expects.

    kcu.column_name is Postgres's internal sql_identifier type, not plain
    text — comparing array_agg(...) directly against a Python list (which
    psycopg2 sends as a text[] array) fails with an operator-does-not-
    exist error unless each element is explicitly cast to text first.
    Caught the hard way: this silently failed on every single call until
    tested against a real Postgres instance rather than trusted by
    inspection."""
    cur.execute("""
        SELECT tc.constraint_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu ON tc.constraint_name = kcu.constraint_name AND tc.table_name = kcu.table_name
        WHERE tc.table_name = %s AND tc.constraint_type = 'UNIQUE'
        GROUP BY tc.constraint_name
        HAVING array_agg(kcu.column_name::text ORDER BY kcu.column_name::text) = %s;
    """, (table_name, sorted(old_columns)))
    row = cur.fetchone()
    if row:
        cur.execute(f'ALTER TABLE {table_name} DROP CONSTRAINT "{row[0]}";')

    # Only add the new constraint if nothing already covers exactly
    # new_columns — makes this safe to re-run on every future deploy.
    cur.execute("""
        SELECT tc.constraint_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu ON tc.constraint_name = kcu.constraint_name AND tc.table_name = kcu.table_name
        WHERE tc.table_name = %s AND tc.constraint_type = 'UNIQUE'
        GROUP BY tc.constraint_name
        HAVING array_agg(kcu.column_name::text ORDER BY kcu.column_name::text) = %s;
    """, (table_name, sorted(new_columns)))
    if not cur.fetchone():
        cur.execute(f"ALTER TABLE {table_name} ADD CONSTRAINT {table_name}_{'_'.join(new_columns)}_key UNIQUE ({', '.join(new_columns)});")


def bootstrap_timetable_schema():
    """Creates/upgrades every table this module owns. Purely additive —
    CREATE TABLE IF NOT EXISTS and ADD COLUMN IF NOT EXISTS throughout, so
    this is safe to run against a fresh install or one with years of live
    school data already in it."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            # --- Timetable Plans: lets a school hold several independent,
            # fully separate timetables per education level (a normal
            # weekday one, plus e.g. a weekend program) — each with its own
            # periods, days, teacher assignments, subject rules, and
            # generated slots, none of which affect any other plan. Exactly
            # one plan per education level is "active" (is_active = TRUE):
            # that's the one every existing report, print view, and staff
            # dashboard shows, so building out a second plan never touches
            # what's currently live until an admin explicitly switches which
            # one is active.
            #
            # active_days is a simple comma-separated list of real day
            # names (any of the 7, in any combination/order) — replacing
            # the old days_per_week count, which could only ever express
            # "the first N days starting from Monday" and could never
            # represent something like a Saturday+Sunday-only plan.
            cur.execute("""
                CREATE TABLE IF NOT EXISTS timetable_plans (
                    id SERIAL PRIMARY KEY,
                    school_id INTEGER REFERENCES schools(id) ON DELETE CASCADE,
                    education_level VARCHAR(100) NOT NULL,
                    name VARCHAR(150) NOT NULL,
                    active_days VARCHAR(200) NOT NULL DEFAULT 'Monday,Tuesday,Wednesday,Thursday,Friday',
                    is_active BOOLEAN NOT NULL DEFAULT FALSE,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_timetable_plans_lookup ON timetable_plans (school_id, education_level);
            """)
            conn.commit()

            cur.execute("""
                CREATE TABLE IF NOT EXISTS timetable_generation_issues (
                    id SERIAL PRIMARY KEY,
                    school_id INTEGER REFERENCES schools(id) ON DELETE CASCADE,
                    grade_name VARCHAR(100) NOT NULL,
                    education_level VARCHAR(100) NOT NULL,
                    stream VARCHAR(100) NOT NULL,
                    issues_json TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT NOW(),
                    UNIQUE(school_id, grade_name, education_level, stream)
                );
            """)
            conn.commit()

            cur.execute("""
                CREATE TABLE IF NOT EXISTS timetable_settings (
                    school_id INTEGER PRIMARY KEY REFERENCES schools(id) ON DELETE CASCADE,
                    days_per_week INTEGER NOT NULL DEFAULT 5
                );

                CREATE TABLE IF NOT EXISTS timetable_periods (
                    id SERIAL PRIMARY KEY,
                    school_id INTEGER REFERENCES schools(id) ON DELETE CASCADE,
                    education_level VARCHAR(100) NOT NULL,
                    label VARCHAR(50) NOT NULL,
                    short_label VARCHAR(20),
                    start_time VARCHAR(20),
                    end_time VARCHAR(20),
                    period_order INTEGER NOT NULL,
                    period_type VARCHAR(20) NOT NULL DEFAULT 'teaching',
                    is_teaching_period BOOLEAN NOT NULL DEFAULT TRUE,
                    UNIQUE(school_id, education_level, period_order)
                );

                CREATE TABLE IF NOT EXISTS teacher_subject_assignments (
                    id SERIAL PRIMARY KEY,
                    school_id INTEGER REFERENCES schools(id) ON DELETE CASCADE,
                    grade_name VARCHAR(100) NOT NULL,
                    education_level VARCHAR(100) NOT NULL,
                    stream VARCHAR(100) NOT NULL,
                    learning_area_id INTEGER REFERENCES learning_areas(id) ON DELETE CASCADE,
                    staff_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    lessons_per_week INTEGER NOT NULL DEFAULT 1,
                    requires_double BOOLEAN NOT NULL DEFAULT FALSE,
                    UNIQUE(school_id, grade_name, education_level, stream, learning_area_id)
                );

                ALTER TABLE teacher_subject_assignments ADD COLUMN IF NOT EXISTS double_lessons_count INTEGER NOT NULL DEFAULT 0;

                -- One-time, safe-to-re-run backfill: a school that already
                -- had "requires double" checked before this feature existed
                -- gets exactly one double (double_lessons_count = 1) —
                -- exactly matching what "requires_double = TRUE" already
                -- meant in the generator, so no existing live school's
                -- timetable behavior changes just from this migration. The
                -- WHERE guard makes this a no-op on any row an admin has
                -- already set a real double count on, so it's safe even if
                -- this runs again on every future deploy. Covers both
                -- regular subjects (learning_area_id) and custom subjects
                -- (custom_subject_id) since both live in this one table.
                UPDATE teacher_subject_assignments
                SET double_lessons_count = 1
                WHERE requires_double = TRUE AND double_lessons_count = 0;

                CREATE TABLE IF NOT EXISTS timetable_slots (
                    id SERIAL PRIMARY KEY,
                    school_id INTEGER REFERENCES schools(id) ON DELETE CASCADE,
                    grade_name VARCHAR(100) NOT NULL,
                    education_level VARCHAR(100) NOT NULL,
                    stream VARCHAR(100) NOT NULL,
                    day_of_week VARCHAR(20) NOT NULL,
                    period_id INTEGER REFERENCES timetable_periods(id) ON DELETE CASCADE,
                    learning_area_id INTEGER REFERENCES learning_areas(id) ON DELETE CASCADE,
                    custom_subject_id INTEGER,
                    co_curricular_activity_id INTEGER,
                    staff_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL
                );
                CREATE INDEX IF NOT EXISTS idx_timetable_slots_lookup ON timetable_slots (school_id, grade_name, education_level, stream);
                CREATE INDEX IF NOT EXISTS idx_timetable_slots_teacher ON timetable_slots (school_id, staff_user_id, day_of_week, period_id);

                CREATE TABLE IF NOT EXISTS teacher_availability (
                    id SERIAL PRIMARY KEY,
                    school_id INTEGER REFERENCES schools(id) ON DELETE CASCADE,
                    staff_user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                    day_of_week VARCHAR(20) NOT NULL,
                    period_id INTEGER REFERENCES timetable_periods(id) ON DELETE CASCADE,
                    status VARCHAR(20) NOT NULL DEFAULT 'not_available',
                    UNIQUE(school_id, staff_user_id, day_of_week, period_id)
                );

                CREATE TABLE IF NOT EXISTS subject_availability (
                    id SERIAL PRIMARY KEY,
                    school_id INTEGER REFERENCES schools(id) ON DELETE CASCADE,
                    learning_area_id INTEGER REFERENCES learning_areas(id) ON DELETE CASCADE,
                    day_of_week VARCHAR(20) NOT NULL,
                    period_id INTEGER REFERENCES timetable_periods(id) ON DELETE CASCADE,
                    status VARCHAR(20) NOT NULL DEFAULT 'not_available',
                    UNIQUE(school_id, learning_area_id, day_of_week, period_id)
                );

                CREATE TABLE IF NOT EXISTS subject_sync_rules (
                    id SERIAL PRIMARY KEY,
                    school_id INTEGER REFERENCES schools(id) ON DELETE CASCADE,
                    learning_area_id INTEGER REFERENCES learning_areas(id) ON DELETE CASCADE,
                    day_of_week VARCHAR(20) NOT NULL,
                    period_id INTEGER REFERENCES timetable_periods(id) ON DELETE CASCADE,
                    UNIQUE(school_id, learning_area_id)
                );

                CREATE TABLE IF NOT EXISTS subject_constraints (
                    id SERIAL PRIMARY KEY,
                    school_id INTEGER REFERENCES schools(id) ON DELETE CASCADE,
                    grade_name VARCHAR(100) NOT NULL,
                    education_level VARCHAR(100) NOT NULL,
                    stream VARCHAR(100) NOT NULL,
                    subject_a_id INTEGER REFERENCES learning_areas(id) ON DELETE CASCADE,
                    subject_b_id INTEGER REFERENCES learning_areas(id) ON DELETE CASCADE,
                    constraint_type VARCHAR(30) NOT NULL
                );

                CREATE TABLE IF NOT EXISTS timetable_custom_subjects (
                    id SERIAL PRIMARY KEY,
                    school_id INTEGER REFERENCES schools(id) ON DELETE CASCADE,
                    education_level VARCHAR(100) NOT NULL,
                    name VARCHAR(150) NOT NULL
                );

                CREATE TABLE IF NOT EXISTS co_curricular_activities (
                    id SERIAL PRIMARY KEY,
                    school_id INTEGER REFERENCES schools(id) ON DELETE CASCADE,
                    name VARCHAR(150) NOT NULL,
                    category VARCHAR(50),
                    schedule_note VARCHAR(255),
                    staff_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    description TEXT
                );

                CREATE TABLE IF NOT EXISTS co_curricular_participants (
                    id SERIAL PRIMARY KEY,
                    activity_id INTEGER REFERENCES co_curricular_activities(id) ON DELETE CASCADE,
                    student_id INTEGER REFERENCES students(id) ON DELETE CASCADE,
                    UNIQUE(activity_id, student_id)
                );
            """)

            # Lets a teacher+lessons/week be assigned to a CUSTOM subject
            # (e.g. a school-specific split of "Creative Arts and Sports"
            # into Music/Art/PE, or a non-examinable subject like PPI) —
            # exactly the same as a normal learning_area assignment, so the
            # auto-generator schedules these with full conflict-avoidance
            # too, not just via manual placement.
            cur.execute("ALTER TABLE teacher_subject_assignments ADD COLUMN IF NOT EXISTS custom_subject_id INTEGER REFERENCES timetable_custom_subjects(id) ON DELETE CASCADE;")
            cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_tsa_custom_subject ON teacher_subject_assignments (school_id, grade_name, education_level, stream, custom_subject_id) WHERE custom_subject_id IS NOT NULL;")
            conn.commit()

            # Lets a custom subject (Music/Art/PE split, PPI, etc.) also have
            # time-off marked, exactly like a regular subject.
            cur.execute("ALTER TABLE subject_availability ADD COLUMN IF NOT EXISTS custom_subject_id INTEGER REFERENCES timetable_custom_subjects(id) ON DELETE CASCADE;")
            cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_sa_custom_subject ON subject_availability (school_id, custom_subject_id, day_of_week, period_id) WHERE custom_subject_id IS NOT NULL;")
            conn.commit()

            # NEW: subject short-codes and colors, first-class in this
            # module rather than a hash-derived color — matches ASC's
            # "each subject gets a defined code and color" entity concept.
            # A separate table, not new columns on the shared
            # `learning_areas` table, since that table is used across the
            # whole app (report cards, marks entry) and shouldn't carry
            # timetable-only presentation fields.
            cur.execute("""
                CREATE TABLE IF NOT EXISTS timetable_subject_config (
                    id SERIAL PRIMARY KEY,
                    school_id INTEGER REFERENCES schools(id) ON DELETE CASCADE,
                    learning_area_id INTEGER REFERENCES learning_areas(id) ON DELETE CASCADE,
                    short_code VARCHAR(10),
                    color_hex VARCHAR(9),
                    UNIQUE(school_id, learning_area_id)
                );
            """)
            conn.commit()

            # "Linked classes" — e.g. Mathematics for Grade 8V and Grade 8J
            # should always land at the same day/period as each other, so
            # if one teacher is absent the other can combine both classes
            # into one room. Matches ASC Timetables' parallel/linked class
            # concept. Deliberately a separate table from subject_sync_rules
            # (which locks a subject to one fixed time SCHOOL-WIDE) — this
            # is a pairing between two SPECIFIC classes, and the two
            # classes named can be in different grades and even different
            # education levels (their own separate bell schedules), which
            # is exactly Francis's real example: Grade 9V and Grade 8J.
            cur.execute("""
                CREATE TABLE IF NOT EXISTS class_link_rules (
                    id SERIAL PRIMARY KEY,
                    school_id INTEGER REFERENCES schools(id) ON DELETE CASCADE,
                    learning_area_id INTEGER REFERENCES learning_areas(id) ON DELETE CASCADE,
                    custom_subject_id INTEGER REFERENCES timetable_custom_subjects(id) ON DELETE CASCADE,
                    class_a_grade_name VARCHAR(100) NOT NULL,
                    class_a_education_level VARCHAR(100) NOT NULL,
                    class_a_stream VARCHAR(100) NOT NULL,
                    class_b_grade_name VARCHAR(100) NOT NULL,
                    class_b_education_level VARCHAR(100) NOT NULL,
                    class_b_stream VARCHAR(100) NOT NULL,
                    created_at TIMESTAMP DEFAULT NOW()
                );
            """)
            conn.commit()


            # ================================================================
            # Multi-plan migration: give every table that makes up a
            # timetable its own plan_id, so a school can hold several fully
            # independent timetables per education level (their own periods,
            # days, teacher assignments, and every rule) without one
            # touching another. This is purely additive and defensively
            # wrapped — each step is safe to re-run on every future deploy,
            # and a problem with any single step must never crash the whole
            # app's startup.
            # ================================================================

            for tbl in [
                "timetable_periods", "teacher_subject_assignments", "timetable_slots",
                "teacher_availability", "subject_availability", "subject_sync_rules",
                "subject_constraints", "timetable_custom_subjects", "class_link_rules",
                "timetable_generation_issues",
            ]:
                try:
                    cur.execute(f"ALTER TABLE {tbl} ADD COLUMN IF NOT EXISTS plan_id INTEGER REFERENCES timetable_plans(id) ON DELETE CASCADE;")
                    conn.commit()
                except Exception as e:
                    conn.rollback()
                    print(f"[timetable multi-plan migration] Could not add plan_id to {tbl}: {e}")

            # Marks a slot as pre-placed by the level-wide shared-teacher
            # doubles pass (see _preplace_shared_teacher_doubles), which
            # runs BEFORE any single class's own generation — resolving a
            # teacher shared across multiple classes' double-lesson needs
            # jointly, rather than leaving it to whichever class happens
            # to generate first. generate_draft_timetable's own DELETE and
            # setup steps are made aware of this flag specifically so a
            # per-class regeneration preserves these rows instead of
            # wiping them out and re-discovering the same cross-class
            # conflict from scratch.
            try:
                cur.execute("ALTER TABLE timetable_slots ADD COLUMN IF NOT EXISTS is_preplaced BOOLEAN NOT NULL DEFAULT FALSE;")
                conn.commit()
            except Exception as e:
                conn.rollback()
                print(f"[timetable multi-plan migration] Could not add is_preplaced to timetable_slots: {e}")

            # Widen every unique constraint that predates plan_id — without
            # this, a second plan trying to insert e.g. a teaching
            # assignment for the same class+subject as the first plan would
            # be rejected outright as a duplicate, even though the two rows
            # now genuinely belong to two separate plans.
            for tbl, old_cols, new_cols in [
                ("timetable_periods", ["school_id", "education_level", "period_order"], ["school_id", "education_level", "period_order", "plan_id"]),
                ("teacher_subject_assignments", ["school_id", "grade_name", "education_level", "stream", "learning_area_id"], ["school_id", "grade_name", "education_level", "stream", "learning_area_id", "plan_id"]),
                ("teacher_availability", ["school_id", "staff_user_id", "day_of_week", "period_id"], ["school_id", "staff_user_id", "day_of_week", "period_id", "plan_id"]),
                ("subject_availability", ["school_id", "learning_area_id", "day_of_week", "period_id"], ["school_id", "learning_area_id", "day_of_week", "period_id", "plan_id"]),
                ("subject_sync_rules", ["school_id", "learning_area_id"], ["school_id", "learning_area_id", "plan_id"]),
                ("timetable_generation_issues", ["school_id", "grade_name", "education_level", "stream"], ["school_id", "grade_name", "education_level", "stream", "plan_id"]),
                # This one wasn't in the original table catalog used to
                # plan this migration — it turned out to have a real,
                # already-deployed UNIQUE(school_id, education_level, name)
                # constraint on the live database that didn't show up
                # anywhere in this file's own CREATE TABLE statement,
                # discovered the hard way via a live "Save As" duplicate-
                # key crash. _widen_unique_constraint finds a constraint by
                # its actual columns, not by name or origin, so this
                # correctly finds and widens it regardless of how or when
                # it was originally created.
                ("timetable_custom_subjects", ["school_id", "education_level", "name"], ["school_id", "education_level", "name", "plan_id"]),
            ]:
                try:
                    _widen_unique_constraint(cur, tbl, old_cols, new_cols)
                    conn.commit()
                except Exception as e:
                    conn.rollback()
                    print(f"[timetable multi-plan migration] Could not widen unique constraint on {tbl}: {e}")

            # The two PARTIAL unique indexes (custom-subject variants) are
            # plain CREATE INDEX statements, not named table constraints, so
            # they're simpler to widen directly: drop the old one, create a
            # new one covering the same columns plus plan_id.
            for old_index_name, tbl, new_index_name, cols_expr in [
                ("idx_tsa_custom_subject", "teacher_subject_assignments", "idx_tsa_custom_subject_plan",
                 "school_id, grade_name, education_level, stream, custom_subject_id, plan_id"),
                ("idx_sa_custom_subject", "subject_availability", "idx_sa_custom_subject_plan",
                 "school_id, custom_subject_id, day_of_week, period_id, plan_id"),
            ]:
                try:
                    cur.execute(f"DROP INDEX IF EXISTS {old_index_name};")
                    cur.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS {new_index_name} ON {tbl} ({cols_expr}) WHERE custom_subject_id IS NOT NULL;")
                    conn.commit()
                except Exception as e:
                    conn.rollback()
                    print(f"[timetable multi-plan migration] Could not widen partial index on {tbl}: {e}")

            # One-time backfill: any (school_id, education_level) that
            # already has real timetable data but no plan yet gets a single
            # "Main Timetable" plan, marked active immediately — so every
            # existing report, print view, and staff dashboard keeps
            # showing exactly what it already showed, with zero visible
            # change for any live school. active_days is carried over from
            # that school's existing (school-wide) days_per_week setting,
            # so a school that had a 6-day week configured keeps it.
            # WHERE plan_id IS NULL makes every step here safe to re-run —
            # once a school/level is migrated, this never touches it again.
            #
            # Split into two passes because not every table has its own
            # education_level column: teacher_availability, subject_
            # availability, and subject_sync_rules are tied to a specific
            # period_id instead, which itself belongs to one specific
            # education level's bell schedule — so Pass 1 must migrate
            # timetable_periods (and every table with its own
            # education_level) first, and Pass 2 then derives the right
            # plan for the period-based tables by following period_id to
            # the now-migrated timetable_periods.plan_id. A few rows in
            # subject_sync_rules can have no period_id set at all (a rule
            # not yet given a fixed slot) — those fall back to deriving
            # their level from their subject's own learning_areas.
            # education_level instead.
            try:
                cur.execute("""
                    SELECT DISTINCT school_id, education_level FROM (
                        SELECT school_id, education_level FROM teacher_subject_assignments WHERE plan_id IS NULL
                        UNION
                        SELECT school_id, education_level FROM timetable_periods WHERE plan_id IS NULL
                        UNION
                        SELECT school_id, education_level FROM timetable_slots WHERE plan_id IS NULL
                    ) AS needing_migration;
                """)
                to_migrate = cur.fetchall()

                for row in to_migrate:
                    school_id_m, education_level_m = row[0], row[1]

                    cur.execute("SELECT days_per_week FROM timetable_settings WHERE school_id = %s;", (school_id_m,))
                    settings_row = cur.fetchone()
                    days_per_week_m = (settings_row[0] if settings_row else 5) or 5
                    all_day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
                    active_days_m = ",".join(all_day_names[:max(1, min(6, days_per_week_m))])

                    cur.execute("""
                        INSERT INTO timetable_plans (school_id, education_level, name, active_days, is_active)
                        VALUES (%s, %s, 'Main Timetable', %s, TRUE)
                        RETURNING id;
                    """, (school_id_m, education_level_m, active_days_m))
                    new_plan_id = cur.fetchone()[0]

                    # Pass 1: tables with their own direct education_level column.
                    for tbl in ["timetable_periods", "teacher_subject_assignments", "timetable_slots",
                                "subject_constraints", "timetable_custom_subjects", "timetable_generation_issues"]:
                        cur.execute(f"UPDATE {tbl} SET plan_id = %s WHERE school_id = %s AND education_level = %s AND plan_id IS NULL;", (new_plan_id, school_id_m, education_level_m))
                    conn.commit()

                # Pass 2: tables keyed by period_id instead of education_level
                # directly — derive their plan by following period_id to the
                # now-migrated timetable_periods.plan_id.
                for tbl in ["teacher_availability", "subject_availability", "subject_sync_rules"]:
                    cur.execute(f"""
                        UPDATE {tbl} AS t
                        SET plan_id = tp.plan_id
                        FROM timetable_periods tp
                        WHERE t.period_id = tp.id AND t.plan_id IS NULL AND tp.plan_id IS NOT NULL;
                    """)
                conn.commit()

                # Fallback for subject_availability/subject_sync_rules rows
                # with no period_id set yet (a rule not yet given a fixed
                # slot) — derive the level from the subject's own
                # learning_areas.education_level instead. Uses a comma-join
                # (implicit cross join) rather than an explicit JOIN...ON,
                # since Postgres doesn't allow the UPDATE target table (t)
                # to be referenced inside a JOIN...ON clause within an
                # UPDATE...FROM — every correlation has to move to WHERE
                # instead, which a comma-join allows.
                for tbl in ["subject_availability", "subject_sync_rules"]:
                    cur.execute(f"""
                        UPDATE {tbl} AS t
                        SET plan_id = tplan.id
                        FROM learning_areas la, timetable_plans tplan
                        WHERE t.learning_area_id = la.id
                          AND tplan.school_id = t.school_id
                          AND tplan.education_level = la.education_level
                          AND tplan.is_active = TRUE
                          AND t.plan_id IS NULL;
                    """)
                conn.commit()

                # class_link_rules spans two classes that can be in two
                # DIFFERENT education levels (Francis's real example: Grade
                # 9V linked with Grade 8J) — there's no single level a link
                # rule cleanly belongs to. Backfilling against class A's
                # own level is a reasonable, documented choice here, not a
                # perfect one; a link between two levels that were migrated
                # at different times could still need a manual re-check.
                cur.execute("""
                    UPDATE class_link_rules AS clr
                    SET plan_id = tplan.id
                    FROM timetable_plans tplan
                    WHERE tplan.school_id = clr.school_id AND tplan.education_level = clr.class_a_education_level
                      AND tplan.is_active = TRUE AND clr.plan_id IS NULL;
                """)
                conn.commit()
            except Exception as e:
                conn.rollback()
                print(f"[timetable multi-plan migration] Backfill failed: {e}")


def resolve_plan_id(cur, school_id: int, education_level: str, plan_id_param: int = None) -> int:
    """The single source of truth for "which plan are we actually working
    on" across every plan-aware route. If plan_id_param is given (the
    admin explicitly selected a plan on the page), it's used after
    confirming it genuinely belongs to this school+level. Otherwise
    resolves to whichever plan is currently marked active — the same
    thing every route did implicitly before plans existed, so any link
    or bookmark with no plan_id at all keeps working exactly as before.
    If somehow no plan exists yet for this school+level (shouldn't
    happen after the migration, but this is a genuine safety net, not a
    decoration), one is created on the fly."""
    if plan_id_param:
        cur.execute("SELECT id FROM timetable_plans WHERE id = %s AND school_id = %s AND education_level = %s;", (plan_id_param, school_id, education_level))
        row = cur.fetchone()
        if row:
            return row[0] if not isinstance(row, dict) else row['id']

    cur.execute("SELECT id FROM timetable_plans WHERE school_id = %s AND education_level = %s AND is_active = TRUE LIMIT 1;", (school_id, education_level))
    row = cur.fetchone()
    if row:
        return row[0] if not isinstance(row, dict) else row['id']

    cur.execute("""
        INSERT INTO timetable_plans (school_id, education_level, name, active_days, is_active)
        VALUES (%s, %s, 'Main Timetable', 'Monday,Tuesday,Wednesday,Thursday,Friday', TRUE) RETURNING id;
    """, (school_id, education_level))
    new_row = cur.fetchone()
    return new_row[0] if not isinstance(new_row, dict) else new_row['id']


def get_plan_options_html(cur, school_id: int, education_level: str, current_plan_id: int) -> str:
    """The plan-switcher dropdown shown at the top of every plan-aware
    page — lets an admin actually pick a specific (possibly non-active)
    plan to work on, which is the whole point of plans existing."""
    cur.execute("SELECT id, name, is_active FROM timetable_plans WHERE school_id = %s AND education_level = %s ORDER BY is_active DESC, created_at ASC;", (school_id, education_level))
    plans = cur.fetchall()
    options = "".join(
        f"<option value='{p['id']}' {'selected' if p['id'] == current_plan_id else ''}>{esc(p['name'])}{' (Active)' if p['is_active'] else ' (Draft)'}</option>"
        for p in plans
    )
    return options


def get_school_days(cur, school_id: int, plan_id: int = None):
    """Returns the days in order for a specific plan, e.g.
    ['Monday', ..., 'Friday'] — or ['Saturday', 'Sunday'] for a custom
    weekend plan. When plan_id is given, reads that plan's own
    active_days — the real per-plan custom day list. Callers that don't
    pass plan_id (not yet updated to be plan-aware) fall back to the old
    school-wide days_per_week setting, exactly as before — so nothing
    breaks for a route this pass didn't get to yet, it just doesn't see
    per-plan custom days until it's updated too."""
    if plan_id:
        cur.execute("SELECT active_days FROM timetable_plans WHERE id = %s;", (plan_id,))
        row = cur.fetchone()
        if row:
            active_days_str = row['active_days'] if isinstance(row, dict) else row[0]
            if active_days_str:
                return [d.strip() for d in active_days_str.split(",") if d.strip()]

    try:
        cur.execute("SELECT days_per_week FROM timetable_settings WHERE school_id = %s;", (school_id,))
        row = cur.fetchone()
        days_per_week = (row['days_per_week'] if isinstance(row, dict) else row[0]) if row else 5
    except Exception:
        days_per_week = 5
    all_days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
    return all_days[:max(1, min(6, days_per_week))]


def get_periods_for_level(cur, school_id: int, education_level: str, plan_id: int = None):
    if plan_id:
        cur.execute("""
            SELECT * FROM timetable_periods
            WHERE school_id = %s AND education_level = %s AND plan_id = %s
            ORDER BY period_order ASC;
        """, (school_id, education_level, plan_id))
        return cur.fetchall()  # correctly empty for a genuinely new plan with no periods yet — never falls back to another plan's data
    # Only reached when no plan_id was given at all (a caller not yet
    # updated to be plan-aware) — the old, plan-agnostic query.
    cur.execute("""
        SELECT * FROM timetable_periods
        WHERE school_id = %s AND education_level = %s
        ORDER BY period_order ASC;
    """, (school_id, education_level))
    return cur.fetchall()


def _section_label(grade_name: str, stream: str) -> str:
    return grade_name if (not stream or stream == "SINGLE STREAM") else f"{grade_name} — {stream}"


def get_subject_style(cur, school_id: int, learning_area_id: int, subject_name: str):
    """Returns (short_code, color_hex) for a subject — its configured
    values if set, otherwise a sensible auto-generated fallback (an
    abbreviation, and a deterministic color from the default palette so
    the same subject always gets the same color even before anyone
    configures one explicitly)."""
    cur.execute("""
        SELECT short_code, color_hex FROM timetable_subject_config
        WHERE school_id = %s AND learning_area_id = %s;
    """, (school_id, learning_area_id))
    row = cur.fetchone()
    short_code = (row['short_code'] if row else None) or abbreviate_subject(subject_name)
    color_hex = (row['color_hex'] if row else None) or DEFAULT_SUBJECT_COLORS[learning_area_id % len(DEFAULT_SUBJECT_COLORS)]
    return short_code, color_hex


# ============================================================
# Workspace Hub — the new central navigation, ASC-style: quick
# launchers to every entity panel, plus Test & Generate front and
# center rather than buried inside each class.
# ============================================================

@router.get("/timetable/dashboard/{school_id}", response_class=HTMLResponse)
def timetable_workspace_hub(school_id: int, request: Request):
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
                SELECT DISTINCT c.grade_name, c.education_level, s.stream
                FROM students s
                JOIN classes c ON s.class_id = c.id
                WHERE s.school_id = %s AND (s.status IS NULL OR s.status != 'GRADUATED')
                ORDER BY c.grade_name ASC, s.stream ASC;
            """, (school_id,))
            sections = cur.fetchall()

            cur.execute("SELECT COUNT(*) AS cnt FROM timetable_periods WHERE school_id = %s;", (school_id,))
            has_periods = cur.fetchone()['cnt'] > 0

            cur.execute("""
                SELECT grade_name, education_level, stream, COUNT(*) AS cnt
                FROM timetable_slots WHERE school_id = %s
                GROUP BY grade_name, education_level, stream;
            """, (school_id,))
            slot_counts = {(r['grade_name'], r['education_level'], r['stream']): r['cnt'] for r in cur.fetchall()}

            cur.execute("SELECT COUNT(*) AS cnt FROM learning_areas;")
            subject_count = cur.fetchone()['cnt']

            cur.execute("SELECT COUNT(DISTINCT staff_user_id) AS cnt FROM teacher_subject_assignments WHERE school_id = %s AND staff_user_id IS NOT NULL;", (school_id,))
            teacher_count = cur.fetchone()['cnt']

    sections_by_level = {}
    for sec in sections:
        sections_by_level.setdefault(sec['education_level'], []).append(sec)

    level_accent = {"ECDE": "#db2777", "Lower Primary": "#0d9488", "Upper Primary": "#0891b2", "Junior School": "#7c3aed"}

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
                "<span class='text-[10px] font-bold px-2 py-0.5 rounded-full bg-teal-50 text-teal-700 border border-teal-200'>Set</span>"
                if has_timetable else
                "<span class='text-[10px] font-bold px-2 py-0.5 rounded-full bg-amber-50 text-amber-700 border border-amber-200'>Not set</span>"
            )
            cards_html += f"""
            <div class='bg-white border border-slate-200/80 p-4 rounded-2xl shadow-xs hover:shadow-md transition-shadow flex flex-col justify-between gap-2.5 border-l-4' style='border-left-color:{accent};'>
                <div class="flex items-center justify-between">
                    <h3 class='text-sm font-black text-slate-800'>{esc(_section_label(sec['grade_name'], sec['stream']))}</h3>
                    {status_badge}
                </div>
                <div class='grid grid-cols-2 gap-2'>
                    <a href='/timetable/assignments/{school_id}?grade_name={encoded_grade}&education_level={encoded_level}&stream={encoded_stream}' class='bg-slate-100 hover:bg-slate-200 text-slate-700 text-center text-xs py-1.5 rounded-lg font-semibold transition'>Teachers</a>
                    <a href='/timetable/grade/{school_id}?grade_name={encoded_grade}&education_level={encoded_level}&stream={encoded_stream}' class='bg-slate-800 hover:bg-slate-900 text-white text-center text-xs py-1.5 rounded-lg font-semibold transition'>Open</a>
                </div>
            </div>
            """
        level_groups_html += f"""
        <div class="mb-6">
            <div class="flex items-center justify-between mb-3">
                <h2 class="text-sm font-black text-slate-700">{esc(level_name)}</h2>
                <form action="/api/v1/timetable/test-and-generate-level/{school_id}" method="post" onsubmit="return confirm('Test and generate every class in {esc(level_name)}?');">
                    <input type="hidden" name="education_level" value="{esc(level_name)}">
                    <button type="submit" class="bg-amber-500 hover:bg-amber-600 text-white px-3.5 py-1.5 rounded-xl text-xs font-bold transition shadow-sm">🧪 Test &amp; Generate Level</button>
                </form>
            </div>
            <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">{cards_html}</div>
        </div>
        """

    return f"""
    <!DOCTYPE html>
    <html>
    <head><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Elimu Hub | Timetable Workspace</title><script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script></head>
    <body class="bg-[#F7F9F8] min-h-screen">
        <header class="bg-white border-b px-6 sm:px-8 py-4">
            <div class="flex flex-col sm:flex-row sm:justify-between sm:items-center gap-3">
                <div>
                    <h1 class="text-base font-bold text-slate-900">🗓️ Timetable Workspace — {esc(school['name'])}</h1>
                    <p class="text-xs text-slate-400">{len(sections)} class(es) · {subject_count} subject(s) · {teacher_count} teacher(s) assigned</p>
                </div>
                <a href="{get_dashboard_url(request, school_id)}" class="bg-slate-200 hover:bg-slate-300 text-slate-700 px-4 py-2 rounded-xl text-xs font-bold text-center transition">← Main Dashboard</a>
            </div>
            <div class="mt-3">
                <form action="/api/v1/timetable/test-and-generate-school/{school_id}" method="post" onsubmit="return confirm('Test and generate EVERY class across the WHOLE school? This is the safest option if any teacher teaches across more than one education level, since it checks conflicts across all of them together. This replaces existing entries for every class that passes validation.');">
                    <button type="submit" class="w-full sm:w-auto bg-amber-600 hover:bg-amber-700 text-white px-5 py-2.5 rounded-xl text-xs font-bold transition shadow-sm">🧪🏫 Test &amp; Generate WHOLE SCHOOL</button>
                </form>
                <p class="text-[11px] text-slate-400 mt-1">Use this instead of a single level's button if any teacher teaches across more than one education level (e.g. Lower and Upper Primary) — it checks for conflicts across all levels together, not just within one.</p>
            </div>
            <div class="flex gap-2 flex-wrap mt-4">
                <a href="/timetable/plans/{school_id}" class="bg-indigo-700 hover:bg-indigo-800 text-white px-3.5 py-2 rounded-xl text-xs font-bold text-center transition shadow-sm">🗂 Timetable Plans</a>
                <a href="/timetable/subjects-config/{school_id}" class="bg-white hover:bg-slate-50 text-slate-600 border border-slate-200 px-3.5 py-2 rounded-xl text-xs font-bold text-center transition">🎨 Subjects</a>
                <a href="/timetable/custom-subjects/{school_id}" class="bg-white hover:bg-slate-50 text-slate-600 border border-slate-200 px-3.5 py-2 rounded-xl text-xs font-bold text-center transition">➕ Custom Subjects</a>
                <a href="/timetable/periods/{school_id}" class="bg-white hover:bg-slate-50 text-slate-600 border border-slate-200 px-3.5 py-2 rounded-xl text-xs font-bold text-center transition">⏱ Periods &amp; Days</a>
                <a href="/timetable/availability/{school_id}" class="bg-white hover:bg-slate-50 text-slate-600 border border-slate-200 px-3.5 py-2 rounded-xl text-xs font-bold text-center transition">🧑‍🏫 Teacher Availability</a>
                <a href="/timetable/subject-availability/{school_id}" class="bg-white hover:bg-slate-50 text-slate-600 border border-slate-200 px-3.5 py-2 rounded-xl text-xs font-bold text-center transition">📚 Subject Time-Off</a>
                <a href="/timetable/sync-rules/{school_id}" class="bg-white hover:bg-slate-50 text-slate-600 border border-slate-200 px-3.5 py-2 rounded-xl text-xs font-bold text-center transition">🔗 Same-Time Subject Rules</a>
                <a href="/timetable/class-links/{school_id}" class="bg-white hover:bg-slate-50 text-slate-600 border border-slate-200 px-3.5 py-2 rounded-xl text-xs font-bold text-center transition">🔗 Linked Classes</a>
                <a href="/timetable/view/{school_id}" class="bg-white hover:bg-slate-50 text-slate-600 border border-slate-200 px-3.5 py-2 rounded-xl text-xs font-bold text-center transition">🔀 Whole/Teachers/Subjects View</a>
                <a href="/timetable/collision-check/{school_id}" class="bg-rose-600 hover:bg-rose-700 text-white px-3.5 py-2 rounded-xl text-xs font-bold text-center transition shadow-sm">🔍 Check for Collisions</a>
                <a href="/timetable/teacher-workload/{school_id}" class="bg-white hover:bg-slate-50 text-slate-600 border border-slate-200 px-3.5 py-2 rounded-xl text-xs font-bold text-center transition">📊 Teacher Workload</a>
                <a href="/timetable/teachers/{school_id}" class="bg-white hover:bg-slate-50 text-slate-600 border border-slate-200 px-3.5 py-2 rounded-xl text-xs font-bold text-center transition">🖨 Print Teacher Timetables</a>
            </div>
        </header>
        <div class="p-6 sm:p-8 max-w-6xl mx-auto">
            {"<div class='bg-amber-50 border border-amber-200 text-amber-800 text-sm px-4 py-3 rounded-xl mb-6'>⏱ <b>Set up your periods and bell times first</b> — go to <a href='/timetable/periods/" + str(school_id) + "' class='underline font-bold'>Periods &amp; Days</a> before generating any timetable.</div>" if not has_periods else ""}
            {level_groups_html or "<p class='text-slate-400 text-xs italic text-center py-8 bg-white border border-dashed rounded-2xl'>No classes with students yet — add students first.</p>"}
        </div>
    </body>
    </html>
    """


# ============================================================
# Subjects Config — short codes and colors per subject, the ASC-style
# "each subject is a visual entity" concept. Subjects themselves
# (learning_areas) are fetched from the shared curriculum data already
# used everywhere else in Elimu Hub — never re-entered here.
# ============================================================

@router.get("/timetable/plans/{school_id}", response_class=HTMLResponse)
def timetable_plans_view(request: Request, school_id: int, created: str = None):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT name FROM schools WHERE id = %s;", (school_id,))
            school = cur.fetchone()
            if not school:
                raise HTTPException(status_code=404, detail="School not found.")

            cur.execute("SELECT * FROM timetable_plans WHERE school_id = %s ORDER BY education_level ASC, is_active DESC, created_at ASC;", (school_id,))
            plans = cur.fetchall()

    plans_by_level = {}
    for p in plans:
        plans_by_level.setdefault(p['education_level'], []).append(p)

    ALL_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    default_days = {"Monday", "Tuesday", "Wednesday", "Thursday", "Friday"}

    level_sections_html = ""
    for level in EDUCATION_LEVELS:
        level_plans = plans_by_level.get(level, [])
        plan_cards = ""
        for p in level_plans:
            days_display = p['active_days'].replace(",", ", ")
            badge = (
                "<span class='text-[10px] font-bold px-2 py-0.5 rounded-full bg-emerald-50 text-emerald-700 border border-emerald-200'>ACTIVE — visible to staff now</span>"
                if p['is_active'] else
                "<span class='text-[10px] font-bold px-2 py-0.5 rounded-full bg-slate-100 text-slate-500 border border-slate-200'>Draft — not visible to staff</span>"
            )

            actions = ""
            if not p['is_active']:
                actions += f"""
                <form action="/api/v1/timetable/plans/set-active/{school_id}/{p['id']}" method="post" class="inline" onsubmit="return confirm('Make {esc(p['name'])!r} the active plan for {esc(level)}? Staff, reports, and printing will immediately switch to showing this plan instead of whichever one is active now.');">
                    <button type="submit" class="text-xs font-bold text-emerald-700 hover:underline">Set Active</button>
                </form>
                """
            actions += f"""
            <a href="/timetable/plans/duplicate/{school_id}/{p['id']}" class="text-xs font-bold text-indigo-700 hover:underline ml-3">Save As Copy</a>
            """
            if not p['is_active']:
                actions += f"""
                <form action="/api/v1/timetable/plans/delete/{school_id}/{p['id']}" method="post" class="inline ml-3" onsubmit="return confirm('Delete {esc(p['name'])!r} permanently? Every period, teacher assignment, and generated slot belonging to this plan will be deleted too. This cannot be undone.');">
                    <button type="submit" class="text-xs font-bold text-rose-600 hover:underline">Delete</button>
                </form>
                """

            plan_cards += f"""
            <div class="border border-slate-200 rounded-xl p-3 flex items-center justify-between flex-wrap gap-2">
                <div>
                    <p class="text-sm font-bold text-slate-800">{esc(p['name'])} {badge}</p>
                    <p class="text-[11px] text-slate-400 mt-0.5">Days: {esc(days_display)}</p>
                </div>
                <div>{actions}</div>
            </div>
            """

        day_checkboxes = "".join(
            f"<label class='flex items-center gap-1 text-xs'><input type='checkbox' name='days' value='{d}' {'checked' if d in default_days else ''}> {d}</label>"
            for d in ALL_DAYS
        )

        level_sections_html += f"""
        <div class="bg-white p-5 rounded-2xl border shadow-xs space-y-3">
            <h3 class="text-sm font-black text-slate-800">{esc(level)}</h3>
            <div class="space-y-2">
                {plan_cards or "<p class='text-xs text-slate-400 italic'>No plans yet for this level — the first one you create can be set active.</p>"}
            </div>
            <details class="pt-2 border-t border-slate-100">
                <summary class="text-xs font-bold text-indigo-700 cursor-pointer">+ Create a new plan for {esc(level)}</summary>
                <form action="/api/v1/timetable/plans/create/{school_id}" method="post" class="mt-3 space-y-2">
                    <input type="hidden" name="education_level" value="{esc(level)}">
                    <input type="text" name="name" placeholder="e.g. Weekend Program" required class="w-full border border-slate-200 p-2 rounded-lg text-xs">
                    <div class="flex flex-wrap gap-3 bg-slate-50 p-2.5 rounded-lg">{day_checkboxes}</div>
                    <button type="submit" class="bg-indigo-700 hover:bg-indigo-800 text-white text-xs font-bold px-4 py-2 rounded-lg">Create Plan</button>
                </form>
            </details>
        </div>
        """

    return f"""
    <!DOCTYPE html>
    <html>
    <head><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Elimu Hub | Timetable Plans</title><script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script></head>
    <body class="bg-slate-100 min-h-screen p-4 sm:p-8">
        <div class="max-w-3xl mx-auto space-y-4">
            <a href="/timetable/dashboard/{school_id}" class="text-slate-500 hover:text-slate-700 text-xs font-bold inline-block">← Back to Timetable Workspace</a>
            <div class="bg-white p-6 rounded-2xl border shadow-xs">
                <h2 class="text-lg font-black text-slate-800">🗂 Timetable Plans — {esc(school['name'])}</h2>
                <p class="text-xs text-slate-400 mt-1">Create several independent timetables per education level — a normal weekday one, plus e.g. a weekend program — each with its own days, periods, teacher assignments, and rules. Only the plan marked ACTIVE is what staff, reports, and printing actually show.</p>
            </div>
            {"<div class='bg-emerald-50 border border-emerald-200 text-emerald-800 text-xs px-4 py-2.5 rounded-xl'>✅ Done.</div>" if created else ""}
            {level_sections_html}
        </div>
    </body>
    </html>
    """


@router.post("/api/v1/timetable/plans/create/{school_id}")
async def create_timetable_plan(school_id: int, request: Request):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    form_data = await request.form()
    education_level = (form_data.get("education_level") or "").strip()
    name = (form_data.get("name") or "").strip()[:150]
    days_selected = form_data.getlist("days")

    if not education_level or not name:
        raise HTTPException(status_code=400, detail="A plan needs both a name and an education level.")

    all_days_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    ordered_days = [d for d in all_days_order if d in days_selected]
    active_days_str = ",".join(ordered_days) if ordered_days else "Monday,Tuesday,Wednesday,Thursday,Friday"

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO timetable_plans (school_id, education_level, name, active_days, is_active)
                VALUES (%s, %s, %s, %s, FALSE);
            """, (school_id, education_level, name, active_days_str))
            conn.commit()

    return RedirectResponse(url=f"/timetable/plans/{school_id}?created=1", status_code=303)


@router.post("/api/v1/timetable/plans/set-active/{school_id}/{plan_id}")
def set_active_timetable_plan(school_id: int, plan_id: int, request: Request):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT education_level FROM timetable_plans WHERE id = %s AND school_id = %s;", (plan_id, school_id))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Plan not found.")
            education_level = row[0]

            # Exactly one active plan per (school, education_level) —
            # switch the old one off before switching the new one on.
            cur.execute("UPDATE timetable_plans SET is_active = FALSE WHERE school_id = %s AND education_level = %s;", (school_id, education_level))
            cur.execute("UPDATE timetable_plans SET is_active = TRUE, updated_at = NOW() WHERE id = %s;", (plan_id,))
            conn.commit()

    return RedirectResponse(url=f"/timetable/plans/{school_id}", status_code=303)


@router.post("/api/v1/timetable/plans/delete/{school_id}/{plan_id}")
def delete_timetable_plan(school_id: int, plan_id: int, request: Request):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT is_active FROM timetable_plans WHERE id = %s AND school_id = %s;", (plan_id, school_id))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Plan not found.")
            if row[0]:
                raise HTTPException(status_code=400, detail="Can't delete the active plan — set a different plan active first, or create a new one, before deleting this.")

            # Every dependent table's plan_id column was created with
            # ON DELETE CASCADE — deleting this one row automatically
            # deletes every period, assignment, slot, and rule that
            # belongs only to this plan.
            cur.execute("DELETE FROM timetable_plans WHERE id = %s;", (plan_id,))
            conn.commit()

    return RedirectResponse(url=f"/timetable/plans/{school_id}", status_code=303)


@router.get("/timetable/plans/duplicate/{school_id}/{plan_id}", response_class=HTMLResponse)
def duplicate_timetable_plan_form(school_id: int, plan_id: int, request: Request):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM timetable_plans WHERE id = %s AND school_id = %s;", (plan_id, school_id))
            source_plan = cur.fetchone()
            if not source_plan:
                raise HTTPException(status_code=404, detail="Plan not found.")

    source_days = set(source_plan['active_days'].split(","))
    all_days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    day_checkboxes = "".join(
        f"<label class='flex items-center gap-1 text-xs'><input type='checkbox' name='days' value='{d}' {'checked' if d in source_days else ''}> {d}</label>"
        for d in all_days
    )

    return f"""
    <!DOCTYPE html>
    <html>
    <head><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Elimu Hub | Save As Copy</title><script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script></head>
    <body class="bg-slate-100 min-h-screen p-4 sm:p-8">
        <div class="max-w-md mx-auto space-y-4">
            <a href="/timetable/plans/{school_id}" class="text-slate-500 hover:text-slate-700 text-xs font-bold inline-block">← Back to Plans</a>
            <div class="bg-white p-6 rounded-2xl border shadow-xs space-y-4">
                <h2 class="text-lg font-black text-slate-800">Save "{esc(source_plan['name'])}" As a Copy</h2>
                <p class="text-xs text-slate-400">Creates a brand new, fully independent plan for {esc(source_plan['education_level'])} — every period, teacher assignment, subject rule, and already-generated slot from "{esc(source_plan['name'])}" is copied over. Editing the new copy afterward never touches the original.</p>
                <form method="post" action="/api/v1/timetable/plans/duplicate/{school_id}/{plan_id}" class="space-y-3">
                    <div>
                        <label class="text-[11px] font-semibold text-slate-500 block mb-1">New plan name</label>
                        <input type="text" name="name" value="{esc(source_plan['name'])} (Copy)" required class="w-full border border-slate-200 p-2.5 rounded-lg text-sm">
                    </div>
                    <div>
                        <label class="text-[11px] font-semibold text-slate-500 block mb-1">Days for the new copy</label>
                        <div class="flex flex-wrap gap-3 bg-slate-50 p-2.5 rounded-lg">{day_checkboxes}</div>
                    </div>
                    <button type="submit" class="w-full bg-indigo-700 hover:bg-indigo-800 text-white text-sm font-bold py-2.5 rounded-xl transition">Create Copy</button>
                </form>
            </div>
        </div>
    </body>
    </html>
    """


@router.post("/api/v1/timetable/plans/duplicate/{school_id}/{plan_id}")
async def duplicate_timetable_plan(school_id: int, plan_id: int, request: Request):
    """Copies every row belonging to one plan into a brand new one. Done
    in dependency order — timetable_periods and timetable_custom_subjects
    are copied FIRST, building an old-id -> new-id map for each, since
    everything else (slots, availability, sync rules, assignments, linked
    classes) references those two tables' own ids and must be rewritten
    to point at the new copies, not the originals."""
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    form_data = await request.form()
    new_name = (form_data.get("name") or "").strip()[:150]
    days_selected = form_data.getlist("days")
    if not new_name:
        raise HTTPException(status_code=400, detail="The new plan needs a name.")

    all_days_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    ordered_days = [d for d in all_days_order if d in days_selected]

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM timetable_plans WHERE id = %s AND school_id = %s;", (plan_id, school_id))
            source_plan = cur.fetchone()
            if not source_plan:
                raise HTTPException(status_code=404, detail="Plan not found.")

            active_days_str = ",".join(ordered_days) if ordered_days else source_plan['active_days']

            cur.execute("""
                INSERT INTO timetable_plans (school_id, education_level, name, active_days, is_active)
                VALUES (%s, %s, %s, %s, FALSE) RETURNING id;
            """, (school_id, source_plan['education_level'], new_name, active_days_str))
            new_plan_id = cur.fetchone()['id']

            # --- Step 1: timetable_periods — build old_id -> new_id map ---
            cur.execute("SELECT * FROM timetable_periods WHERE plan_id = %s;", (plan_id,))
            period_id_map = {}
            for row in cur.fetchall():
                cur.execute("""
                    INSERT INTO timetable_periods (school_id, education_level, label, short_label, start_time, end_time, period_order, period_type, is_teaching_period, plan_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id;
                """, (row['school_id'], row['education_level'], row['label'], row['short_label'], row['start_time'], row['end_time'], row['period_order'], row['period_type'], row['is_teaching_period'], new_plan_id))
                period_id_map[row['id']] = cur.fetchone()['id']

            # --- Step 2: timetable_custom_subjects — build old_id -> new_id map ---
            cur.execute("SELECT * FROM timetable_custom_subjects WHERE plan_id = %s;", (plan_id,))
            custom_subject_id_map = {}
            for row in cur.fetchall():
                cur.execute("""
                    INSERT INTO timetable_custom_subjects (school_id, education_level, name, plan_id)
                    VALUES (%s, %s, %s, %s) RETURNING id;
                """, (row['school_id'], row['education_level'], row['name'], new_plan_id))
                custom_subject_id_map[row['id']] = cur.fetchone()['id']

            # --- Step 3: teacher_subject_assignments (remap custom_subject_id) ---
            cur.execute("SELECT * FROM teacher_subject_assignments WHERE plan_id = %s;", (plan_id,))
            for row in cur.fetchall():
                new_custom_id = custom_subject_id_map.get(row['custom_subject_id']) if row['custom_subject_id'] else None
                cur.execute("""
                    INSERT INTO teacher_subject_assignments (school_id, grade_name, education_level, stream, learning_area_id, staff_user_id, lessons_per_week, requires_double, double_lessons_count, custom_subject_id, plan_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
                """, (row['school_id'], row['grade_name'], row['education_level'], row['stream'], row['learning_area_id'], row['staff_user_id'], row['lessons_per_week'], row['requires_double'], row['double_lessons_count'], new_custom_id, new_plan_id))

            # --- Step 4: timetable_slots (remap period_id, custom_subject_id) ---
            cur.execute("SELECT * FROM timetable_slots WHERE plan_id = %s;", (plan_id,))
            for row in cur.fetchall():
                new_period_id = period_id_map.get(row['period_id'])
                if new_period_id is None:
                    continue  # source slot's period was somehow not copied — skip rather than insert a dangling reference
                new_custom_id = custom_subject_id_map.get(row['custom_subject_id']) if row['custom_subject_id'] else None
                cur.execute("""
                    INSERT INTO timetable_slots (school_id, grade_name, education_level, stream, day_of_week, period_id, learning_area_id, custom_subject_id, co_curricular_activity_id, staff_user_id, plan_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
                """, (row['school_id'], row['grade_name'], row['education_level'], row['stream'], row['day_of_week'], new_period_id, row['learning_area_id'], new_custom_id, row['co_curricular_activity_id'], row['staff_user_id'], new_plan_id))

            # --- Step 5: teacher_availability (remap period_id) ---
            cur.execute("SELECT * FROM teacher_availability WHERE plan_id = %s;", (plan_id,))
            for row in cur.fetchall():
                new_period_id = period_id_map.get(row['period_id'])
                if new_period_id is None:
                    continue
                cur.execute("""
                    INSERT INTO teacher_availability (school_id, staff_user_id, day_of_week, period_id, status, plan_id)
                    VALUES (%s, %s, %s, %s, %s, %s);
                """, (row['school_id'], row['staff_user_id'], row['day_of_week'], new_period_id, row['status'], new_plan_id))

            # --- Step 6: subject_availability (remap period_id, custom_subject_id) ---
            cur.execute("SELECT * FROM subject_availability WHERE plan_id = %s;", (plan_id,))
            for row in cur.fetchall():
                new_period_id = period_id_map.get(row['period_id'])
                if new_period_id is None:
                    continue
                new_custom_id = custom_subject_id_map.get(row['custom_subject_id']) if row['custom_subject_id'] else None
                cur.execute("""
                    INSERT INTO subject_availability (school_id, learning_area_id, day_of_week, period_id, status, custom_subject_id, plan_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s);
                """, (row['school_id'], row['learning_area_id'], row['day_of_week'], new_period_id, row['status'], new_custom_id, new_plan_id))

            # --- Step 7: subject_sync_rules (remap period_id — can be NULL) ---
            cur.execute("SELECT * FROM subject_sync_rules WHERE plan_id = %s;", (plan_id,))
            for row in cur.fetchall():
                new_period_id = period_id_map.get(row['period_id']) if row['period_id'] else None
                cur.execute("""
                    INSERT INTO subject_sync_rules (school_id, learning_area_id, day_of_week, period_id, plan_id)
                    VALUES (%s, %s, %s, %s, %s);
                """, (row['school_id'], row['learning_area_id'], row['day_of_week'], new_period_id, new_plan_id))

            # --- Step 8: subject_constraints (no period/custom-subject reference) ---
            cur.execute("SELECT * FROM subject_constraints WHERE plan_id = %s;", (plan_id,))
            for row in cur.fetchall():
                cur.execute("""
                    INSERT INTO subject_constraints (school_id, grade_name, education_level, stream, subject_a_id, subject_b_id, constraint_type, plan_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s);
                """, (row['school_id'], row['grade_name'], row['education_level'], row['stream'], row['subject_a_id'], row['subject_b_id'], row['constraint_type'], new_plan_id))

            # --- Step 9: class_link_rules (remap custom_subject_id) ---
            cur.execute("SELECT * FROM class_link_rules WHERE plan_id = %s;", (plan_id,))
            for row in cur.fetchall():
                new_custom_id = custom_subject_id_map.get(row['custom_subject_id']) if row['custom_subject_id'] else None
                cur.execute("""
                    INSERT INTO class_link_rules (school_id, learning_area_id, custom_subject_id, class_a_grade_name, class_a_education_level, class_a_stream, class_b_grade_name, class_b_education_level, class_b_stream, plan_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
                """, (row['school_id'], row['learning_area_id'], new_custom_id, row['class_a_grade_name'], row['class_a_education_level'], row['class_a_stream'], row['class_b_grade_name'], row['class_b_education_level'], row['class_b_stream'], new_plan_id))

            conn.commit()

    return RedirectResponse(url=f"/timetable/plans/{school_id}?created=1", status_code=303)


@router.get("/timetable/subjects-config/{school_id}", response_class=HTMLResponse)
def subjects_config_view(school_id: int, request: Request, education_level: str = "Upper Primary"):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT id, name FROM learning_areas WHERE education_level = %s;", (education_level,))
            subjects = sort_subjects_for_display(cur.fetchall(), education_level)

            cur.execute("SELECT learning_area_id, short_code, color_hex FROM timetable_subject_config WHERE school_id = %s;", (school_id,))
            existing = {r['learning_area_id']: r for r in cur.fetchall()}

    level_tabs = "".join(
        f"""<a href="/timetable/subjects-config/{school_id}?education_level={urllib.parse.quote(lvl)}"
               class="px-4 py-2 rounded-xl text-xs font-bold transition {'bg-indigo-800 text-white' if lvl == education_level else 'bg-white border border-slate-200 text-slate-600 hover:bg-slate-50'}">{lvl}</a>"""
        for lvl in EDUCATION_LEVELS
    )

    rows_html = ""
    for i, sub in enumerate(subjects):
        cfg = existing.get(sub['id'])
        short_code = (cfg['short_code'] if cfg else None) or abbreviate_subject(sub['name'])
        color_hex = (cfg['color_hex'] if cfg else None) or DEFAULT_SUBJECT_COLORS[i % len(DEFAULT_SUBJECT_COLORS)]
        rows_html += f"""
        <div class="flex items-center gap-3 py-2.5 border-b border-slate-50 last:border-0">
            <span class="w-5 h-5 rounded-md shrink-0" style="background:{esc(color_hex)};"></span>
            <span class="text-sm font-semibold text-slate-700 flex-1">{esc(sub['name'])}</span>
            <input type="text" name="short_code_{sub['id']}" value="{esc(short_code)}" maxlength="10" class="border p-2 rounded-lg w-24 text-xs text-center font-bold uppercase">
            <input type="color" name="color_{sub['id']}" value="{esc(color_hex)}" class="w-10 h-9 rounded-lg border cursor-pointer">
        </div>
        """

    return f"""
    <!DOCTYPE html>
    <html>
    <head><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Elimu Hub | Subjects</title><script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script></head>
    <body class="bg-[#F7F9F8] min-h-screen p-4 sm:p-8">
        <div class="max-w-xl mx-auto space-y-4">
            <div class="bg-white p-6 rounded-2xl border shadow-xs">
                <h2 class="text-lg font-black text-slate-800">🎨 Subjects — Codes &amp; Colors</h2>
                <p class="text-xs text-slate-400 mt-1">These short codes and colors are used across every timetable view and printout. This list is your curriculum's graded subjects (English, Math, etc.) — add or remove those in your main Subjects setup, not here.</p>
                <p class="text-xs text-slate-500 mt-2 bg-indigo-50 border border-indigo-100 rounded-lg px-3 py-2">Need to add, edit, or delete a subject that's <b>only for scheduling</b> — like splitting Creative Arts into Music/Art/PE, or a non-examinable subject like PPI? That's done on the <a href="/timetable/custom-subjects/{school_id}?education_level={urllib.parse.quote(education_level)}" class="text-indigo-700 font-bold hover:underline">Custom Subjects page →</a></p>
            </div>
            <div class="flex gap-2 flex-wrap">{level_tabs}</div>
            <form action="/api/v1/timetable/subjects-config/save/{school_id}" method="post" class="bg-white p-6 rounded-2xl border shadow-xs">
                <input type="hidden" name="education_level" value="{esc(education_level)}">
                {rows_html or "<p class='text-slate-400 text-xs italic py-4'>No subjects configured for this level yet.</p>"}
                <button type="submit" class="w-full mt-4 bg-indigo-800 hover:bg-indigo-900 text-white font-bold py-3 rounded-xl text-sm transition">Save</button>
            </form>
            <a href="/timetable/dashboard/{school_id}" class="bg-slate-200 hover:bg-slate-300 text-slate-700 font-bold py-2.5 px-5 rounded-xl text-sm transition inline-block">← Back to Workspace</a>
        </div>
    </body>
    </html>
    """


@router.post("/api/v1/timetable/subjects-config/save/{school_id}")
async def save_subjects_config(school_id: int, request: Request):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    form_data = await request.form()
    education_level = (form_data.get("education_level") or "").strip()

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT id FROM learning_areas WHERE education_level = %s;", (education_level,))
            subjects = cur.fetchall()

            for sub in subjects:
                short_code = (form_data.get(f"short_code_{sub['id']}") or "").strip().upper()[:10]
                color_hex = (form_data.get(f"color_{sub['id']}") or "").strip()
                if not short_code and not color_hex:
                    continue
                cur.execute("""
                    INSERT INTO timetable_subject_config (school_id, learning_area_id, short_code, color_hex)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (school_id, learning_area_id) DO UPDATE SET short_code = EXCLUDED.short_code, color_hex = EXCLUDED.color_hex;
                """, (school_id, sub['id'], short_code or None, color_hex or None))
            conn.commit()

    return RedirectResponse(url=f"/timetable/subjects-config/{school_id}?education_level={urllib.parse.quote(education_level)}", status_code=303)


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
        SELECT learning_area_id, staff_user_id, lessons_per_week, requires_double, double_lessons_count FROM teacher_subject_assignments
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
        doubles = a.get('double_lessons_count') or 0
        if doubles > 0 and not has_consecutive_pair:
            errors.append(f"'{sub['name']}' is set for {doubles} double lesson(s) per week, but {education_level} has no two consecutive teaching periods anywhere in the day — a double lesson literally cannot be placed. Either add consecutive periods or set doubles/week to 0 for this subject.")
        elif doubles * 2 > lessons:
            errors.append(f"'{sub['name']}' is set for {doubles} double lesson(s) per week, which needs {doubles * 2} lessons just for the doubles, but only {lessons} lesson(s)/week are configured in total. Reduce the number of doubles or increase lessons/week for this subject.")

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



# ============================================================
# Periods & Days
# ============================================================

@router.get("/timetable/periods/{school_id}", response_class=HTMLResponse)
def timetable_periods_view(school_id: int, request: Request, education_level: str = "Lower Primary", plan_id: int = None):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT name FROM schools WHERE id = %s;", (school_id,))
            school = cur.fetchone()
            if not school:
                raise HTTPException(status_code=404, detail="School not found.")

            resolved_plan_id = resolve_plan_id(cur, school_id, education_level, plan_id)
            cur.execute("SELECT name, active_days FROM timetable_plans WHERE id = %s;", (resolved_plan_id,))
            plan_row = cur.fetchone()
            plan_options_html = get_plan_options_html(cur, school_id, education_level, resolved_plan_id)
            current_days = set((plan_row['active_days'] or "").split(",")) if plan_row else set()

            periods = get_periods_for_level(cur, school_id, education_level, resolved_plan_id)

    all_seven_days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    day_checkboxes = "".join(
        f"<label class='flex items-center gap-1.5 text-xs font-semibold text-slate-600'><input type='checkbox' name='days' value='{d}' {'checked' if d in current_days else ''}> {d}</label>"
        for d in all_seven_days
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
                <form action="/api/v1/timetable/periods/delete/{school_id}/{p['id']}?plan_id={resolved_plan_id}" method="post" onsubmit="return confirm('Delete period \\'{esc(p['label'])}\\'? Any timetable slots using it will be cleared too.');">
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
            <div class="bg-indigo-50 border border-indigo-200 rounded-2xl p-4 flex flex-col sm:flex-row sm:items-center gap-3">
                <label class="text-xs font-bold text-indigo-800 shrink-0">📋 Working on plan:</label>
                <form method="get" action="/timetable/periods/{school_id}" class="flex-1 flex gap-2">
                    <input type="hidden" name="education_level" value="{esc(education_level)}">
                    <select name="plan_id" onchange="this.form.submit()" class="flex-1 border border-indigo-200 bg-white p-2 rounded-xl text-xs font-semibold">{plan_options_html}</select>
                </form>
                <a href="/timetable/plans/{school_id}" class="text-[11px] font-bold text-indigo-700 hover:underline whitespace-nowrap">Manage Plans →</a>
            </div>

            <div class="bg-white p-5 sm:p-6 rounded-2xl border shadow-xs">
                <h2 class="text-sm font-bold text-slate-800 mb-1">Days for "{esc(plan_row['name'] if plan_row else 'this plan')}"</h2>
                <p class="text-xs text-slate-400 mb-3">Pick any combination — this is specific to this one plan, so a weekend plan can run on just Saturday + Sunday while your normal weekday plan is untouched.</p>
                <form action="/api/v1/timetable/periods/days/{school_id}?education_level={urllib.parse.quote(education_level)}&plan_id={resolved_plan_id}" method="post" class="flex flex-col gap-3">
                    <div class="flex flex-wrap gap-3 bg-slate-50 p-3 rounded-xl">{day_checkboxes}</div>
                    <button type="submit" class="bg-indigo-700 hover:bg-indigo-800 text-white font-bold px-5 py-2.5 rounded-xl text-sm transition self-start">Save Days</button>
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
                        <tbody>{period_rows or "<tr><td colspan='7' class='text-center p-6 text-slate-400 italic text-xs'>No periods configured yet for this plan/level — add the first one below.</td></tr>"}</tbody>
                    </table>
                </div>
                <form action="/api/v1/timetable/periods/add/{school_id}" method="post" class="p-5 sm:p-6 bg-slate-50/50 border-t grid grid-cols-1 sm:grid-cols-6 gap-3">
                    <input type="hidden" name="education_level" value="{esc(education_level)}">
                    <input type="hidden" name="plan_id" value="{resolved_plan_id}">
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
async def save_timetable_days(school_id: int, request: Request, education_level: str, plan_id: int):
    """Saves this ONE plan's own day selection — any combination of the
    7 real days, not the old school-wide "count from Monday" model. A
    weekend plan and a normal weekday plan can each have their own
    completely independent day list."""
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    form_data = await request.form()
    days_selected = form_data.getlist("days")
    all_days_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    ordered_days = [d for d in all_days_order if d in days_selected]
    active_days_str = ",".join(ordered_days) if ordered_days else "Monday,Tuesday,Wednesday,Thursday,Friday"

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE timetable_plans SET active_days = %s, updated_at = NOW() WHERE id = %s AND school_id = %s;", (active_days_str, plan_id, school_id))
            conn.commit()

    return RedirectResponse(url=f"/timetable/periods/{school_id}?education_level={urllib.parse.quote(education_level)}&plan_id={plan_id}", status_code=303)


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
    plan_id: int = Form(...),
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
                "SELECT COALESCE(MAX(period_order), 0) + 1 AS next_order FROM timetable_periods WHERE school_id = %s AND education_level = %s AND plan_id = %s;",
                (school_id, education_level, plan_id)
            )
            next_order = cur.fetchone()[0]
            cur.execute("""
                INSERT INTO timetable_periods (school_id, education_level, period_order, label, short_label, start_time, end_time, is_teaching_period, period_type, plan_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
            """, (school_id, education_level, next_order, label, short_label, start_time, end_time, is_teaching_period, period_type, plan_id))
            conn.commit()

    return RedirectResponse(url=f"/timetable/periods/{school_id}?education_level={urllib.parse.quote(education_level)}&plan_id={plan_id}", status_code=303)


@router.post("/api/v1/timetable/periods/delete/{school_id}/{period_id}")
def delete_timetable_period(school_id: int, period_id: int, request: Request, plan_id: int = None):
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

    return RedirectResponse(url=f"/timetable/periods/{school_id}?education_level={urllib.parse.quote(level)}" + (f"&plan_id={plan_id}" if plan_id else ""), status_code=303)



# ============================================================
# Teacher Assignments
# ============================================================

@router.get("/timetable/assignments/{school_id}", response_class=HTMLResponse)
def teacher_assignments_view(school_id: int, request: Request, grade_name: str, education_level: str, stream: str, plan_id: int = None):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            resolved_plan_id = resolve_plan_id(cur, school_id, education_level, plan_id)
            plan_options_html = get_plan_options_html(cur, school_id, education_level, resolved_plan_id)

            cur.execute("SELECT id, name FROM learning_areas WHERE education_level = %s;", (education_level,))
            subjects = sort_subjects_for_display(cur.fetchall(), education_level)

            cur.execute("SELECT id, name FROM timetable_custom_subjects WHERE school_id = %s AND education_level = %s AND plan_id = %s ORDER BY name ASC;", (school_id, education_level, resolved_plan_id))
            custom_subjects = cur.fetchall()

            cur.execute("SELECT id, email, full_name FROM users WHERE school_id = %s AND role = 'staff' AND is_verified = TRUE ORDER BY full_name NULLS LAST, email ASC;", (school_id,))
            staff_members = cur.fetchall()

            cur.execute("""
                SELECT learning_area_id, custom_subject_id, staff_user_id, lessons_per_week, requires_double, double_lessons_count FROM teacher_subject_assignments
                WHERE school_id = %s AND grade_name = %s AND education_level = %s AND stream = %s AND plan_id = %s;
            """, (school_id, grade_name, education_level, stream, resolved_plan_id))
            all_assignments = cur.fetchall()
            current_assignments = {r['learning_area_id']: r for r in all_assignments if r['learning_area_id'] is not None}
            current_custom_assignments = {r['custom_subject_id']: r for r in all_assignments if r['custom_subject_id'] is not None}

    def _assignment_row(field_prefix, item_id, item_name, existing):
        assigned_id = existing.get('staff_user_id')
        lessons_per_week = existing.get('lessons_per_week', 1)
        double_lessons_count = existing.get('double_lessons_count', 0) or 0
        options = "<option value=''>— Unassigned —</option>" + "".join(
            f"<option value='{m['id']}' {'selected' if m['id'] == assigned_id else ''}>{esc(m['full_name'] or m['email'])}</option>"
            for m in staff_members
        )
        return f"""
        <div class="flex flex-col sm:flex-row sm:items-center gap-2 sm:gap-3 py-2.5 border-b border-slate-50 last:border-0">
            <span class="text-sm font-semibold text-slate-700 sm:w-40 shrink-0">{esc(item_name)}</span>
            <select name="{field_prefix}_{item_id}" class="border p-2 rounded-lg text-xs font-semibold bg-white flex-1 min-w-0">{options}</select>
            <div class="flex items-center gap-2 shrink-0">
                <label class="text-[10px] font-bold text-slate-500">Lessons/wk</label>
                <input type="number" name="lessons_{field_prefix}_{item_id}" value="{lessons_per_week}" min="0" max="20" class="border p-1.5 rounded-lg text-xs w-14 text-center">
                <label class="text-[10px] font-bold text-slate-500">Doubles/wk</label>
                <input type="number" name="doubles_{field_prefix}_{item_id}" value="{double_lessons_count}" min="0" max="10" class="border p-1.5 rounded-lg text-xs w-14 text-center" title="How many of this subject's weekly lessons should be back-to-back double periods (e.g. 2 doubles + 1 single = 5 lessons/week). The rest are single lessons.">
            </div>
        </div>
        """

    rows_html = "".join(_assignment_row("teacher", sub['id'], sub['name'], current_assignments.get(sub['id'], {})) for sub in subjects)
    custom_rows_html = "".join(_assignment_row("teachercustom", cs['id'], cs['name'], current_custom_assignments.get(cs['id'], {})) for cs in custom_subjects)

    section_label = _section_label(grade_name, stream)
    return f"""
    <!DOCTYPE html>
    <html>
    <head><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Elimu Hub | Assign Teachers</title><script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script></head>
    <body class="bg-slate-100 min-h-screen p-4 sm:p-8">
        <div class="max-w-xl mx-auto space-y-4">
            <div class="bg-indigo-50 border border-indigo-200 rounded-2xl p-4 flex flex-col sm:flex-row sm:items-center gap-3">
                <label class="text-xs font-bold text-indigo-800 shrink-0">📋 Working on plan:</label>
                <form method="get" action="/timetable/assignments/{school_id}" class="flex-1 flex gap-2">
                    <input type="hidden" name="grade_name" value="{esc(grade_name)}">
                    <input type="hidden" name="education_level" value="{esc(education_level)}">
                    <input type="hidden" name="stream" value="{esc(stream)}">
                    <select name="plan_id" onchange="this.form.submit()" class="flex-1 border border-indigo-200 bg-white p-2 rounded-xl text-xs font-semibold">{plan_options_html}</select>
                </form>
            </div>
            <div class="bg-white p-6 rounded-2xl border shadow-xs">
            <h2 class="text-lg font-black text-slate-800">Assign Teachers</h2>
            <p class="text-xs text-slate-400 mb-4">{esc(section_label)} ({esc(education_level)}) — who teaches each subject, how many lessons per week, and whether it needs a double lesson (e.g. for practicals).</p>
            {"<p class='text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded-lg p-3 mb-4'>No verified staff accounts yet — add and activate staff first, then come back to assign them here.</p>" if not staff_members else ""}
            <form action="/api/v1/timetable/assignments/{school_id}" method="post" class="space-y-1">
                <input type="hidden" name="grade_name" value="{esc(grade_name)}">
                <input type="hidden" name="education_level" value="{esc(education_level)}">
                <input type="hidden" name="stream" value="{esc(stream)}">
                <input type="hidden" name="plan_id" value="{resolved_plan_id}">
                {rows_html or "<p class='text-slate-400 text-xs italic'>No subjects configured for this education level.</p>"}
                {f'''<p class="text-[10px] font-bold uppercase tracking-wider text-slate-400 pt-4 pb-1">Custom Subjects (non-examinable / split subjects)</p>{custom_rows_html}''' if custom_subjects else ""}
                <p class="text-[11px] text-slate-400 pt-3">Need a subject that isn't listed — like splitting Creative Arts into Music/Art/PE, or adding a non-graded subject like PPI? <a href="/timetable/custom-subjects/{school_id}?education_level={urllib.parse.quote(education_level)}&plan_id={resolved_plan_id}" class="text-indigo-700 font-bold hover:underline">Add a Custom Subject →</a></p>
                <div class="pt-4 flex gap-3">
                    <button type="submit" class="bg-indigo-700 hover:bg-indigo-800 text-white font-bold py-2.5 px-5 rounded-xl text-sm transition">Save Assignments</button>
                    <a href="/timetable/dashboard/{school_id}" class="bg-slate-200 hover:bg-slate-300 text-slate-700 font-bold py-2.5 px-5 rounded-xl text-sm transition">← Back</a>
                </div>
            </form>
            </div>
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
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            plan_id = resolve_plan_id(cur, school_id, education_level, int(form.get("plan_id")) if form.get("plan_id") else None)

            for key, value in form.items():
                # Regular subjects: "teacher_{learning_area_id}". Custom
                # subjects use a distinct "teachercustom_{id}" prefix so the
                # two never collide even though both are plain integer ids.
                if key.startswith("teachercustom_"):
                    custom_subject_id = int(key.replace("teachercustom_", ""))
                    staff_user_id = int(value) if value else None
                    try:
                        lessons_per_week = max(0, min(20, int(form.get(f"lessons_teachercustom_{custom_subject_id}", 1) or 1)))
                    except ValueError:
                        lessons_per_week = 1
                    try:
                        double_lessons_count = max(0, min(10, int(form.get(f"doubles_teachercustom_{custom_subject_id}", 0) or 0)))
                    except ValueError:
                        double_lessons_count = 0
                    # Kept in sync purely for backward compatibility with
                    # any other code path still reading the old boolean —
                    # double_lessons_count is the real source of truth now.
                    requires_double = double_lessons_count > 0

                    # Check-then-update-or-insert rather than ON CONFLICT —
                    # this targets a partial unique index (only enforced
                    # when custom_subject_id IS NOT NULL), and matching
                    # ON CONFLICT against a partial index correctly requires
                    # repeating its WHERE clause exactly; simpler and just
                    # as safe to avoid that entirely, the same lesson
                    # learned earlier with the finance module's fee
                    # structures.
                    cur.execute("""
                        SELECT id FROM teacher_subject_assignments
                        WHERE school_id = %s AND grade_name = %s AND education_level = %s AND stream = %s AND custom_subject_id = %s AND plan_id = %s;
                    """, (school_id, grade_name, education_level, stream, custom_subject_id, plan_id))
                    existing_row = cur.fetchone()
                    if existing_row:
                        cur.execute("""
                            UPDATE teacher_subject_assignments SET staff_user_id = %s, lessons_per_week = %s, requires_double = %s, double_lessons_count = %s
                            WHERE id = %s;
                        """, (staff_user_id, lessons_per_week, requires_double, double_lessons_count, existing_row['id']))
                    else:
                        cur.execute("""
                            INSERT INTO teacher_subject_assignments (school_id, staff_user_id, custom_subject_id, grade_name, education_level, stream, lessons_per_week, requires_double, double_lessons_count, plan_id)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
                        """, (school_id, staff_user_id, custom_subject_id, grade_name, education_level, stream, lessons_per_week, requires_double, double_lessons_count, plan_id))
                    continue

                if not key.startswith("teacher_"):
                    continue
                learning_area_id = int(key.replace("teacher_", ""))
                staff_user_id = int(value) if value else None

                try:
                    lessons_per_week = max(0, min(20, int(form.get(f"lessons_teacher_{learning_area_id}", 1) or 1)))
                except ValueError:
                    lessons_per_week = 1
                try:
                    double_lessons_count = max(0, min(10, int(form.get(f"doubles_teacher_{learning_area_id}", 0) or 0)))
                except ValueError:
                    double_lessons_count = 0
                requires_double = double_lessons_count > 0

                # ON CONFLICT targets the actual current unique constraint —
                # (school_id, grade_name, education_level, stream,
                # learning_area_id, plan_id), widened to include plan_id
                # when multi-plan support was added. The 5-column version
                # this used to say no longer matches any real constraint on
                # the table at all — every save through this exact path
                # was failing outright with a live Postgres error the
                # moment that migration ran, until this fix.
                cur.execute("""
                    INSERT INTO teacher_subject_assignments (school_id, staff_user_id, learning_area_id, grade_name, education_level, stream, lessons_per_week, requires_double, double_lessons_count, plan_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (school_id, grade_name, education_level, stream, learning_area_id, plan_id)
                    DO UPDATE SET staff_user_id = EXCLUDED.staff_user_id, lessons_per_week = EXCLUDED.lessons_per_week, requires_double = EXCLUDED.requires_double, double_lessons_count = EXCLUDED.double_lessons_count;
                """, (school_id, staff_user_id, learning_area_id, grade_name, education_level, stream, lessons_per_week, requires_double, double_lessons_count, plan_id))
            conn.commit()

    encoded_grade = urllib.parse.quote(grade_name)
    encoded_level = urllib.parse.quote(education_level)
    encoded_stream = urllib.parse.quote(stream)
    return RedirectResponse(url=f"/timetable/grade/{school_id}?grade_name={encoded_grade}&education_level={encoded_level}&stream={encoded_stream}&plan_id={plan_id}", status_code=303)



# ============================================================
# Availability picker, Teacher Timetable picker, Teacher Workload
# ============================================================

@router.post("/api/v1/timetable/sync-teachers/{school_id}")
def sync_teacher_names_into_slots(school_id: int, request: Request, grade_name: str = Form(...), education_level: str = Form(...), stream: str = Form(...)):
    """Updates ONLY which teacher is shown on each already-generated
    timetable slot, to match the CURRENT teacher_subject_assignments for
    this class — every day/period placement stays exactly where it
    already was. This exists because saving an assignment change on its
    own never touches timetable_slots at all (that table is only ever
    written by Generate/Test & Generate or a manual per-cell edit), so
    without this, the only way to pick up a reassigned teacher is a full
    Test & Generate — which reshuffles every subject's day/period
    placement across the whole class, not just the one subject whose
    teacher changed. This is the lightweight alternative for the common
    case: a teacher goes on leave, gets swapped, and nothing else about
    the timetable should move.

    Only touches slots holding a regular or custom academic subject —
    co-curricular slots have no corresponding teacher_subject_assignments
    row at all, so the join simply never matches them, leaving them
    untouched automatically."""
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE timetable_slots ts
                SET staff_user_id = tsa.staff_user_id
                FROM teacher_subject_assignments tsa
                WHERE ts.school_id = %s AND ts.grade_name = %s AND ts.education_level = %s AND ts.stream = %s
                  AND tsa.school_id = ts.school_id AND tsa.grade_name = ts.grade_name
                  AND tsa.education_level = ts.education_level AND tsa.stream = ts.stream
                  AND (
                        (ts.learning_area_id IS NOT NULL AND ts.learning_area_id = tsa.learning_area_id)
                        OR
                        (ts.custom_subject_id IS NOT NULL AND ts.custom_subject_id = tsa.custom_subject_id)
                      );
            """, (school_id, grade_name, education_level, stream))
            synced_count = cur.rowcount
            conn.commit()

    encoded_grade = urllib.parse.quote(grade_name)
    encoded_level = urllib.parse.quote(education_level)
    encoded_stream = urllib.parse.quote(stream)
    sync_result = "none" if synced_count == 0 else "ok"
    return RedirectResponse(url=f"/timetable/grade/{school_id}?grade_name={encoded_grade}&education_level={encoded_level}&stream={encoded_stream}&synced={sync_result}", status_code=303)


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

    # Staff go straight to their own schedule — they can't view anyone
    # else's now anyway (enforced server-side in print_teacher_timetable),
    # so showing them a picker full of names they can't actually open
    # would just be a confusing dead end. Admin/superadmin still get the
    # full picker, since they legitimately manage every teacher's schedule.
    viewer = get_current_session_user(request)
    if viewer and viewer['role'] == 'staff':
        return RedirectResponse(url=f"/timetable/print/teacher/{school_id}/{viewer['id']}", status_code=303)

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



# ============================================================
# Collision Checker
# ============================================================

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

    def _parse_time_to_minutes(time_str):
        if not time_str:
            return None
        cleaned = time_str.strip().upper().replace(".", "")
        is_pm = "PM" in cleaned
        is_am = "AM" in cleaned
        cleaned = cleaned.replace("AM", "").replace("PM", "").strip()
        parts = cleaned.replace(".", ":").split(":")
        try:
            hour = int(parts[0])
            minute = int(parts[1]) if len(parts) > 1 else 0
        except (ValueError, IndexError):
            return None
        if is_pm and hour < 12:
            hour += 12
        if is_am and hour == 12:
            hour = 0
        return hour * 60 + minute

    # Group by (day, teacher) first, then within each group find every
    # slot whose actual clock time overlaps another — this is what catches
    # a teacher double-booked across two different education levels at the
    # same real time, since each level's periods are separate rows with
    # different ids even when the times match exactly.
    by_day_teacher = {}
    for slot in all_slots:
        key = (slot['day_of_week'], slot['staff_user_id'])
        by_day_teacher.setdefault(key, []).append(slot)

    collision_groups = []
    for (day, teacher_id), slots in by_day_teacher.items():
        timed_slots = [(s, _parse_time_to_minutes(s['start_time']), _parse_time_to_minutes(s['end_time'])) for s in slots]
        timed_slots = [t for t in timed_slots if t[1] is not None and t[2] is not None]
        used = set()
        for i, (slot_a, a_start, a_end) in enumerate(timed_slots):
            if i in used:
                continue
            overlapping = [slot_a]
            for j, (slot_b, b_start, b_end) in enumerate(timed_slots):
                if j <= i or j in used:
                    continue
                if a_start < b_end and b_start < a_end:
                    overlapping.append(slot_b)
                    used.add(j)
            if len(overlapping) > 1:
                distinct_classes = {(s['grade_name'], s['stream']) for s in overlapping}
                if len(distinct_classes) > 1:
                    collision_groups.append(overlapping)
                used.add(i)

    return collision_groups


@router.get("/timetable/collision-check/{school_id}", response_class=HTMLResponse)
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



# ============================================================
# Custom Subjects, Co-Curricular Activities & Rosters
# ============================================================

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



# ============================================================
# Teacher Availability, Subject Availability, Sync Rules, Constraints
# ============================================================

@router.get("/timetable/availability/{school_id}/{teacher_id}", response_class=HTMLResponse)
def teacher_availability_grid(school_id: int, teacher_id: int, request: Request, education_level: str = "Lower Primary", plan_id: int = None):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT id, full_name, email FROM users WHERE id = %s AND school_id = %s AND role = 'staff';", (teacher_id, school_id))
            teacher = cur.fetchone()
            if not teacher:
                raise HTTPException(status_code=404, detail="Teacher not found.")

            resolved_plan_id = resolve_plan_id(cur, school_id, education_level, plan_id)
            plan_options_html = get_plan_options_html(cur, school_id, education_level, resolved_plan_id)

            days = get_school_days(cur, school_id, resolved_plan_id)
            conn.commit()

            periods = [p for p in get_periods_for_level(cur, school_id, education_level, resolved_plan_id) if p['is_teaching_period']]

            cur.execute("""
                SELECT day_of_week, period_id, status FROM teacher_availability
                WHERE school_id = %s AND staff_user_id = %s AND plan_id = %s;
            """, (school_id, teacher_id, resolved_plan_id))
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
        <div class="max-w-4xl mx-auto space-y-4">
            <div class="bg-indigo-50 border border-indigo-200 rounded-2xl p-4 flex flex-col sm:flex-row sm:items-center gap-3">
                <label class="text-xs font-bold text-indigo-800 shrink-0">📋 Working on plan:</label>
                <form method="get" action="/timetable/availability/{school_id}/{teacher_id}" class="flex-1 flex gap-2">
                    <input type="hidden" name="education_level" value="{esc(education_level)}">
                    <select name="plan_id" onchange="this.form.submit()" class="flex-1 border border-indigo-200 bg-white p-2 rounded-xl text-xs font-semibold">{plan_options_html}</select>
                </form>
            </div>
            <div class="bg-white p-6 rounded-2xl border shadow-xs">
            <h2 class="text-lg font-black text-slate-800">👩‍🏫 {esc(teacher['full_name'] or teacher['email'])} — Availability</h2>
            <p class="text-xs text-slate-400 mb-3">Mark when this teacher is unavailable (e.g. part-time, other commitments). The timetable generator and manual editor will both respect this.</p>
            <div class="flex gap-2 flex-wrap mb-4">
                <span class="text-xs font-bold text-slate-500 self-center mr-1">Level:</span>{level_tabs}
            </div>
            <form action="/api/v1/timetable/availability/update/{school_id}/{teacher_id}?education_level={urllib.parse.quote(education_level)}&plan_id={resolved_plan_id}" method="post">
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
        </div>
    </body>
    </html>
    """


@router.post("/api/v1/timetable/availability/update/{school_id}/{teacher_id}")
async def save_teacher_availability(school_id: int, teacher_id: int, request: Request, education_level: str = "Lower Primary", plan_id: int = None):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    form = await request.form()

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT id FROM users WHERE id = %s AND school_id = %s AND role = 'staff';", (teacher_id, school_id))
            if not cur.fetchone():
                raise HTTPException(status_code=404, detail="Teacher not found.")

            resolved_plan_id = resolve_plan_id(cur, school_id, education_level, plan_id)

            periods = [p for p in get_periods_for_level(cur, school_id, education_level, resolved_plan_id) if p['is_teaching_period']]
            period_ids = [p['id'] for p in periods]
            days = get_school_days(cur, school_id, resolved_plan_id)

            for day in days:
                for period_id in period_ids:
                    field_name = f"status_{day}_{period_id}"
                    status = form.get(field_name, "available")
                    if status == "available":
                        # Available is the implicit default — no need to store a row for it.
                        cur.execute("""
                            DELETE FROM teacher_availability
                            WHERE school_id = %s AND staff_user_id = %s AND day_of_week = %s AND period_id = %s AND plan_id = %s;
                        """, (school_id, teacher_id, day, period_id, resolved_plan_id))
                    else:
                        # ON CONFLICT targets the actual current unique
                        # constraint — (school_id, staff_user_id,
                        # day_of_week, period_id, plan_id), widened to
                        # include plan_id when multi-plan support was
                        # added. The 4-column version this used to say no
                        # longer matches any real constraint on the table,
                        # so every save through this exact path was
                        # failing outright with a live Postgres error the
                        # moment that migration ran, until this fix.
                        cur.execute("""
                            INSERT INTO teacher_availability (school_id, staff_user_id, day_of_week, period_id, status, plan_id)
                            VALUES (%s, %s, %s, %s, %s, %s)
                            ON CONFLICT (school_id, staff_user_id, day_of_week, period_id, plan_id)
                            DO UPDATE SET status = EXCLUDED.status;
                        """, (school_id, teacher_id, day, period_id, status, resolved_plan_id))
            conn.commit()

    return RedirectResponse(url=f"/timetable/availability/{school_id}/{teacher_id}?education_level={urllib.parse.quote(education_level)}&plan_id={resolved_plan_id}", status_code=303)


@router.get("/timetable/subject-availability/{school_id}", response_class=HTMLResponse)
def subject_availability_picker(school_id: int, request: Request):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT id, name, education_level FROM learning_areas ORDER BY education_level ASC, name ASC;")
            subjects = cur.fetchall()

            cur.execute("SELECT id, name, education_level FROM timetable_custom_subjects WHERE school_id = %s ORDER BY education_level ASC, name ASC;", (school_id,))
            custom_subjects = cur.fetchall()

    rows_html = "".join(f"""
        <a href="/timetable/subject-availability/{school_id}/{s['id']}" class="flex items-center justify-between p-4 border-b last:border-0 hover:bg-slate-50 transition">
            <span class="text-sm font-bold text-slate-800">{esc(s['name'])} <span class="text-[10px] text-slate-400 font-normal">({esc(s['education_level'])})</span></span>
            <span class="text-xs text-indigo-700 font-bold">Set Time Off →</span>
        </a>
    """ for s in subjects)

    custom_rows_html = "".join(f"""
        <a href="/timetable/subject-availability/{school_id}/{cs['id'] + CUSTOM_SUBJECT_ID_OFFSET}" class="flex items-center justify-between p-4 border-b last:border-0 hover:bg-slate-50 transition">
            <span class="text-sm font-bold text-slate-800">{esc(cs['name'])} <span class="text-[10px] text-slate-400 font-normal">({esc(cs['education_level'])} — custom)</span></span>
            <span class="text-xs text-indigo-700 font-bold">Set Time Off →</span>
        </a>
    """ for cs in custom_subjects)

    return f"""
    <!DOCTYPE html>
    <html>
    <head><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Elimu Hub | Subject Time Off</title><script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script></head>
    <body class="bg-slate-100 min-h-screen p-4 sm:p-8">
        <div class="max-w-xl mx-auto bg-white p-6 rounded-2xl border shadow-xs">
            <h2 class="text-lg font-black text-slate-800">📚 Subject Time Off</h2>
            <p class="text-xs text-slate-400 mb-4">Pick a subject to mark days/periods it can't (or preferably shouldn't) be scheduled in — e.g. no Math last period, PE unavailable when the field is in use.</p>
            <div>{rows_html or "<p class='text-slate-400 text-xs italic p-4'>No subjects configured yet.</p>"}</div>
            {f'''<p class="text-[10px] font-bold uppercase tracking-wider text-slate-400 pt-4 pb-1 px-1">Custom Subjects</p><div>{custom_rows_html}</div>''' if custom_subjects else ""}
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

    is_custom = learning_area_id >= CUSTOM_SUBJECT_ID_OFFSET
    real_custom_id = (learning_area_id - CUSTOM_SUBJECT_ID_OFFSET) if is_custom else None

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if is_custom:
                cur.execute("SELECT id, name, education_level FROM timetable_custom_subjects WHERE id = %s AND school_id = %s;", (real_custom_id, school_id))
            else:
                cur.execute("SELECT id, name, education_level FROM learning_areas WHERE id = %s;", (learning_area_id,))
            subject = cur.fetchone()
            if not subject:
                raise HTTPException(status_code=404, detail="Subject not found.")

            days = get_school_days(cur, school_id)
            conn.commit()

            periods = [p for p in get_periods_for_level(cur, school_id, subject['education_level']) if p['is_teaching_period']]

            if is_custom:
                cur.execute("""
                    SELECT day_of_week, period_id, status FROM subject_availability
                    WHERE school_id = %s AND custom_subject_id = %s;
                """, (school_id, real_custom_id))
            else:
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

    is_custom = learning_area_id >= CUSTOM_SUBJECT_ID_OFFSET
    real_custom_id = (learning_area_id - CUSTOM_SUBJECT_ID_OFFSET) if is_custom else None

    form = await request.form()

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            if is_custom:
                cur.execute("SELECT education_level FROM timetable_custom_subjects WHERE id = %s AND school_id = %s;", (real_custom_id, school_id))
            else:
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

                    if is_custom:
                        if status == "available":
                            cur.execute("""
                                DELETE FROM subject_availability
                                WHERE school_id = %s AND custom_subject_id = %s AND day_of_week = %s AND period_id = %s;
                            """, (school_id, real_custom_id, day, period_id))
                        else:
                            # Check-then-update-or-insert rather than
                            # ON CONFLICT — this targets a partial unique
                            # index (only enforced when custom_subject_id
                            # IS NOT NULL), and matching ON CONFLICT against
                            # a partial index correctly requires repeating
                            # its WHERE clause exactly; simpler and just as
                            # safe to avoid entirely.
                            cur.execute("""
                                SELECT id FROM subject_availability
                                WHERE school_id = %s AND custom_subject_id = %s AND day_of_week = %s AND period_id = %s;
                            """, (school_id, real_custom_id, day, period_id))
                            existing_row = cur.fetchone()
                            if existing_row:
                                cur.execute("UPDATE subject_availability SET status = %s WHERE id = %s;", (status, existing_row[0]))
                            else:
                                cur.execute("""
                                    INSERT INTO subject_availability (school_id, custom_subject_id, day_of_week, period_id, status)
                                    VALUES (%s, %s, %s, %s, %s);
                                """, (school_id, real_custom_id, day, period_id, status))
                    else:
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


@router.get("/timetable/class-links/{school_id}", response_class=HTMLResponse)
def class_link_rules_view(school_id: int, request: Request):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Every class currently in the school, for the two class pickers.
            cur.execute("""
                SELECT DISTINCT c.grade_name, c.education_level, s.stream
                FROM students s
                JOIN classes c ON s.class_id = c.id
                WHERE s.school_id = %s AND (s.status IS NULL OR s.status != 'GRADUATED')
                ORDER BY c.grade_name ASC, s.stream ASC;
            """, (school_id,))
            classes = cur.fetchall()

            # Every subject — regular and custom, combined in one encoded
            # list exactly like the Teaching Assignments picker does
            # (custom subject ids offset by CUSTOM_SUBJECT_ID_OFFSET so
            # both kinds share one dropdown/value space).
            cur.execute("SELECT id, name, education_level FROM learning_areas ORDER BY education_level ASC, name ASC;")
            subjects = [{'choice_id': r['id'], 'name': r['name'], 'education_level': r['education_level']} for r in cur.fetchall()]
            cur.execute("SELECT id, name, education_level FROM timetable_custom_subjects WHERE school_id = %s ORDER BY education_level ASC, name ASC;", (school_id,))
            subjects += [{'choice_id': r['id'] + CUSTOM_SUBJECT_ID_OFFSET, 'name': r['name'] + ' (custom)', 'education_level': r['education_level']} for r in cur.fetchall()]

            cur.execute("""
                SELECT clr.*,
                       la.name AS learning_area_name,
                       cs.name AS custom_subject_name
                FROM class_link_rules clr
                LEFT JOIN learning_areas la ON clr.learning_area_id = la.id
                LEFT JOIN timetable_custom_subjects cs ON clr.custom_subject_id = cs.id
                WHERE clr.school_id = %s
                ORDER BY clr.created_at DESC;
            """, (school_id,))
            rules = cur.fetchall()

    class_options = "".join(
        f"<option value='{esc(c['grade_name'])}|{esc(c['education_level'])}|{esc(c['stream'])}'>{esc(_section_label(c['grade_name'], c['stream']))} ({esc(c['education_level'])})</option>"
        for c in classes
    )
    subject_options = "".join(
        f"<option value='{s['choice_id']}'>{esc(s['name'])} — {esc(s['education_level'])}</option>" for s in subjects
    )

    rows_html = ""
    for r in rules:
        subject_name = r['learning_area_name'] or r['custom_subject_name'] or "Unknown subject"
        rows_html += f"""
        <tr class="border-b text-sm">
            <td class="p-3">
                <p class="font-bold text-slate-800">{esc(subject_name)}</p>
            </td>
            <td class="p-3 text-slate-600">{esc(_section_label(r['class_a_grade_name'], r['class_a_stream']))} <span class="text-slate-400">({esc(r['class_a_education_level'])})</span></td>
            <td class="p-3 text-slate-600">{esc(_section_label(r['class_b_grade_name'], r['class_b_stream']))} <span class="text-slate-400">({esc(r['class_b_education_level'])})</span></td>
            <td class="p-3">
                <form action="/api/v1/timetable/class-links/delete/{school_id}/{r['id']}" method="post" onsubmit="return confirm('Remove this link? The two classes will no longer be forced to the same time for this subject the next time either is generated.');">
                    <button type="submit" class="text-rose-600 hover:text-rose-800 text-xs font-bold">Remove</button>
                </form>
            </td>
        </tr>
        """

    return f"""
    <!DOCTYPE html>
    <html>
    <head><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Elimu Hub | Linked Classes</title><script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script></head>
    <body class="bg-slate-100 min-h-screen p-4 sm:p-8">
        <div class="max-w-4xl mx-auto space-y-4">
            <div class="bg-white p-6 rounded-2xl border shadow-xs">
                <h2 class="text-lg font-black text-slate-800">🔗 Linked Classes</h2>
                <p class="text-xs text-slate-400 mb-4">
                    Force two specific classes to have the same subject scheduled at the exact same day and period — e.g. Mathematics for Grade 8V and Grade 8J always at the same time. Useful so one teacher can combine both classes if a colleague is absent. The two classes can be different grades and even different education levels.
                </p>
                <p class="text-[11px] text-amber-700 bg-amber-50 border border-amber-100 rounded-lg px-3 py-2 mb-4">
                    ⚠️ After adding or changing a link, run <b>Test &amp; Generate</b> (or <b>Sync Teacher Names</b>, if only the teacher changed) on <b>both</b> linked classes. Whichever class is generated first sets the time; the other one then matches it. If you only regenerate one of the two afterward, they can drift apart again until you regenerate the other too.
                </p>
                <form action="/api/v1/timetable/class-links/save/{school_id}" method="post" class="grid grid-cols-1 sm:grid-cols-3 gap-2 items-end bg-slate-50 border border-slate-200 rounded-xl p-3">
                    <div>
                        <label class="text-[11px] font-semibold text-slate-500 block mb-1">Subject</label>
                        <select name="subject_choice" required class="w-full border border-slate-200 bg-white p-2 rounded-lg text-xs">{subject_options}</select>
                    </div>
                    <div>
                        <label class="text-[11px] font-semibold text-slate-500 block mb-1">Class A</label>
                        <select name="class_a" required class="w-full border border-slate-200 bg-white p-2 rounded-lg text-xs">{class_options}</select>
                    </div>
                    <div>
                        <label class="text-[11px] font-semibold text-slate-500 block mb-1">Class B</label>
                        <select name="class_b" required class="w-full border border-slate-200 bg-white p-2 rounded-lg text-xs">{class_options}</select>
                    </div>
                    <div class="sm:col-span-3">
                        <button type="submit" class="bg-indigo-700 hover:bg-indigo-800 text-white text-xs font-bold px-4 py-2 rounded-lg transition">+ Add Link</button>
                    </div>
                </form>
            </div>
            <div class="bg-white p-6 rounded-2xl border shadow-xs">
                <div class="overflow-x-auto">
                    <table class="w-full text-left border-collapse">
                        <thead><tr class="border-b-2 text-[11px] uppercase text-slate-400"><th class="p-3">Subject</th><th class="p-3">Class A</th><th class="p-3">Class B</th><th class="p-3"></th></tr></thead>
                        <tbody>{rows_html or "<tr><td colspan='4' class='p-6 text-center text-slate-400 italic text-xs'>No linked classes yet.</td></tr>"}</tbody>
                    </table>
                </div>
                <div class="pt-4">
                    <a href="/timetable/dashboard/{school_id}" class="bg-slate-200 hover:bg-slate-300 text-slate-700 font-bold py-2.5 px-5 rounded-xl text-sm transition inline-block">← Back</a>
                </div>
            </div>
        </div>
    </body>
    </html>
    """


@router.post("/api/v1/timetable/class-links/save/{school_id}")
def save_class_link_rule(school_id: int, request: Request, subject_choice: int = Form(...), class_a: str = Form(...), class_b: str = Form(...)):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    a_grade, a_level, a_stream = class_a.split("|")
    b_grade, b_level, b_stream = class_b.split("|")

    if (a_grade, a_level, a_stream) == (b_grade, b_level, b_stream):
        raise HTTPException(status_code=400, detail="Class A and Class B must be two different classes.")

    is_custom = subject_choice >= CUSTOM_SUBJECT_ID_OFFSET
    learning_area_id = None if is_custom else subject_choice
    custom_subject_id = (subject_choice - CUSTOM_SUBJECT_ID_OFFSET) if is_custom else None

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO class_link_rules
                    (school_id, learning_area_id, custom_subject_id,
                     class_a_grade_name, class_a_education_level, class_a_stream,
                     class_b_grade_name, class_b_education_level, class_b_stream)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s);
            """, (school_id, learning_area_id, custom_subject_id, a_grade, a_level, a_stream, b_grade, b_level, b_stream))
            conn.commit()

    return RedirectResponse(url=f"/timetable/class-links/{school_id}", status_code=303)


@router.post("/api/v1/timetable/class-links/delete/{school_id}/{rule_id}")
def delete_class_link_rule(school_id: int, rule_id: int, request: Request):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM class_link_rules WHERE id = %s AND school_id = %s;", (rule_id, school_id))
            conn.commit()

    return RedirectResponse(url=f"/timetable/class-links/{school_id}", status_code=303)


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
                <a href="/timetable/class-links/{school_id}" class="text-xs text-indigo-700 font-bold hover:underline">🔗 Need a subject to run at the exact same time for two specific classes (e.g. so they can combine if a teacher is absent)? Set that up here →</a>
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



# ============================================================
# Grade/Class Timetable View, Test & Generate (single class + whole level)
# ============================================================

@router.get("/timetable/grade/{school_id}", response_class=HTMLResponse)
def timetable_grade_view(school_id: int, request: Request, grade_name: str, education_level: str, stream: str, test_issues: str = None, synced: str = None):
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

            cur.execute("""
                SELECT issues_json FROM timetable_generation_issues
                WHERE school_id = %s AND grade_name = %s AND education_level = %s AND stream = %s;
            """, (school_id, grade_name, education_level, stream))
            issues_row = cur.fetchone()
            # issues_json is either the new {"shortfalls": [...], "relaxed": [...]}
            # shape, or an old bare list from before "relaxed" existed —
            # handle both so nothing breaks for a school whose last
            # generation ran before this change.
            _parsed_issues = json.loads(issues_row['issues_json']) if issues_row else []
            if isinstance(_parsed_issues, dict):
                generation_shortfalls = _parsed_issues.get('shortfalls', [])
                generation_relaxed = _parsed_issues.get('relaxed', [])
            else:
                generation_shortfalls = _parsed_issues
                generation_relaxed = []

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
            import base64
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

    # Persistent, until the next (re)generation clears or replaces it —
    # unlike test_issues above (a one-time query-param message from a
    # single Test & Generate click), this comes from the database, so it
    # keeps showing up every time this page loads until the underlying
    # shortfall is actually resolved and this class is regenerated again.
    if generation_shortfalls:
        shortfall_items = "".join(
            f"<li class='mb-1.5'><b>{esc(sf['subject'])}</b> is short {sf['short_by']} lesson(s) this week — {esc(sf['detail'])}</li>"
            for sf in generation_shortfalls
        )
        test_issues_html += f"""
        <div class="bg-rose-50 border border-rose-200 text-rose-800 text-sm px-4 py-3 rounded-xl mb-3 mx-6 mt-4">
            <p class="font-bold mb-1.5">🎯 This timetable is short of the exact lesson counts configured — {len(generation_shortfalls)} subject(s) affected:</p>
            <ul class="list-disc list-inside space-y-1 text-xs">{shortfall_items}</ul>
            <p class="text-xs mt-2 italic">To fix this, relax whichever rule is named above for the affected subject(s), or reduce another subject's lessons-per-week to free up room, then run Test &amp; Generate again.</p>
        </div>
        """

    if generation_relaxed:
        relaxed_items = "".join(f"<li class='mb-1'>{esc(note)}</li>" for note in generation_relaxed)
        test_issues_html += f"""
        <div class="bg-amber-50 border border-amber-200 text-amber-800 text-sm px-4 py-3 rounded-xl mb-3 mx-6 mt-4">
            <p class="font-bold mb-1.5">⚖️ {len(generation_relaxed)} lesson(s) needed a Same-Day or Consecutive-Forbidden rule relaxed to hit the exact configured count — Subject Time-Off, Teacher Availability, and teacher double-booking were never touched:</p>
            <ul class="list-disc list-inside space-y-1 text-xs">{relaxed_items}</ul>
            <p class="text-xs mt-2 italic">Worth a quick look to confirm each of these is acceptable — if not, adjust that subject's lessons-per-week or the rule involved and regenerate.</p>
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
                <a href="/timetable/dashboard/{school_id}" class="bg-amber-50 hover:bg-amber-100 text-amber-700 border border-amber-200 px-4 py-2 rounded-xl text-xs font-bold transition" title="Generation now only runs per education level or for the whole school — that's the only way teacher/room collisions across classes get checked properly.">🧪 Generate from Timetable Workspace →</a>
                <form action="/api/v1/timetable/sync-teachers/{school_id}" method="post" onsubmit="return confirm('Sync teacher names for {esc(section_label)}? This updates which teacher shows on each already-scheduled subject to match the current Assignments — day/period placement is left exactly as it is.');">
                    <input type="hidden" name="grade_name" value="{esc(grade_name)}">
                    <input type="hidden" name="education_level" value="{esc(education_level)}">
                    <input type="hidden" name="stream" value="{esc(stream)}">
                    <button type="submit" class="bg-indigo-600 hover:bg-indigo-700 text-white px-4 py-2 rounded-xl text-xs font-bold transition shadow-sm">🔄 Sync Teacher Names</button>
                </form>
                <form action="/api/v1/timetable/new/{school_id}" method="post" onsubmit="return confirm('Start a brand-new BLANK timetable for {esc(section_label)}? This clears every period currently scheduled — you\\'ll build it up from scratch by hand. This cannot be undone.');">
                    <input type="hidden" name="grade_name" value="{esc(grade_name)}">
                    <input type="hidden" name="education_level" value="{esc(education_level)}">
                    <input type="hidden" name="stream" value="{esc(stream)}">
                    <button type="submit" class="bg-white border border-slate-200 hover:bg-slate-50 text-slate-600 px-4 py-2 rounded-xl text-xs font-bold transition">＋ New</button>
                </form>
                <a href="/timetable/print/{school_id}?grade_name={encoded_grade}&education_level={encoded_level}&stream={encoded_stream}" target="_blank" class="bg-teal-700 hover:bg-teal-800 text-white px-4 py-2 rounded-xl text-xs font-bold transition">🖨 Print</a>
                <a href="/timetable/assignments/{school_id}?grade_name={encoded_grade}&education_level={encoded_level}&stream={encoded_stream}" class="bg-white border border-slate-200 hover:bg-slate-50 text-slate-600 px-4 py-2 rounded-xl text-xs font-bold transition">Teachers</a>
                <a href="/timetable/dashboard/{school_id}" class="bg-white border border-slate-200 hover:bg-slate-50 text-slate-600 px-4 py-2 rounded-xl text-xs font-bold transition">← Back</a>
            </div>
        </header>
        {"<div class='bg-emerald-50 border border-emerald-200 text-emerald-800 text-sm px-4 py-3 rounded-xl mb-3 mx-6 mt-4'>✅ Teacher names synced from current Assignments — day/period placement was left untouched.</div>" if synced == "ok" else ""}
        {"<div class='bg-amber-50 border border-amber-200 text-amber-800 text-sm px-4 py-3 rounded-xl mb-3 mx-6 mt-4'>⚠️ Nothing to sync — no timetable has been generated for this class yet. Use Test &amp; Generate first.</div>" if synced == "none" else ""}
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
        import base64
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
        import base64
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

            # Level-wide pre-placement of double lessons for teachers shared
            # across multiple classes at this level — resolves the actual,
            # confirmed cross-class bottleneck (a shared teacher's best
            # double-lesson slots getting greedily claimed by whichever
            # class happens to generate first) BEFORE any single class's
            # own generation runs. See _preplace_shared_teacher_doubles for
            # the full rationale. Wrapped defensively — if anything about
            # this enhancement pass goes wrong, whole-level generation must
            # still proceed exactly as it did before this existed, just
            # without the extra cross-class coordination.
            if sections:
                try:
                    days = get_school_days(cur, school_id)
                    teaching_periods = [p for p in get_periods_for_level(cur, school_id, education_level) if p['is_teaching_period']]
                    sorted_teaching = sorted(teaching_periods, key=lambda p: p['period_order'])
                    consecutive_period_pairs = [
                        (sorted_teaching[i], sorted_teaching[i + 1])
                        for i in range(len(sorted_teaching) - 1)
                        if sorted_teaching[i + 1]['period_order'] - sorted_teaching[i]['period_order'] == 1
                    ]
                    cur.execute("SELECT staff_user_id, day_of_week, period_id FROM teacher_availability WHERE school_id = %s AND status = 'not_available';", (school_id,))
                    unavailable = {(r['staff_user_id'], r['day_of_week'], r['period_id']) for r in cur.fetchall()}
                    cur.execute("SELECT learning_area_id, day_of_week, period_id FROM subject_availability WHERE school_id = %s AND status = 'not_available' AND learning_area_id IS NOT NULL;", (school_id,))
                    subject_unavailable = {(r['learning_area_id'], r['day_of_week'], r['period_id']) for r in cur.fetchall()}
                    cur.execute("SELECT custom_subject_id, day_of_week, period_id FROM subject_availability WHERE school_id = %s AND status = 'not_available' AND custom_subject_id IS NOT NULL;", (school_id,))
                    subject_unavailable |= {(r['custom_subject_id'] + CUSTOM_SUBJECT_ID_OFFSET, r['day_of_week'], r['period_id']) for r in cur.fetchall()}

                    preplaced = _preplace_shared_teacher_doubles(cur, school_id, education_level, sections, days, teaching_periods, consecutive_period_pairs, unavailable, subject_unavailable)

                    # Clear any stale pre-placed rows from a previous run
                    # first, so re-running this doesn't accumulate
                    # duplicates from an old, no-longer-relevant pass.
                    for sec in sections:
                        cur.execute(
                            "DELETE FROM timetable_slots WHERE school_id = %s AND grade_name = %s AND education_level = %s AND stream = %s AND is_preplaced = TRUE;",
                            (school_id, sec['grade_name'], education_level, sec['stream'])
                        )
                    for (g, s), placements in preplaced.items():
                        for p in placements:
                            learning_area_id = p['subject_id'] if p['subject_id'] < CUSTOM_SUBJECT_ID_OFFSET else None
                            custom_subject_id = (p['subject_id'] - CUSTOM_SUBJECT_ID_OFFSET) if p['subject_id'] >= CUSTOM_SUBJECT_ID_OFFSET else None
                            for period_id in (p['period1_id'], p['period2_id']):
                                cur.execute("""
                                    INSERT INTO timetable_slots (school_id, grade_name, education_level, stream, day_of_week, period_id, learning_area_id, custom_subject_id, staff_user_id, is_preplaced)
                                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE);
                                """, (school_id, g, education_level, s, p['day'], period_id, learning_area_id, custom_subject_id, p['teacher_id']))
                    conn.commit()
                except Exception as e:
                    conn.rollback()
                    print(f"[timetable level-wide doubles pre-placement] Skipped due to error, falling back to per-class generation only: {e}")

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

    import base64
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


@router.post("/api/v1/timetable/test-and-generate-school/{school_id}")
def test_and_generate_whole_school(school_id: int, request: Request):
    """Runs Test & Generate across EVERY class in EVERY education level,
    one level after another — specifically for a teacher who teaches
    across multiple levels (e.g. Lower Primary and Upper Primary). Doing
    each level in isolation would only ever check conflicts within that
    one level's own generated slots at the time; running the whole school
    in one pass, level by level in sequence, means a teacher's Lower
    Primary bookings are already committed and visible by the time Upper
    Primary is generated — and the final collision check now compares by
    real clock-time overlap, not raw period_id, so a cross-level conflict
    at the same actual time is caught even though each level has its own
    separate period rows."""
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    all_class_results = []
    for education_level in EDUCATION_LEVELS:
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

        for sec in sections:
            grade_name, stream = sec['grade_name'], sec['stream']
            with get_db_connection() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    errors, warnings = validate_timetable_setup(cur, school_id, grade_name, education_level, stream)

            if errors:
                all_class_results.append({'grade_name': grade_name, 'stream': stream, 'education_level': education_level, 'status': 'skipped', 'errors': errors, 'warnings': warnings})
                continue

            generate_draft_timetable(school_id, request, grade_name, education_level, stream)
            all_class_results.append({'grade_name': grade_name, 'stream': stream, 'education_level': education_level, 'status': 'generated', 'errors': [], 'warnings': warnings})

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # No education_level filter — scans across the WHOLE school,
            # which is exactly what catches a teacher double-booked between
            # two different levels at the same real time.
            collisions = _find_timetable_collisions(cur, school_id, None)

    import base64
    payload = base64.b64encode(json.dumps({
        'education_level': 'Whole School',
        'class_results': all_class_results,
        'collision_count': len(collisions),
        'collisions': [
            {
                'teacher': (slots[0]['full_name'] or slots[0]['email'] or 'Unknown teacher'),
                'day': slots[0]['day_of_week'],
                'period': slots[0]['period_label'],
                'classes': [f"{s['grade_name']} — {s['stream']} ({s['education_level']}) — {s['subject_name'] or 'Unknown subject'}" for s in slots],
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

    import base64
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
        row_level = r.get('education_level', education_level)
        encoded_grade = urllib.parse.quote(r['grade_name'])
        encoded_stream = urllib.parse.quote(r['stream'])
        encoded_level = urllib.parse.quote(row_level)
        if r['status'] == 'generated':
            status_badge = "<span class='text-[10px] font-bold px-2 py-0.5 rounded-full bg-emerald-50 text-emerald-700 border border-emerald-200'>✅ Generated</span>"
            detail_html = "".join(f"<li class='text-amber-700'>⚠️ {esc(w)}</li>" for w in r['warnings'])
        else:
            status_badge = "<span class='text-[10px] font-bold px-2 py-0.5 rounded-full bg-rose-50 text-rose-700 border border-rose-200'>❌ Skipped</span>"
            detail_html = "".join(f"<li class='text-rose-700'>{esc(e)}</li>" for e in r['errors'])
        class_rows_html += f"""
        <div class="bg-white rounded-2xl border shadow-xs p-4 mb-3">
            <div class="flex items-center justify-between">
                <a href="/timetable/grade/{school_id}?grade_name={encoded_grade}&education_level={encoded_level}&stream={encoded_stream}" class="text-sm font-bold text-slate-800 hover:underline">{esc(section_label)}{' <span class=\"text-slate-400 font-normal\">(' + esc(row_level) + ')</span>' if education_level == 'Whole School' else ''}</a>
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



# ============================================================
# Generation Engine — the hardened, tested placement algorithm
# (Phase 1 locked placements, Phase 2 doubles, Phase 3 fill-remaining
# with conflict-avoidance-first candidate selection)
# ============================================================

def _preplace_shared_teacher_doubles(cur, school_id: int, education_level: str, sections: list, days: list, teaching_periods: list, consecutive_period_pairs: list, unavailable: set, subject_unavailable: set) -> dict:
    """Resolves double-lesson placement for teachers shared across MULTIPLE
    classes at this education level, BEFORE any single class's own
    generation begins.

    Without this, whole-level generation runs class-by-class in a plain
    loop — a teacher's best double-lesson slots can get greedily claimed
    by whichever class happens to generate first, leaving a later class
    unable to find any valid double slot for that same teacher, even
    when a different arrangement across both classes would have worked.
    This is the actual, confirmed cause behind "low ability to handle
    complex teacher lesson allocation... for an entire educational level."

    Deliberately uses a first-fit heuristic (not a full joint backtracking
    search across every shared teacher's every class at once) — a much
    simpler, lower-risk piece of code to get right on something this
    central, and it doesn't need to be perfect on its own: whatever it
    can't resolve here still falls through to the existing, already
    well-tested per-class Phase 2 backtracking search, which picks up
    exactly where this leaves off using the correctly-reduced remaining
    count. A partial win here is still a real win; this is a pure
    enhancement layered on top of proven logic, not a replacement for it.

    Returns: {(grade_name, stream): [ {subject_id, day, period1_id,
    period2_id, teacher_id}, ... ]}
    """
    cur.execute("""
        SELECT grade_name, stream, learning_area_id, custom_subject_id, staff_user_id, double_lessons_count
        FROM teacher_subject_assignments
        WHERE school_id = %s AND education_level = %s AND double_lessons_count > 0 AND staff_user_id IS NOT NULL;
    """, (school_id, education_level))
    rows = cur.fetchall()

    section_set = {(s['grade_name'], s['stream']) for s in sections}

    items = []
    for r in rows:
        key = (r['grade_name'], r['stream'])
        if key not in section_set:
            continue
        sid = (r['custom_subject_id'] + CUSTOM_SUBJECT_ID_OFFSET) if r['custom_subject_id'] else r['learning_area_id']
        if sid is None:
            continue
        items.append({
            'grade_name': r['grade_name'], 'stream': r['stream'], 'subject_id': sid,
            'teacher_id': r['staff_user_id'], 'needed': r['double_lessons_count'],
        })

    # Only teachers genuinely shared across 2+ DIFFERENT classes at this
    # level need this pass — a teacher teaching doubles in just one class
    # has no cross-class conflict to resolve; the per-class search
    # already handles that correctly on its own, so leaving them alone
    # here keeps this pass focused only on the actual bottleneck.
    teacher_class_counts = {}
    for item in items:
        teacher_class_counts.setdefault(item['teacher_id'], set()).add((item['grade_name'], item['stream']))
    shared_teacher_ids = {t for t, classes in teacher_class_counts.items() if len(classes) >= 2}
    items = [item for item in items if item['teacher_id'] in shared_teacher_ids]

    if not items:
        return {}

    items_by_teacher = {}
    for item in items:
        items_by_teacher.setdefault(item['teacher_id'], []).append(item)

    result = {}
    teacher_slot_used = set()  # (teacher_id, day, period_id) claimed by THIS pass so far

    # Tightest-bottleneck-first: a teacher needing doubles across the most
    # classes has the most total slots to fit into one shared weekly
    # schedule, so they're resolved first — the same "most constrained
    # first" principle already used inside a single class's own doubles
    # search, just applied one level up.
    for teacher_id in sorted(items_by_teacher.keys(), key=lambda t: -len(items_by_teacher[t])):
        for item in items_by_teacher[teacher_id]:
            sid = item['subject_id']
            still_needed = item['needed']
            placed_for_this_item = []
            days_used_by_this_item = set()

            for day in days:
                if still_needed <= 0:
                    break
                if day in days_used_by_this_item:
                    continue
                for p1, p2 in consecutive_period_pairs:
                    if (teacher_id, day, p1['id']) in teacher_slot_used or (teacher_id, day, p2['id']) in teacher_slot_used:
                        continue
                    if (teacher_id, day, p1['id']) in unavailable or (teacher_id, day, p2['id']) in unavailable:
                        continue
                    if (sid, day, p1['id']) in subject_unavailable or (sid, day, p2['id']) in subject_unavailable:
                        continue
                    teacher_slot_used.add((teacher_id, day, p1['id']))
                    teacher_slot_used.add((teacher_id, day, p2['id']))
                    days_used_by_this_item.add(day)
                    placed_for_this_item.append({'subject_id': sid, 'day': day, 'period1_id': p1['id'], 'period2_id': p2['id'], 'teacher_id': teacher_id})
                    still_needed -= 1
                    break

            if placed_for_this_item:
                key = (item['grade_name'], item['stream'])
                result.setdefault(key, []).extend(placed_for_this_item)

    return result


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

            # Custom subjects (a school-specific split like Music/Art/PE out
            # of "Creative Arts and Sports", or a non-examinable subject
            # like PPI) are folded into the SAME scheduling queue as regular
            # subjects, using a large id offset (CUSTOM_SUBJECT_ID_OFFSET,
            # defined once at module level) — this guarantees zero
            # collision with real learning_area ids (a school will never
            # have a million subjects), so every existing dict/set keyed by
            # subject id keeps working completely unchanged. Only _place()
            # needs to know about the offset, to route the INSERT to
            # custom_subject_id instead of learning_area_id.
            cur.execute("SELECT id, name FROM timetable_custom_subjects WHERE school_id = %s AND education_level = %s ORDER BY name ASC;", (school_id, education_level))
            custom_subjects_raw = cur.fetchall()
            custom_subjects = [{'id': cs['id'] + CUSTOM_SUBJECT_ID_OFFSET, 'name': cs['name']} for cs in custom_subjects_raw]
            subjects = subjects + custom_subjects

            cur.execute("""
                SELECT learning_area_id, staff_user_id, lessons_per_week, requires_double, double_lessons_count FROM teacher_subject_assignments
                WHERE school_id = %s AND grade_name = %s AND education_level = %s AND stream = %s AND learning_area_id IS NOT NULL;
            """, (school_id, grade_name, education_level, stream))
            assignment_rows = cur.fetchall()
            teacher_for_subject = {r['learning_area_id']: r['staff_user_id'] for r in assignment_rows}
            lessons_per_week_for_subject = {r['learning_area_id']: r['lessons_per_week'] for r in assignment_rows}
            # double_lessons_count is the real source of truth (how MANY
            # double lessons per week, entered manually) — requires_double
            # is only kept around for any other code path that might still
            # read the old boolean; it's always kept in sync as
            # double_lessons_count > 0 wherever this is saved.
            double_count_for_subject = {r['learning_area_id']: (r['double_lessons_count'] or 0) for r in assignment_rows}

            cur.execute("""
                SELECT custom_subject_id, staff_user_id, lessons_per_week, requires_double, double_lessons_count FROM teacher_subject_assignments
                WHERE school_id = %s AND grade_name = %s AND education_level = %s AND stream = %s AND custom_subject_id IS NOT NULL;
            """, (school_id, grade_name, education_level, stream))
            for r in cur.fetchall():
                offset_id = r['custom_subject_id'] + CUSTOM_SUBJECT_ID_OFFSET
                teacher_for_subject[offset_id] = r['staff_user_id']
                lessons_per_week_for_subject[offset_id] = r['lessons_per_week']
                double_count_for_subject[offset_id] = (r['double_lessons_count'] or 0)

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

            # Subject "time off" — a subject marked "not_available" at a slot
            # is a hard block; "conditional" is a soft preference to avoid.
            # Covers both regular subjects and custom ones (offset id).
            # Computed BEFORE locked placements below, specifically so a
            # sync rule can be checked against it — a "same time every
            # week" rule must never override an explicit "Not Available"
            # for that exact subject/slot; the page itself promises this
            # is a hard block in every phase, not just the fill-remaining one.
            cur.execute("""
                SELECT learning_area_id, day_of_week, period_id, status FROM subject_availability
                WHERE school_id = %s AND status IN ('not_available', 'conditional') AND learning_area_id IS NOT NULL;
            """, (school_id,))
            subject_unavailable, subject_conditional = set(), set()
            for r in cur.fetchall():
                key = (r['learning_area_id'], r['day_of_week'], r['period_id'])
                (subject_unavailable if r['status'] == 'not_available' else subject_conditional).add(key)

            cur.execute("""
                SELECT custom_subject_id, day_of_week, period_id, status FROM subject_availability
                WHERE school_id = %s AND status IN ('not_available', 'conditional') AND custom_subject_id IS NOT NULL;
            """, (school_id,))
            for r in cur.fetchall():
                key = (r['custom_subject_id'] + CUSTOM_SUBJECT_ID_OFFSET, r['day_of_week'], r['period_id'])
                (subject_unavailable if r['status'] == 'not_available' else subject_conditional).add(key)

            locked_placements = {}
            free_subjects = []
            for subj in subjects:
                # A subject explicitly configured with 0 lessons/week must
                # never be scheduled at all, in any phase — including via a
                # "same time every week" sync rule, which otherwise ignores
                # lessons_per_week entirely.
                if lessons_per_week_for_subject.get(subj['id']) == 0:
                    continue
                rule = sync_rules.get(subj['id'])
                if rule and rule[0] in days and rule[1] in teaching_period_ids and (subj['id'], rule[0], rule[1]) not in subject_unavailable:
                    locked_placements[(rule[0], rule[1])] = subj
                else:
                    free_subjects.append(subj)
            if not free_subjects:
                # Safety net: never leave the queue completely empty (e.g.
                # if every subject happens to have a sync rule) — but still
                # never includes a subject explicitly set to 0 lessons/week.
                free_subjects = [s for s in subjects if lessons_per_week_for_subject.get(s['id']) != 0]

            # Track which teacher is already booked at each (day, period) —
            # compared by ACTUAL CLOCK TIME OVERLAP, not raw period_id.
            # This matters specifically for a teacher who teaches across
            # multiple education levels: Lower Primary's "Period 1" and
            # Upper Primary's "Period 1" are separate database rows with
            # different ids, even when they run the exact same real-world
            # time — so comparing by period_id alone would miss a teacher
            # genuinely double-booked across two levels at the same clock
            # time. Comparing by parsed start/end time catches this
            # correctly regardless of which level either booking is in.
            def _parse_time_to_minutes(time_str):
                if not time_str:
                    return None
                cleaned = time_str.strip().upper().replace(".", "")
                is_pm = "PM" in cleaned
                is_am = "AM" in cleaned
                cleaned = cleaned.replace("AM", "").replace("PM", "").strip()
                parts = cleaned.replace(".", ":").split(":")
                try:
                    hour = int(parts[0])
                    minute = int(parts[1]) if len(parts) > 1 else 0
                except (ValueError, IndexError):
                    return None
                if is_pm and hour < 12:
                    hour += 12
                if is_am and hour == 12:
                    hour = 0
                return hour * 60 + minute

            def _time_ranges_overlap(a_start, a_end, b_start, b_end):
                if None in (a_start, a_end, b_start, b_end):
                    return False
                return a_start < b_end and b_start < a_end

            this_level_period_times = {
                p['id']: (_parse_time_to_minutes(p['start_time']), _parse_time_to_minutes(p['end_time']))
                for p in teaching_periods
            }

            cur.execute("""
                SELECT ts.day_of_week, ts.staff_user_id, tp.start_time, tp.end_time
                FROM timetable_slots ts
                JOIN timetable_periods tp ON ts.period_id = tp.id
                WHERE ts.school_id = %s AND ts.staff_user_id IS NOT NULL
                  AND NOT (ts.grade_name = %s AND ts.education_level = %s AND ts.stream = %s);
            """, (school_id, grade_name, education_level, stream))
            booked = {}
            for r in cur.fetchall():
                other_start = _parse_time_to_minutes(r['start_time'])
                other_end = _parse_time_to_minutes(r['end_time'])
                for this_period_id, (this_start, this_end) in this_level_period_times.items():
                    if _time_ranges_overlap(this_start, this_end, other_start, other_end):
                        booked.setdefault((r['day_of_week'], this_period_id), set()).add(r['staff_user_id'])

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

            # Clear this section's existing plan before laying down the new
            # one — EXCEPT any rows the level-wide shared-teacher doubles
            # pass already placed (is_preplaced = TRUE), which get loaded
            # back in below instead of being wiped and rediscovered from
            # scratch. A single-class "Generate for this class only" run
            # never has any such rows to begin with, so this preserves
            # the exact same delete-everything behavior it always had.
            cur.execute("DELETE FROM timetable_slots WHERE school_id = %s AND grade_name = %s AND education_level = %s AND stream = %s AND is_preplaced IS NOT TRUE;", (school_id, grade_name, education_level, stream))

            # How many lessons each subject still needs this week — defaults
            # to 1 for any subject without an explicit assignment configured.
            # A subject the admin never configured at all defaults to 1
            # lesson/week (a reasonable assumption for something nobody's
            # touched yet) — but a subject explicitly set to 0 lessons/week
            # means "don't schedule this for this class" and must be
            # respected exactly as 0, not silently forced back up to 1.
            #
            # Initialized for EVERY subject (both free_subjects AND locked
            # ones in locked_placements) — not just free_subjects. A locked
            # (sync-rule) subject still needs a real entry here: _place()
            # unconditionally decrements remaining[subject['id']], so a
            # subject missing from this dict would raise a KeyError the
            # first time a Same-Time Subject Rule actually got used.
            remaining = {}
            for subj in subjects:
                if lessons_per_week_for_subject.get(subj['id']) == 0:
                    continue
                configured = lessons_per_week_for_subject.get(subj['id'])
                remaining[subj['id']] = 1 if configured is None else max(0, configured)
            filled = {}       # (day, period_id) -> subject already placed there
            used_today_by_day = {day: set() for day in days}
            last_subject_by_day = {day: None for day in days}

            # Load back any rows the level-wide pre-pass placed for this
            # exact class — reserving their slots (filled, booked, used-
            # today) and reducing how many MORE lessons that subject still
            # needs, so nothing downstream tries to re-place work that's
            # already done. For a class with no pre-placed rows (the
            # normal case, and always true for single-class generation),
            # this query simply returns nothing and changes nothing.
            cur.execute("""
                SELECT day_of_week, period_id, learning_area_id, custom_subject_id, staff_user_id
                FROM timetable_slots
                WHERE school_id = %s AND grade_name = %s AND education_level = %s AND stream = %s AND is_preplaced = TRUE;
            """, (school_id, grade_name, education_level, stream))
            for r in cur.fetchall():
                preplaced_sid = (r['custom_subject_id'] + CUSTOM_SUBJECT_ID_OFFSET) if r['custom_subject_id'] else r['learning_area_id']
                filled[(r['day_of_week'], r['period_id'])] = preplaced_sid
                booked.setdefault((r['day_of_week'], r['period_id']), set()).add(r['staff_user_id'])
                used_today_by_day.setdefault(r['day_of_week'], set()).add(preplaced_sid)
                if preplaced_sid in remaining:
                    remaining[preplaced_sid] = max(0, remaining[preplaced_sid] - 1)

            def _place(day, period_id, subject, teacher):
                is_custom = subject['id'] >= CUSTOM_SUBJECT_ID_OFFSET
                real_learning_area_id = None if is_custom else subject['id']
                real_custom_subject_id = (subject['id'] - CUSTOM_SUBJECT_ID_OFFSET) if is_custom else None
                cur.execute("""
                    INSERT INTO timetable_slots (school_id, grade_name, education_level, stream, day_of_week, period_id, learning_area_id, custom_subject_id, staff_user_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s);
                """, (school_id, grade_name, education_level, stream, day, period_id, real_learning_area_id, real_custom_subject_id, teacher))
                filled[(day, period_id)] = subject
                if teacher:
                    booked.setdefault((day, period_id), set()).add(teacher)
                used_today_by_day[day].add(subject['id'])
                remaining[subject['id']] -= 1

            # How many slots in the WHOLE week is each subject actually
            # allowed in, given its own Subject Time-Off? Needed here
            # (earlier than before) specifically for Phase 0 below.
            total_week_slots = [(d, p['id']) for d in days for p in teaching_periods]
            valid_slot_count = {
                subj['id']: sum(1 for (d, pid) in total_week_slots if (subj['id'], d, pid) not in subject_unavailable)
                for subj in free_subjects
            }

            # --- Phase -1: Linked Classes — e.g. Mathematics for Grade 8V
            # and Grade 8J must land at the exact same day/period as each
            # other, so one teacher can combine both classes into one room
            # if a colleague is absent. This is the highest-priority
            # placement of all, running even before the zero-slack Phase 0
            # below — it's an explicit, deliberate pairing an admin set up
            # on purpose, not a general scheduling preference to balance
            # against others.
            #
            # The two linked classes can be in different education levels
            # with entirely different bell schedules (this is Francis's
            # actual example: Grade 9V and Grade 8J) — matching is done by
            # real clock time via this_level_period_times/_time_ranges_
            # overlap (already computed above for cross-level teacher
            # conflicts), never by period_id, since two different levels'
            # period ids are unrelated database rows even when they cover
            # the exact same time of day.
            #
            # Whichever of the two linked classes is generated FIRST sets
            # the time; regenerating the other one looks up whatever it
            # already has and matches it. If the partner class hasn't been
            # generated yet, this class schedules normally for now — the
            # link takes effect the next time the OTHER class is
            # (re)generated and looks back at this one. That's an inherent
            # limit of generating one class at a time rather than the
            # whole school in one pass; if a link ever drifts apart,
            # regenerating both linked classes again (in either order)
            # re-syncs them.
            cur.execute("""
                SELECT learning_area_id, custom_subject_id,
                       class_a_grade_name, class_a_education_level, class_a_stream,
                       class_b_grade_name, class_b_education_level, class_b_stream
                FROM class_link_rules
                WHERE school_id = %s
                  AND (
                    (class_a_grade_name = %s AND class_a_education_level = %s AND class_a_stream = %s)
                    OR
                    (class_b_grade_name = %s AND class_b_education_level = %s AND class_b_stream = %s)
                  );
            """, (school_id, grade_name, education_level, stream, grade_name, education_level, stream))
            link_rules_for_me = cur.fetchall()

            for rule in link_rules_for_me:
                my_subject_id = rule['learning_area_id'] if rule['learning_area_id'] is not None else (rule['custom_subject_id'] + CUSTOM_SUBJECT_ID_OFFSET)
                if remaining.get(my_subject_id, 0) <= 0:
                    continue  # this subject doesn't apply to my own class at all, or its quota is already 0

                is_a = (rule['class_a_grade_name'], rule['class_a_education_level'], rule['class_a_stream']) == (grade_name, education_level, stream)
                if is_a:
                    partner_grade, partner_level, partner_stream = rule['class_b_grade_name'], rule['class_b_education_level'], rule['class_b_stream']
                else:
                    partner_grade, partner_level, partner_stream = rule['class_a_grade_name'], rule['class_a_education_level'], rule['class_a_stream']

                partner_periods = get_periods_for_level(cur, school_id, partner_level)
                partner_period_times = {
                    p['id']: (_parse_time_to_minutes(p['start_time']), _parse_time_to_minutes(p['end_time']))
                    for p in partner_periods
                }

                cur.execute("""
                    SELECT day_of_week, period_id FROM timetable_slots
                    WHERE school_id = %s AND grade_name = %s AND education_level = %s AND stream = %s
                      AND (learning_area_id = %s OR custom_subject_id = %s)
                    ORDER BY day_of_week, period_id;
                """, (school_id, partner_grade, partner_level, partner_stream,
                      rule['learning_area_id'], rule['custom_subject_id']))
                partner_slots = cur.fetchall()

                subj_obj = next((s for s in subjects if s['id'] == my_subject_id), None)
                if subj_obj is None:
                    continue

                for pslot in partner_slots:
                    if remaining.get(my_subject_id, 0) <= 0:
                        break  # my own weekly quota for this subject is already fully placed

                    partner_start, partner_end = partner_period_times.get(pslot['period_id'], (None, None))
                    if partner_start is None:
                        continue  # partner's period record is missing/malformed — skip just this one occurrence

                    # Find MY OWN period, whose clock time overlaps the
                    # partner's slot (not necessarily the same period_id).
                    my_period_id = None
                    for pid, (my_start, my_end) in this_level_period_times.items():
                        if pid in teaching_period_ids and _time_ranges_overlap(my_start, my_end, partner_start, partner_end):
                            my_period_id = pid
                            break
                    if my_period_id is None:
                        continue  # no matching time-of-day period exists in my own level for this occurrence

                    day = pslot['day_of_week']
                    if day not in days or (day, my_period_id) in filled:
                        continue  # something else already holds this slot for me — leave this occurrence unsynced

                    chosen_teacher = teacher_for_subject.get(my_subject_id)
                    if chosen_teacher and chosen_teacher in booked.get((day, my_period_id), set()):
                        chosen_teacher = None  # conflict — place anyway, teacherless, flagged for manual fix same as elsewhere

                    _place(day, my_period_id, subj_obj, chosen_teacher)
                    last_subject_by_day[day] = my_subject_id

            # --- Phase 0: a subject with ZERO SLACK — exactly as many valid
            # slots in the whole week as lessons it still needs, like PPI
            # allowed in only one slot and needing exactly one lesson —
            # must claim every one of its valid slots FIRST, before Phase 1
            # (sync-rule locks) or Phase 2 (double lessons) can accidentally
            # claim that same slot for a completely different, more
            # flexible subject. Those later phases have no awareness of
            # any other subject's constraints when grabbing their slots;
            # this is what stops a zero-slack subject from silently losing
            # its only chance and ending up unscheduled for the whole week. ---
            for subj in free_subjects:
                sid = subj['id']
                needed = remaining.get(sid, 0)
                if needed <= 0 or valid_slot_count.get(sid, 999) > needed:
                    continue
                for day in days:
                    if remaining.get(sid, 0) <= 0:
                        break
                    for period in teaching_periods:
                        if remaining.get(sid, 0) <= 0:
                            break
                        if (day, period['id']) in filled:
                            continue
                        if (sid, day, period['id']) in subject_unavailable:
                            continue
                        chosen_teacher = teacher_for_subject.get(sid)
                        if chosen_teacher and chosen_teacher in booked.get((day, period['id']), set()):
                            chosen_teacher = None
                        _place(day, period['id'], subj, chosen_teacher)
                        last_subject_by_day[day] = sid

            # --- Phase 1: place locked "same time" subjects first — these
            # always win their slot outright, not drawn from the queue.
            # _place() already decrements remaining[] by exactly 1 for this
            # placement — no separate manual decrement needed (an earlier
            # version double-counted this, silently under-allocating any
            # subject that also had a lessons_per_week > 1 configured
            # alongside its Same-Time rule). ---
            for (day, period_id), locked_subject in locked_placements.items():
                if day not in days or period_id not in teaching_period_ids:
                    continue
                lcid = locked_subject['id']
                chosen_teacher = teacher_for_subject.get(lcid)
                if chosen_teacher and chosen_teacher in booked.get((day, period_id), set()):
                    chosen_teacher = None  # conflict — place subject anyway, flagged for manual fix
                _place(day, period_id, locked_subject, chosen_teacher)
                last_subject_by_day[day] = lcid

            # --- Phase 2: subjects needing double lessons (e.g. for
            # practicals) get their genuine back-to-back pairs placed
            # first, before anything else competes for those slots. This
            # is a real backtracking search across ALL subjects needing
            # doubles at once — not the naive "handle one subject fully,
            # then move to the next" approach a first version of this had,
            # which could fail to find a valid arrangement even when one
            # existed (classic greedy-scheduling failure: the first
            # subject processed grabs whichever slots it finds first,
            # potentially leaving a later subject with no clean day left,
            # even though a different combination would have fit everyone).
            # With multiple subjects each needing multiple doubles per
            # week (e.g. Math ×2, English ×2, CRE ×1), this is exactly the
            # case that needs genuine search rather than first-fit.
            subjects_needing_doubles = [
                (subj, double_count_for_subject.get(subj['id'], 0))
                for subj in free_subjects
                if double_count_for_subject.get(subj['id'], 0) > 0 and remaining.get(subj['id'], 0) >= 2
            ]

            if subjects_needing_doubles:
                # Candidate (day, p1_id, p2_id) slots for each subject,
                # computed once up front — every hard constraint EXCEPT
                # cross-subject collisions (which the search resolves
                # dynamically, since two different subjects' doubles can't
                # both claim the same periods on the same day).
                double_candidates = {}
                for subj, needed in subjects_needing_doubles:
                    sid = subj['id']
                    cand_teacher = teacher_for_subject.get(sid)
                    options = []
                    for day in days:
                        if sid in used_today_by_day[day]:
                            continue  # already has a lesson today from an earlier phase — keep doubles on their own day
                        for p1, p2 in consecutive_period_pairs:
                            if (day, p1['id']) in filled or (day, p2['id']) in filled:
                                continue
                            if (sid, day, p1['id']) in subject_unavailable or (sid, day, p2['id']) in subject_unavailable:
                                continue
                            if cand_teacher and (
                                (cand_teacher, day, p1['id']) in unavailable or (cand_teacher, day, p2['id']) in unavailable
                                or cand_teacher in booked.get((day, p1['id']), set()) or cand_teacher in booked.get((day, p2['id']), set())
                            ):
                                continue
                            options.append((day, p1['id'], p2['id']))
                    double_candidates[sid] = options

                # Most-constrained-subject-first ordering (fewest candidate
                # slots tried first) — a standard, well-proven heuristic
                # that makes backtracking search dramatically faster by
                # resolving the tightest constraints before the looser ones.
                needed_map = {subj['id']: needed for subj, needed in subjects_needing_doubles}
                subj_by_id = {subj['id']: subj for subj, needed in subjects_needing_doubles}
                order = sorted(needed_map.keys(), key=lambda sid: len(double_candidates[sid]))

                assignment = {sid: [] for sid in order}
                used_slot_globally = set()  # (day, period_id) already claimed by some subject's double this search
                search_steps = [0]
                STEP_CAP = 200_000  # defensive cap — real school timetables are nowhere near this; exists purely so a pathological edge case degrades gracefully instead of hanging

                def _try_pick(sid, still_needed, options, start_idx, days_used_by_this_subject, next_i):
                    if search_steps[0] > STEP_CAP:
                        return False
                    search_steps[0] += 1
                    if still_needed == 0:
                        return _backtrack(next_i)
                    for j in range(start_idx, len(options)):
                        day, p1_id, p2_id = options[j]
                        if day in days_used_by_this_subject:
                            continue
                        if (day, p1_id) in used_slot_globally or (day, p2_id) in used_slot_globally:
                            continue
                        assignment[sid].append((day, p1_id, p2_id))
                        used_slot_globally.add((day, p1_id))
                        used_slot_globally.add((day, p2_id))
                        days_used_by_this_subject.add(day)
                        if _try_pick(sid, still_needed - 1, options, j + 1, days_used_by_this_subject, next_i):
                            return True
                        assignment[sid].pop()
                        used_slot_globally.discard((day, p1_id))
                        used_slot_globally.discard((day, p2_id))
                        days_used_by_this_subject.discard(day)
                    return False

                def _backtrack(i):
                    if i == len(order):
                        return True
                    sid = order[i]
                    return _try_pick(sid, needed_map[sid], double_candidates[sid], 0, set(), i + 1)

                found_full_solution = _backtrack(0)

                if found_full_solution:
                    for sid, slots in assignment.items():
                        subj = subj_by_id[sid]
                        cand_teacher = teacher_for_subject.get(sid)
                        for day, p1_id, p2_id in slots:
                            _place(day, p1_id, subj, cand_teacher)
                            _place(day, p2_id, subj, cand_teacher)
                            last_subject_by_day[day] = sid
                else:
                    # No arrangement satisfies every subject's doubles at
                    # once (a genuinely over-constrained request, or the
                    # step cap was hit on an unusually large setup) — fall
                    # back to placing as many as fit, most-constrained
                    # subject first, so the class still gets a usable
                    # timetable instead of zero doubles. Whatever doesn't
                    # fit shows up in the shortfall diagnostics below,
                    # naming exactly which subject and how many lessons
                    # short — same as any other placement shortfall.
                    for sid in order:
                        subj = subj_by_id[sid]
                        cand_teacher = teacher_for_subject.get(sid)
                        placed_for_this_subject = 0
                        days_used = set()
                        for day, p1_id, p2_id in double_candidates[sid]:
                            if placed_for_this_subject >= needed_map[sid]:
                                break
                            if day in days_used:
                                continue
                            if (day, p1_id) in filled or (day, p2_id) in filled:
                                continue
                            _place(day, p1_id, subj, cand_teacher)
                            _place(day, p2_id, subj, cand_teacher)
                            last_subject_by_day[day] = sid
                            days_used.add(day)
                            placed_for_this_subject += 1
                # Whatever's left of any subject's quota after its doubles
                # (whether from the full solution or the fallback) simply
                # falls through to Phase 3 as ordinary single lessons.

            # --- Phase 3: fill remaining empty slots with each subject's
            # remaining single lessons, up to its weekly quota — once a
            # subject's quota is used up it drops out, and once every
            # subject's quota is used up, any leftover slots simply stay
            # free rather than being force-filled. ---
            # (valid_slot_count already computed earlier, for Phase 0.)
            #
            # Split into two sub-passes across the WHOLE week, rather than
            # one single pass — this is what actually fixes a real,
            # confirmed bug: a teacher shared across two classes (e.g.
            # teaching the same subjects in both Grade 8 and Grade 9)
            # would have their subject's weekly quota "used up" on early
            # days via teacherless placements, purely because that's
            # whichever day the loop reached first while every remaining
            # subject in the queue happened to share that busy teacher —
            # even when a later day in the same week was actually
            # completely free for that exact teacher. Verified directly
            # against a real Postgres instance: a shared teacher's second
            # class ended up with 10 teacherless slots concentrated on
            # Monday–Thursday while Friday sat almost entirely empty.
            # Pass A gives every subject a genuine shot at a clean slot
            # ANYWHERE in the week first; only Pass B, once the whole week
            # has had that fair pass, accepts a teacher conflict as a
            # last resort — exactly the same fallback logic as before,
            # just deferred until every slot has had first-round priority.
            all_week_slots = [(day, period) for day in days for period in teaching_periods if (day, period['id']) not in filled]

            def _try_fill_slot(day, period, allow_teacher_conflict):
                nonlocal qi, queue
                if (day, period['id']) in filled or not queue:
                    return False

                used_today = used_today_by_day[day]
                last_subject_id = last_subject_by_day[day]
                conflict_levels = ((True, True), (True, False), (False, True), (False, False)) if allow_teacher_conflict else ((True, True), (False, True))

                chosen_idx, chosen_subject, chosen_teacher = None, None, None
                for avoid_conditional, avoid_teacher_conflict in conflict_levels:
                    if chosen_subject is not None:
                        break
                    priority_order = sorted(
                        range(len(queue)),
                        key=lambda idx: (valid_slot_count.get(queue[idx]['id'], 999), (idx - qi) % len(queue))
                    )
                    for idx in priority_order:
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
                        if cand_teacher and cand_teacher in booked.get((day, period['id']), set()):
                            if avoid_teacher_conflict:
                                continue  # try a different candidate first rather than nulling this one's teacher
                            cand_teacher = None

                        chosen_idx, chosen_subject, chosen_teacher = idx, candidate, cand_teacher
                        break

                if chosen_subject is None and not allow_teacher_conflict:
                    return False  # Pass A: no clean candidate here — leave it for Pass B, don't force anything

                if chosen_subject is None:
                    # Last-resort round-robin fallback (Pass B only) — this
                    # must NEVER mean ignoring a subject's own "Not
                    # Available" time-off or the same-day/consecutive-day
                    # rules — those are hard, non-negotiable blocks, not
                    # preferences to relax. Only teacher-related soft
                    # conflicts get relaxed here; if genuinely every
                    # subject in the queue is hard-blocked at this exact
                    # slot, it's left empty rather than violating one of them.
                    priority_order = sorted(
                        range(len(queue)),
                        key=lambda idx: (valid_slot_count.get(queue[idx]['id'], 999), (idx - qi) % len(queue))
                    )
                    for idx in priority_order:
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
                        chosen_idx, chosen_subject = idx, candidate
                        break

                if chosen_subject is None:
                    return False  # every remaining subject is hard-blocked at this exact slot — leave it empty

                chosen_teacher = teacher_for_subject.get(chosen_subject['id'])
                if chosen_teacher and (
                    chosen_teacher in booked.get((day, period['id']), set())
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
                return True

            queue = [subj for subj in free_subjects for _ in range(remaining.get(subj['id'], 0))]
            qi = 0

            # Pass A — entire week, clean placements only.
            for day, period in all_week_slots:
                if not queue:
                    break
                _try_fill_slot(day, period, allow_teacher_conflict=False)

            # Pass B — entire week again, now accepting teacher conflicts
            # as a last resort for whatever's still unplaced.
            for day, period in all_week_slots:
                if not queue:
                    break
                _try_fill_slot(day, period, allow_teacher_conflict=True)

            # --- Pass C: genuinely last resort — a subject still short of
            # its exact configured weekly quota after Pass A and B may
            # repeat on a day it's already scheduled. This is exactly the
            # real shortfall a tight week with multiple doubles creates:
            # e.g. English's 2 configured doubles use up 2 of the 5 days,
            # and if the other 3 days end up completely filled by other
            # subjects before English's 5th (single) lesson gets a turn,
            # the ONLY way left to reach its exact configured count is a
            # second lesson on a day it already has one. Subject Time-Off
            # and the Same-Day/Consecutive-Forbidden rules stay hard,
            # non-negotiable blocks even here — only "not twice in a day"
            # is being relaxed, and only for a subject still short after
            # every fairer option has already been tried. The repeat is
            # never placed immediately next to that subject's existing
            # lesson that day, so this can never accidentally create an
            # unconfigured double lesson.
            period_index_by_id = {p['id']: idx for idx, p in enumerate(teaching_periods)}

            def _adjacent_period_ids(period_id):
                idx = period_index_by_id.get(period_id)
                if idx is None:
                    return []
                neighbors = []
                if idx > 0:
                    neighbors.append(teaching_periods[idx - 1]['id'])
                if idx < len(teaching_periods) - 1:
                    neighbors.append(teaching_periods[idx + 1]['id'])
                return neighbors

            for day, period in all_week_slots:
                if (day, period['id']) in filled:
                    continue
                still_short = [s for s in free_subjects if remaining.get(s['id'], 0) > 0]
                if not still_short:
                    continue
                used_today = used_today_by_day[day]
                last_subject_id = last_subject_by_day[day]
                still_short.sort(key=lambda s: -remaining.get(s['id'], 0))  # most lessons still missing gets first claim
                chosen = None
                for candidate in still_short:
                    cid = candidate['id']
                    if (cid, day, period['id']) in subject_unavailable:
                        continue
                    other_subjects_today = used_today - {cid}
                    if any((cid, other) in same_day_forbidden for other in other_subjects_today):
                        continue
                    if last_subject_id is not None and cid != last_subject_id and (cid, last_subject_id) in consecutive_forbidden:
                        continue
                    if any(filled.get((day, nb_id), {}).get('id') == cid for nb_id in _adjacent_period_ids(period['id'])):
                        continue  # would sit directly next to this subject's other lesson that day — skip rather than accidentally create an unconfigured double
                    chosen = candidate
                    break
                if chosen is None:
                    continue
                cid = chosen['id']
                cand_teacher = teacher_for_subject.get(cid)
                if cand_teacher and (
                    cand_teacher in booked.get((day, period['id']), set())
                    or (cand_teacher, day, period['id']) in unavailable
                ):
                    cand_teacher = None
                _place(day, period['id'], chosen, cand_teacher)
                last_subject_by_day[day] = cid

            # --- Pass D: the absolute final resort. Same-Day-Forbidden and
            # Consecutive-Forbidden are PREFERENCES an admin set between
            # two subjects — real, worth respecting, but not physical
            # impossibilities, so they're what this pass is allowed to
            # override to close a genuine shortfall. Three things stay
            # hard and non-negotiable here exactly as in every pass before
            # it: Subject Time-Off (a subject's own "Not Available" rule),
            # Teacher Availability (a teacher's own "Not Available" rule),
            # and — the one true physical impossibility — ever placing the
            # same teacher in two different classes at the identical day
            # and period. If the only way to place this subject here would
            # require its teacher to be in two places at once, the subject
            # is scheduled anyway but with no teacher shown (exactly how
            # every other phase already handles a genuine teacher
            # conflict), never as a real collision. Every relaxation used
            # here is recorded and shown to the admin afterward — nothing
            # about this is silent.
            relaxed_placements = []

            for day, period in all_week_slots:
                if (day, period['id']) in filled:
                    continue
                still_short = [s for s in free_subjects if remaining.get(s['id'], 0) > 0]
                if not still_short:
                    continue
                used_today = used_today_by_day[day]
                last_subject_id = last_subject_by_day[day]
                still_short.sort(key=lambda s: -remaining.get(s['id'], 0))

                chosen, broke_rule = None, None
                for candidate in still_short:
                    cid = candidate['id']
                    # The one guard that's never lifted, even here: never
                    # sit this subject directly next to its own other
                    # lesson that day — that would silently create an
                    # unconfigured double/triple, which is a different
                    # problem from the one this pass exists to solve.
                    if any(filled.get((day, nb_id), {}).get('id') == cid for nb_id in _adjacent_period_ids(period['id'])):
                        continue

                    # Subject Time-Off is a hard block here too — not
                    # something this pass is allowed to override, only
                    # Same-Day/Consecutive-Forbidden are.
                    if (cid, day, period['id']) in subject_unavailable:
                        continue

                    cand_teacher_check = teacher_for_subject.get(cid)
                    if cand_teacher_check and (cand_teacher_check, day, period['id']) in unavailable:
                        continue  # Teacher Availability is hard here too — never overridden

                    other_subjects_today = used_today - {cid}
                    blocked_by_same_day = any((cid, other) in same_day_forbidden for other in other_subjects_today)
                    blocked_by_consecutive = last_subject_id is not None and cid != last_subject_id and (cid, last_subject_id) in consecutive_forbidden

                    if not (blocked_by_same_day or blocked_by_consecutive):
                        continue  # not actually blocked by either of these here — an earlier pass would already have placed it

                    chosen = candidate
                    broke_rule = "a Same-Day-Forbidden rule" if blocked_by_same_day else "a Consecutive-Forbidden rule"
                    break

                if chosen is None:
                    continue

                cid = chosen['id']
                cand_teacher = teacher_for_subject.get(cid)
                collided = bool(cand_teacher and cand_teacher in booked.get((day, period['id']), set()))
                if collided:
                    cand_teacher = None  # never an actual double-booking — schedule teacherless instead
                _place(day, period['id'], chosen, cand_teacher)
                last_subject_by_day[day] = cid
                note = f"{day} {period['label']}: placed '{chosen['name']}' by relaxing {broke_rule}"
                if collided:
                    note += " (its usual teacher was also busy at this exact time, so no teacher is shown here — never double-booked)"
                relaxed_placements.append(note)

            # --- Phase 4 has been replaced: it used to force-fill every
            # remaining empty period by scheduling subjects BEYOND their
            # configured weekly quota (e.g. giving Mathematics a 6th
            # lesson when only 5 were configured, just to avoid a blank
            # period). That's exactly the "system is allocating more
            # lessons for a subject... and fewer than the allocated
            # lessons to another" symptom — precision matters more than
            # zero gaps. A period that can't be filled by a subject still
            # within its own configured quota is now left genuinely
            # empty, and exactly which subjects came up short — and why —
            # is computed below and surfaced to the admin, matching how a
            # professional scheduler like ASC Timetables reports an
            # over-constrained setup instead of silently patching it.
            conn.commit()

            # --- Shortfall diagnostics: for every subject that still needs
            # at least one more lesson than it actually got, explain WHY —
            # concretely, per still-empty slot in the week, which specific
            # rule blocked it there. This is what lets an admin see exactly
            # which rule would need to be relaxed to reach a full, exact
            # schedule, rather than just being told "something didn't fit." ---
            shortfalls = []
            still_empty_final = [(day, period) for day in days for period in teaching_periods if (day, period['id']) not in filled]
            for subj in free_subjects:
                sid = subj['id']
                short_by = remaining.get(sid, 0)
                if short_by <= 0:
                    continue

                reasons = []
                for day, period in still_empty_final:
                    used_today = used_today_by_day[day]
                    last_subject_id = last_subject_by_day[day]
                    if (sid, day, period['id']) in subject_unavailable:
                        reasons.append(f"{day} {period['label']}: blocked by '{subj['name']}' Subject Time-Off (marked Not Available)")
                        continue
                    if sid in used_today:
                        reasons.append(f"{day} {period['label']}: '{subj['name']}' is already scheduled once that day, and every additional slot that day is blocked by another rule")
                        continue
                    same_day_conflict = next((other for other in used_today if (sid, other) in same_day_forbidden), None)
                    if same_day_conflict is not None:
                        conflict_name = next((s['name'] for s in subjects if s['id'] == same_day_conflict), "another subject")
                        reasons.append(f"{day} {period['label']}: blocked by a Same-Day-Forbidden rule against '{conflict_name}', already scheduled that day")
                        continue
                    if last_subject_id is not None and (sid, last_subject_id) in consecutive_forbidden:
                        conflict_name = next((s['name'] for s in subjects if s['id'] == last_subject_id), "the previous lesson")
                        reasons.append(f"{day} {period['label']}: blocked by a Consecutive-Forbidden rule against '{conflict_name}', in the period right before it")
                        continue
                    cand_teacher = teacher_for_subject.get(sid)
                    if cand_teacher and (cand_teacher, day, period['id']) in unavailable:
                        reasons.append(f"{day} {period['label']}: the assigned teacher is marked Not Available at that time (Teacher Availability)")
                        continue
                    # A slot that reaches here without a hard block should
                    # actually already be filled by the fair passes above —
                    # if it's not, something more unusual blocked it than
                    # this diagnostic can name specifically.
                    reasons.append(f"{day} {period['label']}: no hard rule blocks it, but the fair-placement pass didn't reach it — try regenerating, or check for a teacher clash with another subject already using that slot")

                if not still_empty_final:
                    shortfalls.append({
                        'subject': subj['name'],
                        'short_by': short_by,
                        'detail': "Every period in the week is already filled by other subjects — there's nowhere left to place it. To fit the missing lesson(s), either add more teaching periods, reduce another subject's lessons-per-week, or relax a Same-Day/Consecutive-Forbidden rule.",
                    })
                else:
                    shortfalls.append({
                        'subject': subj['name'],
                        'short_by': short_by,
                        'detail': "; ".join(reasons[:6]) + (f" (+{len(reasons) - 6} more empty period(s) checked)" if len(reasons) > 6 else ""),
                    })

            # issues_json now stores {"shortfalls": [...], "relaxed": [...]}
            # rather than a bare list — relaxed_placements (from Pass D)
            # records every lesson that only fit by overriding a Subject
            # Time-Off / Same-Day / Consecutive preference, so the admin
            # can see exactly where and why, even though the lesson itself
            # did get placed. The read side handles both this new shape
            # and the old bare-list shape from before this existed.
            if shortfalls or relaxed_placements:
                cur.execute("""
                    INSERT INTO timetable_generation_issues (school_id, grade_name, education_level, stream, issues_json, created_at)
                    VALUES (%s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (school_id, grade_name, education_level, stream)
                    DO UPDATE SET issues_json = EXCLUDED.issues_json, created_at = NOW();
                """, (school_id, grade_name, education_level, stream, json.dumps({"shortfalls": shortfalls, "relaxed": relaxed_placements})))
            else:
                cur.execute("DELETE FROM timetable_generation_issues WHERE school_id = %s AND grade_name = %s AND education_level = %s AND stream = %s;", (school_id, grade_name, education_level, stream))

            conn.commit()

    encoded_grade = urllib.parse.quote(grade_name)
    encoded_level = urllib.parse.quote(education_level)
    encoded_stream = urllib.parse.quote(stream)
    return RedirectResponse(url=f"/timetable/grade/{school_id}?grade_name={encoded_grade}&education_level={encoded_level}&stream={encoded_stream}", status_code=303)



# ============================================================
# Blank Timetable Creator & Manual Slot Editor
# ============================================================

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
            elif kind == "custom":
                cur.execute("""
                    SELECT staff_user_id FROM teacher_subject_assignments
                    WHERE school_id = %s AND custom_subject_id = %s AND grade_name = %s AND education_level = %s AND stream = %s;
                """, (school_id, custom_subject_id, grade_name, education_level, stream))
                assignment = cur.fetchone()
                teacher_id = assignment['staff_user_id'] if assignment else None
            elif kind == "activity":
                # A co-curricular activity's supervisor doubles as the
                # "teacher" for this slot, for conflict-checking purposes.
                cur.execute("SELECT staff_user_id FROM co_curricular_activities WHERE id = %s AND school_id = %s;", (co_curricular_activity_id, school_id))
                activity_row = cur.fetchone()
                teacher_id = activity_row['staff_user_id'] if activity_row else None

            if teacher_id:
                # Matches by ACTUAL OVERLAPPING CLOCK TIME, not just exact
                # period_id — two different education levels have entirely
                # separate period rows even when their real times coincide
                # exactly, so comparing period_id alone would miss a
                # genuine cross-level double-booking (the actual bug this
                # replaced: a teacher could be manually assigned to two
                # different levels' classes at the same real time, since
                # each level's "Period 3" is a different database row).
                cur.execute("SELECT start_time, end_time FROM timetable_periods WHERE id = %s;", (period_id,))
                this_period_row = cur.fetchone()
                this_start = _parse_time_to_minutes_shared(this_period_row['start_time']) if this_period_row else None
                this_end = _parse_time_to_minutes_shared(this_period_row['end_time']) if this_period_row else None

                cur.execute("""
                    SELECT ts.grade_name, ts.stream, tp.start_time, tp.end_time FROM timetable_slots ts
                    JOIN timetable_periods tp ON ts.period_id = tp.id
                    WHERE ts.school_id = %s AND ts.day_of_week = %s
                      AND ts.staff_user_id = %s
                      AND NOT (ts.grade_name = %s AND ts.education_level = %s AND ts.stream = %s);
                """, (school_id, day_of_week, teacher_id, grade_name, education_level, stream))
                clash = None
                if this_start is not None and this_end is not None:
                    for candidate in cur.fetchall():
                        other_start = _parse_time_to_minutes_shared(candidate['start_time'])
                        other_end = _parse_time_to_minutes_shared(candidate['end_time'])
                        if _time_ranges_overlap_shared(this_start, this_end, other_start, other_end):
                            clash = candidate
                            break
                if clash:
                    clash_label = _section_label(clash['grade_name'], clash['stream'])
                    raise HTTPException(
                        status_code=400,
                        detail=f"That teacher is already scheduled to teach {clash_label} at this exact time. Reassign the teacher for this subject, or pick a different subject for this slot."
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
                # Checks whichever id actually applies (a custom subject has
                # no learning_area_id at all, and vice versa) — querying with
                # the wrong one always NULL means "no row found", silently
                # bypassing this check entirely for whichever type wasn't handled.
                if learning_area_id is not None:
                    cur.execute("""
                        SELECT status FROM subject_availability
                        WHERE school_id = %s AND learning_area_id = %s AND day_of_week = %s AND period_id = %s;
                    """, (school_id, learning_area_id, day_of_week, period_id))
                elif custom_subject_id is not None:
                    cur.execute("""
                        SELECT status FROM subject_availability
                        WHERE school_id = %s AND custom_subject_id = %s AND day_of_week = %s AND period_id = %s;
                    """, (school_id, custom_subject_id, day_of_week, period_id))
                else:
                    cur.execute("SELECT NULL AS status WHERE FALSE;")  # co-curricular activities have no subject time-off concept
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
        f"<th style='padding:12px 10px;font-size:17px;{'background:#eef2f7;' if not p['is_teaching_period'] else ''}'>{esc(p['short_label'] or p['label'])}</th>"
        for p in periods
    )
    time_cells = "".join(
        f"<th style='font-weight:normal;font-size:14px;color:#64748b;padding-bottom:8px;'>{esc(p['start_time'] or '')}-{esc(p['end_time'] or '')}</th>"
        for p in periods
    )

    body_rows = ""
    for day_i, day in enumerate(days):
        row = f"<td style='padding:18px 16px;font-weight:bold;font-size:18px;white-space:nowrap;border:1px solid #cbd5e1;'>{esc(day[:2].upper())}</td>"
        for p in periods:
            p_type = p.get('period_type') or ('teaching' if p['is_teaching_period'] else 'break')
            if p_type == 'break':
                if day_i == 0:
                    row += (
                        f"<td rowspan='{len(days)}' style='border:1px solid #cbd5e1;text-align:center;background:#f1f5f9;'>"
                        f"<div style='writing-mode:vertical-rl;transform:rotate(180deg);font-size:16px;font-weight:bold;"
                        f"color:#475569;white-space:nowrap;margin:0 auto;'>{esc(p['label'])}</div></td>"
                    )
                continue  # subsequent days: cell already covered by row 1's rowspan
            if p_type == 'prep':
                row += (
                    "<td style='padding:18px 10px;text-align:center;border:1px solid #e2e8f0;background:#f5f3ff;'>"
                    "<span style='font-size:15px;font-weight:bold;color:#6d28d9;'>PREP</span></td>"
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
            row += f"<td style='padding:18px 10px;text-align:center;border:1px solid #e2e8f0;{cell_bg}'>{content}</td>"
        body_rows += f"<tr>{row}</tr>"

    return f"""
    <table style="width:100%;height:100%;border-collapse:collapse;font-size:19px;margin-top:18px;table-layout:fixed;">
        <thead>
            <tr style="background:#f8fafc;"><th style="padding:10px;"></th>{header_cells}</tr>
            <tr style="background:#f8fafc;"><th></th>{time_cells}</tr>
        </thead>
        <tbody>{body_rows}</tbody>
    </table>
    """



# ============================================================
# View Switcher — Whole / Teachers / Subjects perspectives, ASC-style.
# "Whole" and "Teachers" link to the existing master view and teacher
# picker (already fully built, no need to duplicate that logic); "Subjects"
# is a genuinely new perspective built here.
# ============================================================

@router.get("/timetable/view/{school_id}", response_class=HTMLResponse)
def timetable_view_switcher(school_id: int, request: Request):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT name FROM schools WHERE id = %s;", (school_id,))
            school = cur.fetchone()
            if not school:
                raise HTTPException(status_code=404, detail="School not found.")

    return f"""
    <!DOCTYPE html>
    <html>
    <head><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Elimu Hub | Timetable Views</title><script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script></head>
    <body class="bg-[#F7F9F8] min-h-screen">
        <header class="bg-white border-b px-6 sm:px-8 py-4 flex flex-col sm:flex-row sm:justify-between sm:items-center gap-2">
            <div>
                <h1 class="text-base font-bold text-slate-900">🔀 Timetable Views — {esc(school['name'])}</h1>
                <p class="text-xs text-slate-400">Look at the same schedule from three different angles.</p>
            </div>
            <a href="/timetable/dashboard/{school_id}" class="bg-slate-800 hover:bg-slate-900 text-white px-4 py-2 rounded-xl text-xs font-bold text-center transition">← Back to Workspace</a>
        </header>
        <div class="p-6 sm:p-8 max-w-4xl mx-auto grid grid-cols-1 sm:grid-cols-3 gap-4">
            <a href="/timetable/master/{school_id}" class="bg-white border border-slate-200/80 hover:shadow-md transition-shadow p-6 rounded-2xl shadow-xs block">
                <p class="text-2xl mb-2">🗂️</p>
                <h2 class="text-sm font-black text-slate-800">Whole Schedule</h2>
                <p class="text-xs text-slate-400 mt-1">Every class, every level, side by side — the full picture at once.</p>
            </a>
            <a href="/timetable/teachers/{school_id}" class="bg-white border border-slate-200/80 hover:shadow-md transition-shadow p-6 rounded-2xl shadow-xs block">
                <p class="text-2xl mb-2">🧑‍🏫</p>
                <h2 class="text-sm font-black text-slate-800">By Teacher</h2>
                <p class="text-xs text-slate-400 mt-1">Pick a teacher, see their full week across every class they teach.</p>
            </a>
            <a href="/timetable/view/subjects/{school_id}" class="bg-white border border-slate-200/80 hover:shadow-md transition-shadow p-6 rounded-2xl shadow-xs block">
                <p class="text-2xl mb-2">📚</p>
                <h2 class="text-sm font-black text-slate-800">By Subject</h2>
                <p class="text-xs text-slate-400 mt-1">Pick a subject, see exactly when every class studies it.</p>
            </a>
        </div>
    </body>
    </html>
    """


@router.get("/timetable/view/subjects/{school_id}", response_class=HTMLResponse)
def timetable_subject_perspective(school_id: int, request: Request, education_level: str = "Upper Primary", learning_area_id: int = None):
    """A genuinely new view: pick one subject and see, across every class
    in a level, exactly which day/period it's taught in — useful for
    spotting a subject that's badly clustered on one day, or confirming a
    subject's spread looks sensible school-wide. Purely read-only."""
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT name FROM schools WHERE id = %s;", (school_id,))
            school = cur.fetchone()
            if not school:
                raise HTTPException(status_code=404, detail="School not found.")

            cur.execute("SELECT id, name FROM learning_areas WHERE education_level = %s;", (education_level,))
            subjects = sort_subjects_for_display(cur.fetchall(), education_level)

            if learning_area_id is None and subjects:
                learning_area_id = subjects[0]['id']

            days = get_school_days(cur, school_id)
            periods = [p for p in get_periods_for_level(cur, school_id, education_level) if p['is_teaching_period']]

            grid = {}
            if learning_area_id:
                cur.execute("""
                    SELECT ts.grade_name, ts.stream, ts.day_of_week, ts.period_id, u.full_name AS teacher_name
                    FROM timetable_slots ts
                    LEFT JOIN users u ON ts.staff_user_id = u.id
                    WHERE ts.school_id = %s AND ts.education_level = %s AND ts.learning_area_id = %s;
                """, (school_id, education_level, learning_area_id))
                for row in cur.fetchall():
                    grid[(row['day_of_week'], row['period_id'])] = grid.get((row['day_of_week'], row['period_id']), []) + [
                        f"{_section_label(row['grade_name'], row['stream'])}" + (f" ({row['teacher_name']})" if row['teacher_name'] else "")
                    ]

    level_tabs = "".join(
        f"""<a href="/timetable/view/subjects/{school_id}?education_level={urllib.parse.quote(lvl)}"
               class="px-4 py-2 rounded-xl text-xs font-bold transition {'bg-indigo-800 text-white' if lvl == education_level else 'bg-white border border-slate-200 text-slate-600 hover:bg-slate-50'}">{lvl}</a>"""
        for lvl in EDUCATION_LEVELS
    )
    subject_options = "".join(
        f"<option value='{s['id']}' {'selected' if s['id'] == learning_area_id else ''}>{esc(s['name'])}</option>"
        for s in subjects
    )

    header_cells = "".join(f"<th class='p-2 text-center'>{esc(p['label'])}</th>" for p in periods)
    body_rows = ""
    for day in days:
        cells = ""
        for p in periods:
            entries = grid.get((day, p['id']), [])
            cells += f"<td class='p-2 text-xs text-center align-top'>{'<br>'.join(esc(e) for e in entries) if entries else '<span class=\"text-slate-300\">—</span>'}</td>"
        body_rows += f"<tr class='border-b border-slate-50'><td class='p-2 text-xs font-bold text-slate-600'>{esc(day)}</td>{cells}</tr>"

    return f"""
    <!DOCTYPE html>
    <html>
    <head><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Elimu Hub | Timetable by Subject</title><script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script></head>
    <body class="bg-[#F7F9F8] min-h-screen">
        <header class="bg-white border-b px-6 sm:px-8 py-4 flex flex-col sm:flex-row sm:justify-between sm:items-center gap-2">
            <div>
                <h1 class="text-base font-bold text-slate-900">📚 By Subject — {esc(school['name'])}</h1>
                <p class="text-xs text-slate-400">See exactly when a subject is taught, across every class in a level.</p>
            </div>
            <a href="/timetable/view/{school_id}" class="bg-slate-800 hover:bg-slate-900 text-white px-4 py-2 rounded-xl text-xs font-bold text-center transition">← Back to Views</a>
        </header>
        <div class="p-4 sm:p-8 max-w-5xl mx-auto space-y-4">
            <div class="flex gap-2 flex-wrap">{level_tabs}</div>
            <form method="get" class="flex items-center gap-2">
                <input type="hidden" name="education_level" value="{esc(education_level)}">
                <label class="text-xs font-bold text-slate-500">Subject:</label>
                <select name="learning_area_id" onchange="this.form.submit()" class="border p-2 rounded-lg text-sm bg-white font-semibold">{subject_options}</select>
            </form>
            <div class="bg-white rounded-2xl border shadow-xs overflow-x-auto">
                <table class="w-full">
                    <thead><tr class="bg-slate-50 text-slate-500 text-xs border-b"><th class="p-2 text-left">Day</th>{header_cells}</tr></thead>
                    <tbody>{body_rows or "<tr><td colspan='" + str(len(periods)+1) + "' class='p-8 text-center text-slate-400 text-xs italic'>No periods configured for this level.</td></tr>"}</tbody>
                </table>
            </div>
        </div>
    </body>
    </html>
    """


# ============================================================
# Print Suite — class timetable, teacher timetable, master (whole-level) view
# ============================================================

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
        teacher_line = f"<br><span style='font-size:13px;color:#64748b;'>{esc(teacher_short)}</span>" if teacher_short else ""
        bg_color, text_color = get_subject_color(slot['subject_name'])
        content = f"<b style='color:{text_color};font-size:20px;'>{esc(abbreviate_subject(slot['subject_name']))}</b>{teacher_line}"
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
            @page {{ size: A4 landscape; margin: 5mm; }}
            body {{ font-family: Arial, sans-serif; padding: 20px; color: #1e293b; background:#f1f5f9; }}
            @media print {{ .no-print {{ display: none !important; }} body {{ background: white; padding: 0; }} }}
            th {{ background:#f8fafc; border-bottom:2px solid #cbd5e1; font-size:10px; text-transform:uppercase; color:#64748b; }}
            .print-page {{ max-width: 287mm; margin: 0 auto; background: white; padding: 14mm; border-radius: 10px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }}
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

    # Staff may only ever view their own schedule — never another
    # teacher's, even by guessing/editing the teacher_id in the URL.
    # Admin and superadmin keep full visibility, since they legitimately
    # need to check any teacher's timetable for management purposes.
    viewer = get_current_session_user(request)
    if viewer and viewer['role'] == 'staff' and viewer['id'] != teacher_id:
        raise HTTPException(status_code=403, detail="Access Denied: You can only view your own timetable.")

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
                    content = f"<b style='color:{text_color};font-size:20px;'>{esc(abbreviate_subject(slot['subject_name']))}</b><br><span style='font-size:13px;color:#64748b;'>{esc(class_label)}</span>"
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
            @page {{ size: A4 landscape; margin: 5mm; }}
            body {{ font-family: Arial, sans-serif; padding: 20px; color: #1e293b; background:#f1f5f9; }}
            @media print {{ .no-print {{ display: none !important; }} body {{ background: white; padding: 0; }} }}
            th {{ background:#f8fafc; border-bottom:2px solid #cbd5e1; font-size:10px; text-transform:uppercase; color:#64748b; }}
            .print-page {{ max-width: 287mm; margin: 0 auto; background: white; padding: 14mm; border-radius: 10px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }}
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
            @page {{ size: A4 landscape; margin: 5mm; }}
            body {{ font-family: Arial, sans-serif; padding: 12px; color: #1e293b; background:#f1f5f9; }}
            @media print {{ .no-print {{ display: none !important; }} body {{ background: white; padding: 0; }} }}
            table {{ width: 100%; border-collapse: collapse; margin-top: 12px; font-size: 10.5px; }}
            th, td {{ border: 1px solid #cbd5e1; }}
            th {{ background:#f8fafc; }}
            .print-page {{ max-width: 287mm; margin: 0 auto; background: white; padding: 12mm; border-radius: 10px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); overflow-x:auto; }}
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