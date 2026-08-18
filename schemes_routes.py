"""
schemes_routes.py — Schemes of Work module for Elimu Hub.

Workflow:
  1. Super Admin uploads a master scheme (PDF) for a subject/grade/term,
     tagged with metadata. The system attempts to parse it into structured
     rows (week, lesson, strand, sub-strand, objectives, activities, key
     inquiry questions, resources, assessment). Since PDF table extraction
     is inherently unreliable, the parse is never trusted blindly — Super
     Admin reviews and corrects the parsed rows in a web editor before the
     scheme is finalized as a master template.
  2. School Admins see available masters matching their school's grades
     and can "Import" one, creating a school-specific COPY (never editing
     the master itself) — then assign that copy to the teacher who
     actually teaches that subject/grade.
  3. Teachers see schemes assigned to them in their staff portal, can
     freely edit any field (per explicit product decision — this is their
     document to adapt), and print a professional-looking document with
     their name/TSC number in the header.

Master and copy are separate tables throughout, deliberately: editing a
school's copy must never alter the master template other schools import
from, and re-importing a master must never silently overwrite a
teacher's in-progress customizations.
"""

import os
import re
import logging
import urllib.parse
from fastapi import APIRouter, Request, HTTPException, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from shared import (
    esc,
    get_db_connection,
    RealDictCursor,
    require_school_session,
    require_admin_session,
    require_superadmin_session,
    get_dashboard_url,
    get_current_session_user,
)

router = APIRouter()
logger = logging.getLogger(__name__)

EDUCATION_LEVELS = ["Lower Primary", "Upper Primary", "Junior School"]


def bootstrap_schemes_schema():
    """Creates/upgrades every table this module owns. Purely additive."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            # --- Master templates, uploaded once by Super Admin, shared
            # across every school that teaches that subject/grade. ---
            cur.execute("""
                CREATE TABLE IF NOT EXISTS scheme_masters (
                    id SERIAL PRIMARY KEY,
                    subject_name VARCHAR(150) NOT NULL,
                    grade_name VARCHAR(100) NOT NULL,
                    education_level VARCHAR(100) NOT NULL,
                    term VARCHAR(20) NOT NULL,
                    year INTEGER NOT NULL,
                    title VARCHAR(255),
                    source_pdf_url TEXT,
                    source_pdf_filename VARCHAR(255),
                    parse_review_status VARCHAR(20) NOT NULL DEFAULT 'draft',
                    created_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    created_at TIMESTAMP DEFAULT NOW(),
                    published_at TIMESTAMP
                );
            """)
            # The original uploaded PDF, stored directly rather than
            # depending on Supabase Storage being configured — parsing
            # can miss content on some document layouts, and having the
            # real source right there to reference (not just a filename)
            # is what makes filling in the gaps during review actually
            # fast and trustworthy, rather than a guessing game.
            cur.execute("ALTER TABLE scheme_masters ADD COLUMN IF NOT EXISTS source_pdf_data BYTEA;")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS scheme_master_rows (
                    id SERIAL PRIMARY KEY,
                    master_id INTEGER REFERENCES scheme_masters(id) ON DELETE CASCADE,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    week_number VARCHAR(20),
                    lesson_number VARCHAR(20),
                    strand TEXT,
                    sub_strand TEXT,
                    learning_outcomes TEXT,
                    learning_experiences TEXT,
                    key_inquiry_questions TEXT,
                    learning_resources TEXT,
                    assessment_methods TEXT,
                    reflection TEXT
                );
            """)

            # --- Per-school copies — created when a school "imports" a
            # master. Independent from that point on: editing a copy never
            # touches the master, and re-importing never touches an
            # existing copy. ---
            cur.execute("""
                CREATE TABLE IF NOT EXISTS scheme_copies (
                    id SERIAL PRIMARY KEY,
                    school_id INTEGER REFERENCES schools(id) ON DELETE CASCADE,
                    master_id INTEGER REFERENCES scheme_masters(id) ON DELETE SET NULL,
                    subject_name VARCHAR(150) NOT NULL,
                    grade_name VARCHAR(100) NOT NULL,
                    education_level VARCHAR(100) NOT NULL,
                    stream VARCHAR(100) NOT NULL DEFAULT 'ALL',
                    term VARCHAR(20) NOT NULL,
                    year INTEGER NOT NULL,
                    teacher_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    teacher_name_override VARCHAR(150),
                    tsc_number_override VARCHAR(50),
                    imported_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    imported_at TIMESTAMP DEFAULT NOW()
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS scheme_copy_rows (
                    id SERIAL PRIMARY KEY,
                    copy_id INTEGER REFERENCES scheme_copies(id) ON DELETE CASCADE,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    week_number VARCHAR(20),
                    lesson_number VARCHAR(20),
                    strand TEXT,
                    sub_strand TEXT,
                    learning_outcomes TEXT,
                    learning_experiences TEXT,
                    key_inquiry_questions TEXT,
                    learning_resources TEXT,
                    assessment_methods TEXT,
                    reflection TEXT
                );
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_scheme_copies_school ON scheme_copies (school_id);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_scheme_copies_teacher ON scheme_copies (teacher_user_id);")
            conn.commit()

    # Every published master gets auto-copied to every school that doesn't
    # already have one — no admin import required, per explicit product
    # decision. Idempotent (WHERE NOT EXISTS), so running this on every
    # startup is what safely backfills any master a school is missing —
    # including retroactively for schemes published before this existed,
    # and automatically for schools that register after a master already
    # exists. Verified directly against a real Postgres instance before
    # shipping: correctly skips schools that already have a copy (e.g.
    # one manually imported earlier), correctly leaves that existing
    # copy's rows untouched, and correctly creates+populates new copies
    # for everyone else.
    auto_copy_published_masters_to_all_schools()


def auto_copy_published_masters_to_all_schools():
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                WITH new_copies AS (
                    INSERT INTO scheme_copies (school_id, master_id, subject_name, grade_name, education_level, stream, term, year)
                    SELECT s.id, m.id, m.subject_name, m.grade_name, m.education_level, 'ALL', m.term, m.year
                    FROM schools s
                    CROSS JOIN scheme_masters m
                    WHERE m.parse_review_status = 'published'
                      AND NOT EXISTS (
                          SELECT 1 FROM scheme_copies sc WHERE sc.school_id = s.id AND sc.master_id = m.id
                      )
                    RETURNING id, master_id
                )
                INSERT INTO scheme_copy_rows (copy_id, sort_order, week_number, lesson_number, strand, sub_strand, learning_outcomes, learning_experiences, key_inquiry_questions, learning_resources, assessment_methods, reflection)
                SELECT nc.id, mr.sort_order, mr.week_number, mr.lesson_number, mr.strand, mr.sub_strand, mr.learning_outcomes, mr.learning_experiences, mr.key_inquiry_questions, mr.learning_resources, mr.assessment_methods, mr.reflection
                FROM new_copies nc
                JOIN scheme_master_rows mr ON mr.master_id = nc.master_id;
            """)
            conn.commit()


SCHEME_ROW_FIELDS = [
    "week_number", "lesson_number", "strand", "sub_strand",
    "learning_outcomes", "learning_experiences", "key_inquiry_questions",
    "learning_resources", "assessment_methods", "reflection",
]

SCHEME_ROW_LABELS = {
    "week_number": "Week", "lesson_number": "Lesson No.",
    "strand": "Strand", "sub_strand": "Sub-Strand",
    "learning_outcomes": "Learning Outcomes", "learning_experiences": "Learning Experiences",
    "key_inquiry_questions": "Key Inquiry Question(s)", "learning_resources": "Learning Resources",
    "assessment_methods": "Assessment Methods", "reflection": "Reflection",
}


# ============================================================
# PDF Parsing — best-effort extraction of a scheme's table into structured
# rows. PDF table extraction is inherently unreliable (text position and
# column boundaries vary a lot between documents), so this is deliberately
# NOT trusted blindly anywhere downstream — every parsed row goes through
# a human review/edit step before being saved as a master. Untested
# against a real sample PDF as of writing; expect to tune the column-
# matching logic once tried against an actual upload.
# ============================================================

# Maps flexible header text (lowercased, punctuation-stripped) found in the
# PDF to our internal field names — handles the common variations schemes
# actually use (e.g. "S/NO" vs "WK", "SUB STRAND" vs "SUB-STRAND").
HEADER_MATCH_PATTERNS = {
    "week_number": ["wk", "week", "wks"],
    "lesson_number": ["lsn", "lssn", "kipindi"],
    "strand": ["strand", "madakuu", "theme"],
    "sub_strand": ["substrand", "madandogo"],
    "learning_outcomes": ["learningoutcomes", "specificlearningoutcomes", "objectives", "shabaha", "lessonlearningoutcomes"],
    "learning_experiences": ["learningexperiences", "coreactivities", "shughuli", "shughulizaufunzaji"],
    "key_inquiry_questions": ["keyinquiryquestion", "keyinquiryquestions", "kiq", "maswalidadisi"],
    "learning_resources": ["learningresources", "resources", "nyenzo"],
    "assessment_methods": ["assessmentmethods", "assessment", "tathmini"],
    "reflection": ["reflection", "remarks", "maoni"],
}


def _normalize_header(text):
    return re.sub(r"[^a-z]", "", (text or "").lower())


# Patterns normalized once here, the same way header text gets normalized
# at match time — this is what makes the exact bug found against a real
# Kiswahili scheme structurally impossible to reintroduce: a pattern like
# "mada kuu" written with a space would otherwise never match normalized
# header text (which never contains spaces) via exact OR substring
# matching, silently leaving that column unmapped.
NORMALIZED_HEADER_MATCH_PATTERNS = {
    field: [_normalize_header(p) for p in patterns]
    for field, patterns in HEADER_MATCH_PATTERNS.items()
}


_LETTER_SPACING_PATTERN = re.compile(r"\b(?:[A-Za-z](?:\s(?=[A-Za-z]\b))){2,}[A-Za-z]\b")


def _fix_letter_spacing(text):
    """Some PDF exports render one specific wrapped line within a cell
    with each character individually positioned (a justification
    artifact) rather than as one continuous text run — pdfplumber then
    extracts it as separate single-letter 'words' no matter which
    extraction method is used (confirmed against a real scheme PDF: this
    persisted through extract_words, cropped extract_text, and table cell
    extraction alike). Real English essentially never has a genuine run
    of 3+ standalone single letters, so detecting that pattern and
    rejoining it is a safe, reliable repair — and it never touches a
    real short word like "a" or "I" appearing on its own."""
    if not text:
        return text
    return _LETTER_SPACING_PATTERN.sub(lambda m: m.group(0).replace(" ", ""), text)


def _match_column_to_field(header_text):
    normalized = _normalize_header(header_text)
    if not normalized:
        return None
    # Exact match first, across every field, before any substring
    # matching — otherwise a short pattern like "strand" would wrongly
    # match "SUB-STRAND" (normalized "substrand") just because it's a
    # substring, silently colliding two genuinely different columns onto
    # the same field.
    for field, patterns in NORMALIZED_HEADER_MATCH_PATTERNS.items():
        if normalized in patterns:
            return field
    # Fall back to substring matching, but only for patterns that are at
    # least 5 characters — short patterns are exactly what causes false
    # substring collisions like the one above.
    for field, patterns in NORMALIZED_HEADER_MATCH_PATTERNS.items():
        for pattern in patterns:
            if len(pattern) >= 5 and (pattern in normalized or normalized in pattern):
                return field
    return None


def parse_scheme_pdf(filepath: str):
    """Attempts to extract scheme rows from a PDF using pdfplumber's table
    detection. Returns (rows, warnings) — rows is a best-effort list of
    dicts with SCHEME_ROW_FIELDS keys (missing/unmatched fields are empty
    strings, never crashes on a row that doesn't fully match); warnings is
    a list of human-readable strings surfaced to the admin during review,
    since this parse should never be silently trusted."""
    import pdfplumber

    MAX_PAGES = 40  # a real scheme of work is realistically well under this; this is a safety cap, not an expected limit

    rows = []
    warnings = []

    try:
        with pdfplumber.open(filepath) as pdf:
            if not pdf.pages:
                return [], ["The PDF has no pages."]

            if len(pdf.pages) > MAX_PAGES:
                warnings.append(
                    f"This PDF has {len(pdf.pages)} pages — only the first {MAX_PAGES} were scanned "
                    f"to keep processing time reasonable. Add any remaining lessons manually below."
                )

            # A row must confidently match at least this many of our known
            # fields to be treated as a genuine header row — otherwise a
            # small, unrelated table elsewhere (e.g. a title block that
            # pdfplumber mistakes for a tiny table) gets treated as the
            # column structure for every row that follows, silently
            # wiping out all the real content. This was a real, confirmed
            # bug against an actual scheme PDF.
            MIN_CONFIDENT_HEADER_MATCHES = 4

            # Deliberately checks EVERY row for header-likeness, not just
            # each table's first row — this is what correctly handles a
            # real multi-page scheme: the true header might not be on
            # page 1 at all (a title/cover block there can get detected as
            # its own small "table" first), and later pages' tables don't
            # necessarily repeat the header, so their first row is real
            # data that must not be mistaken for one. Rows encountered
            # before the real header is ever found (like a title page) are
            # correctly skipped rather than guessed at.
            column_field_map = None
            for page_num, page in enumerate(pdf.pages[:MAX_PAGES], start=1):
                tables = page.extract_tables()
                if not tables:
                    continue
                for table in tables:
                    if not table:
                        continue
                    for row in table:
                        candidate_map = [_match_column_to_field(cell) for cell in row]
                        matched_count = sum(1 for f in candidate_map if f)

                        if matched_count >= MIN_CONFIDENT_HEADER_MATCHES:
                            # A genuine header row — establishes (or
                            # re-confirms, on a later page) the column
                            # structure. Never treated as data itself.
                            column_field_map = candidate_map
                            continue

                        if column_field_map is None:
                            # Haven't found the real header yet — this is
                            # pre-header content (e.g. a title page) and
                            # can't be meaningfully mapped to fields.
                            continue

                        if not any((cell or "").strip() for cell in row):
                            continue  # skip fully blank rows

                        row_dict = {field: "" for field in SCHEME_ROW_FIELDS}
                        for col_idx, cell_value in enumerate(row):
                            if col_idx >= len(column_field_map):
                                break
                            field = column_field_map[col_idx]
                            if field:
                                row_dict[field] = _fix_letter_spacing((cell_value or "").strip())
                        # A row whose only content is a stray "Page N"
                        # watermark (picked up as if it were real cell
                        # content) isn't a real lesson — safe to drop
                        # automatically rather than leaving it for manual
                        # cleanup, since this pattern is unambiguous.
                        non_empty_values = [v for v in row_dict.values() if v]
                        if len(non_empty_values) == 1 and re.fullmatch(r"Page \d+", non_empty_values[0].strip()):
                            continue

                        rows.append(row_dict)

            if not rows:
                warnings.append(
                    "Could not detect any table structure in this PDF — it may be a scanned "
                    "image rather than a real text/table layer, or use a layout this parser "
                    "doesn't recognize. You'll need to enter the rows manually below."
                )
    except Exception as e:
        logger.warning(f"Scheme PDF parse failed for {filepath}: {e}")
        return [], [f"The PDF could not be read at all ({e}). You'll need to enter the rows manually below."]

    return rows, warnings


# ============================================================
# Super Admin — upload a master scheme, review/correct the parsed rows,
# then publish it so schools can see and import it.
# ============================================================

@router.get("/superadmin/schemes/upload", response_class=HTMLResponse)
def schemes_upload_form(request: Request):
    auth_error = require_superadmin_session(request)
    if auth_error:
        return auth_error

    level_options = "".join(f"<option value='{esc(lvl)}'>{esc(lvl)}</option>" for lvl in EDUCATION_LEVELS)

    return f"""
    <!DOCTYPE html>
    <html>
    <head><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Elimu Hub | Upload Scheme of Work</title><script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script></head>
    <body class="bg-slate-100 min-h-screen p-4 sm:p-8">
        <div class="max-w-lg mx-auto space-y-4">
            <a href="/superadmin/schemes/list" class="text-slate-500 hover:text-slate-700 text-xs font-bold inline-block">← Back to Master Schemes</a>
            <div class="bg-white p-6 rounded-2xl border shadow-xs">
            <h2 class="text-lg font-black text-slate-800">📘 Upload Master Scheme of Work</h2>
            <p class="text-xs text-slate-400 mb-4">Upload a PDF — the system will try to detect the table and split it into rows automatically. This becomes visible to schools and teachers immediately, so you'll land on the review screen right after to check and fix anything the parser got wrong.</p>
            <form action="/api/v1/superadmin/schemes/upload" method="post" enctype="multipart/form-data" class="space-y-3">
                <div>
                    <label class="text-xs font-bold text-slate-600 block mb-1">Subject / Learning Area</label>
                    <input type="text" name="subject_name" placeholder="e.g. Mathematics" class="w-full border p-2.5 rounded-xl text-sm" required>
                </div>
                <div class="grid grid-cols-2 gap-3">
                    <div>
                        <label class="text-xs font-bold text-slate-600 block mb-1">Education Level</label>
                        <select name="education_level" class="w-full border p-2.5 rounded-xl text-sm bg-white" required>{level_options}</select>
                    </div>
                    <div>
                        <label class="text-xs font-bold text-slate-600 block mb-1">Grade</label>
                        <input type="text" name="grade_name" placeholder="e.g. Grade 8" class="w-full border p-2.5 rounded-xl text-sm" required>
                    </div>
                </div>
                <div class="grid grid-cols-2 gap-3">
                    <div>
                        <label class="text-xs font-bold text-slate-600 block mb-1">Term</label>
                        <select name="term" class="w-full border p-2.5 rounded-xl text-sm bg-white">
                            <option>Term 1</option><option>Term 2</option><option>Term 3</option>
                        </select>
                    </div>
                    <div>
                        <label class="text-xs font-bold text-slate-600 block mb-1">Year</label>
                        <input type="number" name="year" value="2026" class="w-full border p-2.5 rounded-xl text-sm" required>
                    </div>
                </div>
                <div>
                    <label class="text-xs font-bold text-slate-600 block mb-1">Scheme PDF</label>
                    <input type="file" name="pdf_file" accept="application/pdf" class="w-full border p-2.5 rounded-xl text-sm bg-white" required>
                </div>
                <button type="submit" class="w-full bg-indigo-800 hover:bg-indigo-900 text-white font-bold py-3 rounded-xl text-sm transition">Upload &amp; Parse</button>
            </form>
            </div>
        </div>
    </body>
    </html>
    """


@router.post("/api/v1/superadmin/schemes/upload")
async def schemes_upload_process(
    request: Request, subject_name: str = Form(...), education_level: str = Form(...),
    grade_name: str = Form(...), term: str = Form(...), year: int = Form(...),
    pdf_file: UploadFile = File(...),
):
    auth_error = require_superadmin_session(request)
    if auth_error:
        return auth_error

    if not pdf_file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Please upload a PDF file.")

    contents = await pdf_file.read()
    if len(contents) > 20 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="PDF is too large (max 20MB).")

    tmp_path = f"/tmp/scheme_upload_{os.urandom(8).hex()}.pdf"
    with open(tmp_path, "wb") as f:
        f.write(contents)

    try:
        # Run in a background thread, not directly on the event loop —
        # PDF table detection is genuinely CPU-intensive and can take a
        # while on a complex document. Calling it directly here would
        # freeze the entire worker (including its ability to respond to
        # gunicorn's own health checks) for the whole duration, which is
        # exactly what caused a WORKER TIMEOUT / SIGABRT in production.
        parsed_rows, parse_warnings = await run_in_threadpool(parse_scheme_pdf, tmp_path)
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            recorded_by = request.cookies.get("session_user_id")
            # Published immediately on upload — no separate approval step
            # blocking visibility. The review/edit page (redirected to
            # right below) is still there to check and fix content, but
            # it's a convenience, not a gate.
            cur.execute("""
                INSERT INTO scheme_masters (subject_name, grade_name, education_level, term, year, title, source_pdf_filename, source_pdf_data, parse_review_status, created_by_user_id, published_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'published', %s, NOW()) RETURNING id;
            """, (subject_name.strip(), grade_name.strip(), education_level, term, year, f"{subject_name.strip()} — {grade_name.strip()}", pdf_file.filename, contents, recorded_by))
            master_id = cur.fetchone()[0]

            for i, row in enumerate(parsed_rows):
                cur.execute("""
                    INSERT INTO scheme_master_rows (master_id, sort_order, week_number, lesson_number, strand, sub_strand, learning_outcomes, learning_experiences, key_inquiry_questions, learning_resources, assessment_methods, reflection)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
                """, (
                    master_id, i, row["week_number"], row["lesson_number"], row["strand"], row["sub_strand"],
                    row["learning_outcomes"], row["learning_experiences"], row["key_inquiry_questions"],
                    row["learning_resources"], row["assessment_methods"], row["reflection"],
                ))
            conn.commit()

    # Immediately propagate to every school — no admin import required.
    # The bootstrap-time call handles retroactive backfill and new
    # schools registering later; this call is what makes a freshly
    # uploaded scheme visible school-wide right away, not just on the
    # next restart.
    auto_copy_published_masters_to_all_schools()

    warning_param = urllib.parse.quote("|".join(parse_warnings)) if parse_warnings else ""
    return RedirectResponse(url=f"/superadmin/schemes/review/{master_id}?warnings={warning_param}", status_code=303)


@router.get("/superadmin/schemes/pdf/{master_id}")
def schemes_view_source_pdf(master_id: int, request: Request):
    """Serves the original uploaded PDF's raw bytes, for embedding
    directly on the review page — so whoever is fixing up whatever the
    parser missed can see the real source right there, rather than
    needing to dig up their own copy of the file separately."""
    auth_error = require_superadmin_session(request)
    if auth_error:
        return auth_error

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT source_pdf_data, source_pdf_filename FROM scheme_masters WHERE id = %s;", (master_id,))
            row = cur.fetchone()
            if not row or not row[0]:
                raise HTTPException(status_code=404, detail="No source PDF stored for this scheme — it may have been uploaded before this feature was added.")

    pdf_bytes, filename = row
    return Response(
        content=bytes(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename or "scheme.pdf"}"'},
    )


@router.get("/superadmin/schemes/review/{master_id}", response_class=HTMLResponse)
def schemes_review_form(master_id: int, request: Request, warnings: str = ""):
    auth_error = require_superadmin_session(request)
    if auth_error:
        return auth_error

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT id, subject_name, grade_name, education_level, term, year, title,
                       source_pdf_filename, parse_review_status, created_by_user_id, created_at, published_at,
                       (source_pdf_data IS NOT NULL) AS has_source_pdf
                FROM scheme_masters WHERE id = %s;
            """, (master_id,))
            master = cur.fetchone()
            if not master:
                raise HTTPException(status_code=404, detail="Scheme not found.")

            cur.execute("SELECT * FROM scheme_master_rows WHERE master_id = %s ORDER BY sort_order ASC;", (master_id,))
            rows = cur.fetchall()

    warning_list = [w for w in warnings.split("|") if w] if warnings else []
    warnings_html = "".join(f"<li>{esc(w)}</li>" for w in warning_list)

    def _row_block(row_id, row_data, idx):
        field_inputs = ""
        for field in SCHEME_ROW_FIELDS:
            value = esc(row_data.get(field, "") or "")
            label = SCHEME_ROW_LABELS[field]
            is_short = field in ("week_number", "lesson_number")
            field_inputs += f"""
            <div class="{'col-span-1' if is_short else 'col-span-3'}">
                <label class="text-[11px] font-bold text-slate-500 uppercase block mb-1">{esc(label)}</label>
                {"<input type='text' name='" + field + "_" + str(idx) + "' value='" + value + "' class='w-full border p-2 rounded-lg text-sm'>" if is_short else "<textarea name='" + field + "_" + str(idx) + "' rows='3' class='w-full border p-2 rounded-lg text-sm'>" + value + "</textarea>"}
            </div>
            """
        return f"""
        <div class="bg-white border rounded-2xl p-4 grid grid-cols-6 gap-2 relative">
            <button type="button" onclick="this.closest('.bg-white').remove()" class="absolute top-2 right-2 text-rose-400 hover:text-rose-600 text-xs font-bold">✕ Remove</button>
            <input type="hidden" name="row_ids" value="{idx}">
            {field_inputs}
        </div>
        """

    rows_html = "".join(_row_block(r['id'], r, i) for i, r in enumerate(rows))

    if master['has_source_pdf']:
        pdf_panel = f"""
        <iframe src="/superadmin/schemes/pdf/{master_id}" style="width:100%; height:100%; border:none; border-radius:12px;" title="Original uploaded scheme PDF"></iframe>
        """
    else:
        pdf_panel = """
        <div style="width:100%; height:100%; display:flex; align-items:center; justify-content:center; text-align:center; padding:24px;">
            <p style="color:#94a3b8; font-size:13px;">No source PDF stored for this scheme — it was uploaded before this feature was added. Re-upload it to keep the original alongside future edits.</p>
        </div>
        """

    return f"""
    <!DOCTYPE html>
    <html>
    <head><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Elimu Hub | Review Scheme</title><script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script></head>
    <body class="bg-slate-100 min-h-screen p-4 sm:p-8">
        <div class="max-w-[1600px] mx-auto space-y-4">
            <a href="/superadmin/schemes/list" class="text-slate-500 hover:text-slate-700 text-xs font-bold inline-block">← Back to Master Schemes</a>
            <div class="bg-white p-6 rounded-2xl border shadow-xs">
                <h2 class="text-lg font-black text-slate-800">📘 Review — {esc(master['subject_name'])} — {esc(master['grade_name'])} ({esc(master['term'])} {master['year']})</h2>
                <p class="text-xs text-slate-400 mt-1">{len(rows)} row(s) detected. This scheme is already live and visible to schools. Parsing is best-effort, not guaranteed accurate — the original PDF is shown alongside so you can quickly fill in anything it missed.</p>
                {f"<div class='bg-amber-50 border border-amber-200 text-amber-800 text-xs px-4 py-3 rounded-xl mt-3'><b>⚠️ Parser flagged these concerns:</b><ul class='list-disc list-inside mt-1'>{warnings_html}</ul></div>" if warning_list else ""}
            </div>

            <div class="grid grid-cols-1 lg:grid-cols-2 gap-4 items-start">
                <div class="bg-white rounded-2xl border shadow-xs overflow-hidden lg:sticky lg:top-4" style="height: calc(100vh - 220px); min-height: 500px;">
                    {pdf_panel}
                </div>

                <form action="/api/v1/superadmin/schemes/publish/{master_id}" method="post" class="space-y-3">
                    {rows_html or "<p class='text-slate-400 text-sm italic bg-white p-6 rounded-2xl border text-center'>No rows were detected — add them manually using the button below, referencing the PDF alongside, or re-upload a clearer PDF.</p>"}
                    <button type="button" onclick="addBlankRow()" class="w-full bg-white hover:bg-slate-50 border border-dashed border-slate-300 text-slate-500 font-bold py-3 rounded-2xl text-sm transition">+ Add a Row Manually</button>
                    <div class="flex gap-3 sticky bottom-4">
                        <button type="submit" class="flex-1 bg-emerald-700 hover:bg-emerald-800 text-white font-bold py-3.5 rounded-xl text-sm transition shadow-lg">💾 Save Changes</button>
                    </div>
                </form>
            </div>
        </div>

        <script>
        let nextIdx = {len(rows)};
        function addBlankRow() {{
            const fields = {SCHEME_ROW_FIELDS!r};
            const shortFields = ['week_number', 'lesson_number'];
            const labels = {SCHEME_ROW_LABELS!r};
            const container = document.createElement('div');
            container.className = 'bg-white border rounded-2xl p-4 grid grid-cols-6 gap-2 relative';
            let inner = `<button type="button" onclick="this.closest('.bg-white').remove()" class="absolute top-2 right-2 text-rose-400 hover:text-rose-600 text-xs font-bold">✕ Remove</button><input type="hidden" name="row_ids" value="${{nextIdx}}">`;
            fields.forEach(f => {{
                const isShort = shortFields.includes(f);
                inner += `<div class="${{isShort ? 'col-span-1' : 'col-span-3'}}"><label class="text-[11px] font-bold text-slate-500 uppercase block mb-1">${{labels[f]}}</label>${{isShort ? `<input type='text' name='${{f}}_${{nextIdx}}' class='w-full border p-2 rounded-lg text-sm'>` : `<textarea name='${{f}}_${{nextIdx}}' rows='3' class='w-full border p-2 rounded-lg text-sm'></textarea>`}}</div>`;
            }});
            container.innerHTML = inner;
            document.querySelector('form').insertBefore(container, document.querySelector('form').lastElementChild.previousElementSibling);
            nextIdx++;
        }}
        </script>
    </body>
    </html>
    """


@router.post("/api/v1/superadmin/schemes/publish/{master_id}")
async def schemes_publish(master_id: int, request: Request):
    """Saves the (reviewed and corrected) rows as the current master
    content, replacing whatever was there before. The scheme was already
    published at upload time — this just updates its content, it doesn't
    gate visibility. Harmless to re-run repeatedly; published_at simply
    reflects the most recent edit."""
    auth_error = require_superadmin_session(request)
    if auth_error:
        return auth_error

    form = await request.form()
    row_ids = form.getlist("row_ids")

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM scheme_masters WHERE id = %s;", (master_id,))
            if not cur.fetchone():
                raise HTTPException(status_code=404, detail="Scheme not found.")

            # Replace every row wholesale with the reviewed version — the
            # form is the complete, corrected set the admin just approved.
            cur.execute("DELETE FROM scheme_master_rows WHERE master_id = %s;", (master_id,))
            for sort_order, idx in enumerate(row_ids):
                cur.execute("""
                    INSERT INTO scheme_master_rows (master_id, sort_order, week_number, lesson_number, strand, sub_strand, learning_outcomes, learning_experiences, key_inquiry_questions, learning_resources, assessment_methods, reflection)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
                """, (
                    master_id, sort_order,
                    (form.get(f"week_number_{idx}") or "").strip(),
                    (form.get(f"lesson_number_{idx}") or "").strip(),
                    (form.get(f"strand_{idx}") or "").strip(),
                    (form.get(f"sub_strand_{idx}") or "").strip(),
                    (form.get(f"learning_outcomes_{idx}") or "").strip(),
                    (form.get(f"learning_experiences_{idx}") or "").strip(),
                    (form.get(f"key_inquiry_questions_{idx}") or "").strip(),
                    (form.get(f"learning_resources_{idx}") or "").strip(),
                    (form.get(f"assessment_methods_{idx}") or "").strip(),
                    (form.get(f"reflection_{idx}") or "").strip(),
                ))

            cur.execute("UPDATE scheme_masters SET parse_review_status = 'published', published_at = NOW() WHERE id = %s;", (master_id,))
            conn.commit()

    # Safety net — cheap no-op if every school already has a copy, but
    # catches any school that was somehow missed (e.g. a very recent
    # registration racing the original upload's own auto-copy call).
    auto_copy_published_masters_to_all_schools()

    return RedirectResponse(url="/superadmin/schemes/list", status_code=303)


@router.post("/api/v1/superadmin/schemes/delete/{master_id}")
def schemes_delete_master(master_id: int, request: Request):
    """Deletes a master template and its own rows (via CASCADE on
    scheme_master_rows). Deliberately does NOT touch any school's
    already-imported copy — scheme_copies.master_id is ON DELETE SET
    NULL, not CASCADE, specifically so a school's own (possibly
    customized) scheme never disappears just because the master template
    it originally came from was removed."""
    auth_error = require_superadmin_session(request)
    if auth_error:
        return auth_error

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM scheme_masters WHERE id = %s;", (master_id,))
            if not cur.fetchone():
                raise HTTPException(status_code=404, detail="Scheme not found.")
            cur.execute("DELETE FROM scheme_masters WHERE id = %s;", (master_id,))
            conn.commit()

    return RedirectResponse(url="/superadmin/schemes/list", status_code=303)


@router.get("/superadmin/schemes/list", response_class=HTMLResponse)
def schemes_master_list(request: Request):
    auth_error = require_superadmin_session(request)
    if auth_error:
        return auth_error

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT m.*, COUNT(r.id) AS row_count,
                       (SELECT COUNT(*) FROM scheme_copies c WHERE c.master_id = m.id) AS import_count
                FROM scheme_masters m
                LEFT JOIN scheme_master_rows r ON r.master_id = m.id
                GROUP BY m.id ORDER BY m.created_at DESC;
            """)
            masters = cur.fetchall()

    rows_html = ""
    for m in masters:
        if m['import_count'] > 0:
            confirm_msg = (
                f"Remove this master template ({m['subject_name']} — {m['grade_name']})? "
                f"Note: {m['import_count']} school(s) already imported it \\u2014 their own copies are "
                f"completely unaffected and will keep working exactly as they are, but this master "
                f"will no longer be available for new imports. This cannot be undone."
            )
        else:
            confirm_msg = (
                f"Remove this master template ({m['subject_name']} — {m['grade_name']})? "
                f"No school has imported it yet, so nothing else is affected. This cannot be undone."
            )
        # Single quotes inside the message are escaped for the JS string
        # literal; esc() then handles the surrounding HTML attribute.
        confirm_msg_js_safe = confirm_msg.replace("'", "\\'")
        rows_html += f"""
        <div class="bg-white rounded-2xl border shadow-xs p-4 flex items-center justify-between flex-wrap gap-2">
            <div>
                <h3 class="text-sm font-bold text-slate-800">{esc(m['subject_name'])} — {esc(m['grade_name'])} <span class="text-slate-400 font-normal">({esc(m['education_level'])})</span></h3>
                <p class="text-xs text-slate-400">{esc(m['term'])} {m['year']} — {m['row_count']} row(s) — imported by {m['import_count']} school(s)</p>
            </div>
            <div class="flex gap-2 items-center">
                <span class="text-[10px] font-bold px-2 py-1 rounded-full {'bg-emerald-50 text-emerald-700 border border-emerald-200' if m['parse_review_status'] == 'published' else 'bg-amber-50 text-amber-700 border border-amber-200'}">{esc(m['parse_review_status'])}</span>
                <a href="/superadmin/schemes/review/{m['id']}" class="text-indigo-700 hover:underline text-xs font-bold">Edit →</a>
                <form action="/api/v1/superadmin/schemes/delete/{m['id']}" method="post" onsubmit="return confirm('{esc(confirm_msg_js_safe)}');">
                    <button type="submit" class="text-rose-500 hover:text-rose-700 text-xs font-bold">Remove</button>
                </form>
            </div>
        </div>
        """

    return f"""
    <!DOCTYPE html>
    <html>
    <head><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Elimu Hub | Master Schemes</title><script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script></head>
    <body class="bg-[#F7F9F8] min-h-screen">
        <header class="bg-white border-b px-6 sm:px-8 py-4 flex justify-between items-center">
            <div>
                <a href="/superadmin/dashboard" class="text-slate-400 hover:text-slate-600 text-xs font-bold block mb-1">← Back to Super Admin Portal</a>
                <h1 class="text-base font-bold text-slate-900">📘 Master Schemes of Work</h1>
            </div>
            <a href="/superadmin/schemes/upload" class="bg-indigo-800 hover:bg-indigo-900 text-white px-4 py-2 rounded-xl text-xs font-bold transition">+ Upload New</a>
        </header>
        <div class="p-4 sm:p-8 max-w-3xl mx-auto space-y-3">
            {rows_html or "<p class='text-slate-400 text-sm italic text-center py-8'>No schemes uploaded yet.</p>"}
        </div>
    </body>
    </html>
    """


# ============================================================
# School Admin — browse published masters matching their school's grades,
# import one (creating an independent copy), and assign it to the
# teacher who actually teaches that subject/grade.
# ============================================================

@router.get("/schemes/available/{school_id}", response_class=HTMLResponse)
def schemes_available_for_school(school_id: int, request: Request):
    auth_error = require_admin_session(request, school_id)
    if auth_error:
        return auth_error

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT name FROM schools WHERE id = %s;", (school_id,))
            school = cur.fetchone()

            cur.execute("""
                SELECT m.*, COUNT(r.id) AS row_count,
                       EXISTS(SELECT 1 FROM scheme_copies c WHERE c.master_id = m.id AND c.school_id = %s) AS already_imported
                FROM scheme_masters m
                LEFT JOIN scheme_master_rows r ON r.master_id = m.id
                WHERE m.parse_review_status = 'published'
                GROUP BY m.id ORDER BY m.grade_name ASC, m.subject_name ASC;
            """, (school_id,))
            masters = cur.fetchall()

    rows_html = "".join(f"""
        <div class="bg-white rounded-2xl border shadow-xs p-4 flex items-center justify-between flex-wrap gap-2">
            <div>
                <h3 class="text-sm font-bold text-slate-800">{esc(m['subject_name'])} — {esc(m['grade_name'])} <span class="text-slate-400 font-normal">({esc(m['education_level'])})</span></h3>
                <p class="text-xs text-slate-400">{esc(m['term'])} {m['year']} — {m['row_count']} lesson(s) planned</p>
            </div>
            {f'''<span class="text-xs font-bold text-emerald-700 bg-emerald-50 border border-emerald-200 px-3 py-1.5 rounded-xl">✅ Already Imported</span>''' if m['already_imported'] else f'''<a href="/schemes/import/{school_id}/{m['id']}" class="bg-indigo-700 hover:bg-indigo-800 text-white px-4 py-2 rounded-xl text-xs font-bold transition">Import to School →</a>'''}
        </div>
    """ for m in masters)

    return f"""
    <!DOCTYPE html>
    <html>
    <head><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Elimu Hub | Schemes of Work</title><script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script></head>
    <body class="bg-[#F7F9F8] min-h-screen">
        <header class="bg-white border-b px-6 sm:px-8 py-4 flex flex-col sm:flex-row sm:justify-between sm:items-center gap-2">
            <div>
                <a href="/schemes/manage/{school_id}" class="text-slate-400 hover:text-slate-600 text-xs font-bold block mb-1">← Back to Manage Schemes</a>
                <h1 class="text-base font-bold text-slate-900">📘 Schemes of Work — {esc(school['name']) if school else ''}</h1>
                <p class="text-xs text-slate-400">Master schemes published by Elimu Hub, ready to import for your school.</p>
            </div>
            <a href="/schemes/manage/{school_id}" class="bg-slate-800 hover:bg-slate-900 text-white px-4 py-2 rounded-xl text-xs font-bold transition">📋 Manage Imported Schemes</a>
        </header>
        <div class="p-4 sm:p-8 max-w-3xl mx-auto space-y-3">
            {rows_html or "<p class='text-slate-400 text-sm italic text-center py-8'>No published schemes available yet.</p>"}
        </div>
    </body>
    </html>
    """


@router.get("/schemes/import/{school_id}/{master_id}", response_class=HTMLResponse)
def schemes_import_form(school_id: int, master_id: int, request: Request):
    """Confirms the import and picks which teacher/stream it belongs to,
    in one step."""
    auth_error = require_admin_session(request, school_id)
    if auth_error:
        return auth_error

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT subject_name, grade_name, education_level, term, year FROM scheme_masters WHERE id = %s AND parse_review_status = 'published';", (master_id,))
            master = cur.fetchone()
            if not master:
                raise HTTPException(status_code=404, detail="Scheme not found or not yet published.")

            cur.execute("SELECT id, email, full_name FROM users WHERE school_id = %s AND role = 'staff' AND is_verified = TRUE ORDER BY full_name NULLS LAST, email ASC;", (school_id,))
            staff_members = cur.fetchall()

            cur.execute("""
                SELECT DISTINCT s.stream FROM students s JOIN classes c ON s.class_id = c.id
                WHERE s.school_id = %s AND c.grade_name = %s AND c.education_level = %s
                  AND (s.status IS NULL OR s.status != 'GRADUATED') ORDER BY s.stream ASC;
            """, (school_id, master['grade_name'], master['education_level']))
            streams = [r['stream'] for r in cur.fetchall()] or ['ALL']

    staff_options = "".join(f"<option value='{s['id']}'>{esc(s['full_name'] or s['email'])}</option>" for s in staff_members)
    stream_options = "".join(f"<option value='{esc(s)}'>{esc(s)}</option>" for s in streams)

    return f"""
    <!DOCTYPE html>
    <html>
    <head><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Elimu Hub | Import Scheme</title><script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script></head>
    <body class="bg-slate-100 min-h-screen p-4 sm:p-8">
        <div class="max-w-lg mx-auto space-y-4">
            <a href="/schemes/available/{school_id}" class="text-slate-500 hover:text-slate-700 text-xs font-bold inline-block">← Back to Available Schemes</a>
            <div class="bg-white p-6 rounded-2xl border shadow-xs">
            <h2 class="text-lg font-black text-slate-800">Import: {esc(master['subject_name'])} — {esc(master['grade_name'])}</h2>
            <p class="text-xs text-slate-400 mb-4">This creates your school's own copy — customizing it later never affects the master template.</p>
            <form action="/api/v1/schemes/import/{school_id}/{master_id}" method="post" class="space-y-3">
                <div>
                    <label class="text-xs font-bold text-slate-600 block mb-1">Assign to Teacher</label>
                    <select name="teacher_user_id" class="w-full border p-2.5 rounded-xl text-sm bg-white">
                        <option value="">— Assign later —</option>{staff_options}
                    </select>
                </div>
                <div>
                    <label class="text-xs font-bold text-slate-600 block mb-1">Stream</label>
                    <select name="stream" class="w-full border p-2.5 rounded-xl text-sm bg-white">{stream_options}</select>
                </div>
                <button type="submit" class="w-full bg-indigo-800 hover:bg-indigo-900 text-white font-bold py-3 rounded-xl text-sm transition">Import This Scheme</button>
            </form>
            </div>
        </div>
    </body>
    </html>
    """


@router.post("/api/v1/schemes/import/{school_id}/{master_id}")
def schemes_import_process(school_id: int, master_id: int, request: Request, teacher_user_id: str = Form(""), stream: str = Form("ALL")):
    auth_error = require_admin_session(request, school_id)
    if auth_error:
        return auth_error

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT subject_name, grade_name, education_level, term, year FROM scheme_masters WHERE id = %s;", (master_id,))
            master = cur.fetchone()
            if not master:
                raise HTTPException(status_code=404, detail="Scheme not found.")

            teacher_id = int(teacher_user_id) if teacher_user_id else None
            imported_by = request.cookies.get("session_user_id")

            cur.execute("""
                INSERT INTO scheme_copies (school_id, master_id, subject_name, grade_name, education_level, stream, term, year, teacher_user_id, imported_by_user_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id;
            """, (school_id, master_id, master['subject_name'], master['grade_name'], master['education_level'], stream, master['term'], master['year'], teacher_id, imported_by))
            copy_id = cur.fetchone()['id']

            cur.execute("SELECT * FROM scheme_master_rows WHERE master_id = %s ORDER BY sort_order ASC;", (master_id,))
            master_rows = cur.fetchall()
            for row in master_rows:
                cur.execute("""
                    INSERT INTO scheme_copy_rows (copy_id, sort_order, week_number, lesson_number, strand, sub_strand, learning_outcomes, learning_experiences, key_inquiry_questions, learning_resources, assessment_methods, reflection)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
                """, (
                    copy_id, row['sort_order'], row['week_number'], row['lesson_number'], row['strand'], row['sub_strand'],
                    row['learning_outcomes'], row['learning_experiences'], row['key_inquiry_questions'],
                    row['learning_resources'], row['assessment_methods'], row['reflection'],
                ))
            conn.commit()

    return RedirectResponse(url=f"/schemes/manage/{school_id}", status_code=303)


@router.get("/schemes/manage/{school_id}", response_class=HTMLResponse)
def schemes_manage_school(school_id: int, request: Request):
    """Admin's view of every imported scheme at their school, and who
    it's assigned to."""
    auth_error = require_admin_session(request, school_id)
    if auth_error:
        return auth_error

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT name FROM schools WHERE id = %s;", (school_id,))
            school = cur.fetchone()

            cur.execute("""
                SELECT c.*, u.full_name AS teacher_name, u.email AS teacher_email
                FROM scheme_copies c LEFT JOIN users u ON c.teacher_user_id = u.id
                WHERE c.school_id = %s ORDER BY c.grade_name ASC, c.subject_name ASC;
            """, (school_id,))
            copies = cur.fetchall()

            cur.execute("SELECT id, email, full_name FROM users WHERE school_id = %s AND role = 'staff' AND is_verified = TRUE ORDER BY full_name NULLS LAST, email ASC;", (school_id,))
            staff_members = cur.fetchall()

    # Grouped School → Term → Grade/Class → Scheme(s), per the requested
    # reorganization — a flat list stops being usable once a school has
    # imported schemes across multiple terms and grades.
    by_term = {}
    for c in copies:
        by_term.setdefault(c['term'], {}).setdefault(c['grade_name'], []).append(c)

    def _scheme_card(c):
        staff_options = "".join(f"<option value='{s['id']}' {'selected' if s['id'] == c['teacher_user_id'] else ''}>{esc(s['full_name'] or s['email'])}</option>" for s in staff_members)
        confirm_msg = f"Remove {c['subject_name']} — {c['grade_name']} from your school? This deletes your school\\'s copy of this scheme permanently, including any edits made to it. This cannot be undone."
        return f"""
        <div class="bg-white rounded-2xl border shadow-xs p-4 flex items-center justify-between flex-wrap gap-3">
            <div>
                <h3 class="text-sm font-bold text-slate-800">{esc(c['subject_name'])} {esc(c['stream']) if c['stream'] != 'ALL' else ''}</h3>
                <p class="text-xs text-slate-400">{c['year']}</p>
            </div>
            <div class="flex items-center gap-2">
                <form action="/api/v1/schemes/assign/{school_id}/{c['id']}" method="post" class="flex items-center gap-2">
                    <select name="teacher_user_id" class="border p-2 rounded-lg text-xs bg-white">
                        <option value="">— Unassigned —</option>{staff_options}
                    </select>
                    <button type="submit" class="bg-indigo-700 hover:bg-indigo-800 text-white px-3 py-2 rounded-lg text-xs font-bold transition">Save</button>
                    <a href="/schemes/edit/{school_id}/{c['id']}" class="text-indigo-700 hover:underline text-xs font-bold ml-1">Edit →</a>
                </form>
                <form action="/api/v1/schemes/delete-copy/{school_id}/{c['id']}" method="post" onsubmit="return confirm('{esc(confirm_msg)}');">
                    <button type="submit" class="text-rose-500 hover:text-rose-700 text-xs font-bold">Remove</button>
                </form>
            </div>
        </div>
        """

    sections_html = ""
    for term in sorted(by_term.keys()):
        grades_html = ""
        for grade_name in sorted(by_term[term].keys()):
            cards_html = "".join(_scheme_card(c) for c in by_term[term][grade_name])
            grades_html += f"""
            <div class="mb-4">
                <h3 class="text-xs font-bold uppercase tracking-wider text-slate-400 mb-2 pl-1">{esc(grade_name)}</h3>
                <div class="space-y-2">{cards_html}</div>
            </div>
            """
        sections_html += f"""
        <div class="mb-8">
            <h2 class="text-sm font-black text-slate-700 mb-3 pb-2 border-b-2 border-indigo-100">📅 {esc(term)}</h2>
            {grades_html}
        </div>
        """

    return f"""
    <!DOCTYPE html>
    <html>
    <head><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Elimu Hub | Manage Schemes</title><script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script></head>
    <body class="bg-[#F7F9F8] min-h-screen">
        <header class="bg-white border-b px-6 sm:px-8 py-4 flex flex-col sm:flex-row sm:justify-between sm:items-center gap-2">
            <div>
                <a href="/admin/dashboard/{school_id}" class="text-slate-400 hover:text-slate-600 text-xs font-bold block mb-1">← Back to Dashboard</a>
                <h1 class="text-base font-bold text-slate-900">📋 Manage Schemes — {esc(school['name']) if school else ''}</h1>
                <p class="text-xs text-slate-400">Imported schemes, organized by term and grade.</p>
            </div>
            <a href="/schemes/available/{school_id}" class="bg-indigo-800 hover:bg-indigo-900 text-white px-4 py-2 rounded-xl text-xs font-bold transition">+ Import More</a>
        </header>
        <div class="p-4 sm:p-8 max-w-3xl mx-auto">
            {sections_html or "<p class='text-slate-400 text-sm italic text-center py-8'>No schemes imported yet.</p>"}
        </div>
    </body>
    </html>
    """


@router.post("/api/v1/schemes/delete-copy/{school_id}/{copy_id}")
def schemes_delete_copy(school_id: int, copy_id: int, request: Request):
    """Deletes a school's own imported scheme copy. Independent of
    whether the master template it originally came from still exists —
    this is exactly what's needed for a copy that's become orphaned
    (master deleted) or simply no longer wanted, neither of which the
    super admin's master-level delete can address."""
    auth_error = require_admin_session(request, school_id)
    if auth_error:
        return auth_error

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM scheme_copies WHERE id = %s AND school_id = %s;", (copy_id, school_id))
            if not cur.fetchone():
                raise HTTPException(status_code=404, detail="Scheme not found.")
            cur.execute("DELETE FROM scheme_copies WHERE id = %s AND school_id = %s;", (copy_id, school_id))
            conn.commit()

    return RedirectResponse(url=f"/schemes/manage/{school_id}", status_code=303)


@router.post("/api/v1/schemes/assign/{school_id}/{copy_id}")
def schemes_assign_teacher(school_id: int, copy_id: int, request: Request, teacher_user_id: str = Form("")):
    auth_error = require_admin_session(request, school_id)
    if auth_error:
        return auth_error

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            teacher_id = int(teacher_user_id) if teacher_user_id else None
            cur.execute("UPDATE scheme_copies SET teacher_user_id = %s WHERE id = %s AND school_id = %s;", (teacher_id, copy_id, school_id))
            conn.commit()

    return RedirectResponse(url=f"/schemes/manage/{school_id}", status_code=303)


# ============================================================
# Staff (teacher) — view schemes assigned to them, edit any field freely
# (explicit product decision — this is their document to adapt), and
# print a professional document with their name/TSC number in the header.
# ============================================================

@router.get("/schemes/my-schemes/{school_id}", response_class=HTMLResponse)
def my_schemes_list(school_id: int, request: Request):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    user_id = request.cookies.get("session_user_id")
    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT name FROM schools WHERE id = %s;", (school_id,))
            school = cur.fetchone()

            cur.execute("""
                SELECT c.*, (SELECT COUNT(*) FROM scheme_copy_rows r WHERE r.copy_id = c.id) AS row_count
                FROM scheme_copies c
                WHERE c.school_id = %s
                ORDER BY c.grade_name ASC, c.subject_name ASC;
            """, (school_id,))
            my_schemes = cur.fetchall()

    # Grouped by Term → Grade, consistent with the admin's Manage Schemes
    # page — most teachers only have a handful of schemes, but grouping
    # still helps once a teacher has schemes across multiple terms.
    by_term = {}
    for s in my_schemes:
        by_term.setdefault(s['term'], {}).setdefault(s['grade_name'], []).append(s)

    def _scheme_card(s):
        is_assigned_to_me = str(s['teacher_user_id']) == str(user_id)
        badge = "<span class='text-[10px] font-bold px-2 py-0.5 rounded-full bg-indigo-50 text-indigo-700 border border-indigo-200'>Assigned to you</span>" if is_assigned_to_me else ""
        return f"""
        <a href="/schemes/edit/{school_id}/{s['id']}" class="bg-white rounded-2xl border shadow-xs p-4 flex items-center justify-between flex-wrap gap-2 hover:shadow-md transition-shadow block">
            <div>
                <h3 class="text-sm font-bold text-slate-800">{esc(s['subject_name'])} {esc(s['stream']) if s['stream'] != 'ALL' else ''} {badge}</h3>
                <p class="text-xs text-slate-400">{s['year']} — {s['row_count']} lesson(s)</p>
            </div>
            <span class="text-xs text-indigo-700 font-bold">Open →</span>
        </a>
        """

    sections_html = ""
    for term in sorted(by_term.keys()):
        grades_html = ""
        for grade_name in sorted(by_term[term].keys()):
            cards_html = "".join(_scheme_card(s) for s in by_term[term][grade_name])
            grades_html += f"""
            <div class="mb-4">
                <h3 class="text-xs font-bold uppercase tracking-wider text-slate-400 mb-2 pl-1">{esc(grade_name)}</h3>
                <div class="space-y-2">{cards_html}</div>
            </div>
            """
        sections_html += f"""
        <div class="mb-8">
            <h2 class="text-sm font-black text-slate-700 mb-3 pb-2 border-b-2 border-indigo-100">📅 {esc(term)}</h2>
            {grades_html}
        </div>
        """

    return f"""
    <!DOCTYPE html>
    <html>
    <head><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Elimu Hub | My Schemes of Work</title><script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script></head>
    <body class="bg-[#F7F9F8] min-h-screen">
        <header class="bg-white border-b px-6 sm:px-8 py-4">
            <a href="{get_dashboard_url(request, school_id)}" class="text-slate-400 hover:text-slate-600 text-xs font-bold block mb-1">← Back to Dashboard</a>
            <h1 class="text-base font-bold text-slate-900">📘 My Schemes of Work — {esc(school['name']) if school else ''}</h1>
            <p class="text-xs text-slate-400">Every scheme available at your school — open one to review, customize, or print it. Ones assigned specifically to you are marked below.</p>
        </header>
        <div class="p-4 sm:p-8 max-w-3xl mx-auto">
            {sections_html or "<p class='text-slate-400 text-sm italic text-center py-8'>No schemes have been uploaded for your school's grades yet.</p>"}
        </div>
    </body>
    </html>
    """


@router.get("/schemes/edit/{school_id}/{copy_id}", response_class=HTMLResponse)
def scheme_copy_editor(school_id: int, copy_id: int, request: Request, saved: str = None):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM scheme_copies WHERE id = %s AND school_id = %s;", (copy_id, school_id))
            copy = cur.fetchone()
            if not copy:
                raise HTTPException(status_code=404, detail="Scheme not found.")

            cur.execute("SELECT * FROM scheme_copy_rows WHERE copy_id = %s ORDER BY sort_order ASC;", (copy_id,))
            rows = cur.fetchall()

    def _row_block(row_data, idx):
        field_inputs = ""
        for field in SCHEME_ROW_FIELDS:
            value = esc(row_data.get(field, "") or "")
            label = SCHEME_ROW_LABELS[field]
            is_short = field in ("week_number", "lesson_number")
            field_inputs += f"""
            <div class="{'col-span-1' if is_short else 'col-span-3'}">
                <label class="text-[11px] font-bold text-slate-500 uppercase block mb-1">{esc(label)}</label>
                {"<input type='text' name='" + field + "_" + str(idx) + "' value='" + value + "' class='w-full border p-2 rounded-lg text-sm'>" if is_short else "<textarea name='" + field + "_" + str(idx) + "' rows='3' class='w-full border p-2 rounded-lg text-sm'>" + value + "</textarea>"}
            </div>
            """
        return f"""
        <div class="bg-white border rounded-2xl p-4 grid grid-cols-6 gap-2 relative">
            <button type="button" onclick="this.closest('.bg-white').remove()" class="absolute top-2 right-2 text-rose-400 hover:text-rose-600 text-xs font-bold">✕ Remove</button>
            <input type="hidden" name="row_ids" value="{idx}">
            {field_inputs}
        </div>
        """

    rows_html = "".join(_row_block(r, i) for i, r in enumerate(rows))

    viewer = get_current_session_user(request)
    back_url = f"/schemes/manage/{school_id}" if viewer and viewer['role'] != 'staff' else f"/schemes/my-schemes/{school_id}"
    back_label = "Manage Schemes" if viewer and viewer['role'] != 'staff' else "My Schemes of Work"

    return f"""
    <!DOCTYPE html>
    <html>
    <head><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Elimu Hub | {esc(copy['subject_name'])} Scheme</title><script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script></head>
    <body class="bg-slate-100 min-h-screen p-4 sm:p-8">
        <div class="max-w-5xl mx-auto space-y-4">
            <a href="{back_url}" class="text-slate-500 hover:text-slate-700 text-xs font-bold inline-block">← Back to {esc(back_label)}</a>
            <div class="bg-white p-6 rounded-2xl border shadow-xs">
                <div class="flex items-center justify-between flex-wrap gap-2">
                    <h2 class="text-lg font-black text-slate-800">{esc(copy['subject_name'])} — {esc(copy['grade_name'])} {esc(copy['stream']) if copy['stream'] != 'ALL' else ''} ({esc(copy['term'])} {copy['year']})</h2>
                    <a href="/schemes/print/{school_id}/{copy_id}" target="_blank" class="bg-slate-800 hover:bg-slate-900 text-white px-4 py-2 rounded-xl text-xs font-bold transition">🖨 Print</a>
                </div>
                <p class="text-xs text-slate-400 mt-1">Edit any field freely — this is your own copy, changes here never affect the master template or other schools.</p>
                {"<div class='bg-emerald-50 border border-emerald-200 text-emerald-800 text-xs px-4 py-2 rounded-xl mt-3'>✅ Saved.</div>" if saved else ""}
            </div>

            <details class="bg-white rounded-2xl border shadow-xs">
                <summary class="p-4 cursor-pointer text-sm font-bold text-slate-700 select-none">👤 Teacher Details (shown on the printed document)</summary>
                <div class="p-4 pt-0 grid grid-cols-2 gap-3">
                    <form action="/api/v1/schemes/details/{school_id}/{copy_id}" method="post" class="col-span-2 grid grid-cols-2 gap-3">
                        <div>
                            <label class="text-xs font-bold text-slate-600 block mb-1">Teacher Name</label>
                            <input type="text" name="teacher_name_override" value="{esc(copy['teacher_name_override'] or '')}" class="w-full border p-2 rounded-lg text-sm">
                        </div>
                        <div>
                            <label class="text-xs font-bold text-slate-600 block mb-1">TSC Number</label>
                            <input type="text" name="tsc_number_override" value="{esc(copy['tsc_number_override'] or '')}" class="w-full border p-2 rounded-lg text-sm">
                        </div>
                        <button type="submit" class="col-span-2 bg-slate-800 hover:bg-slate-900 text-white font-bold py-2 rounded-xl text-sm transition">Save Details</button>
                    </form>
                </div>
            </details>

            <form action="/api/v1/schemes/save/{school_id}/{copy_id}" method="post" class="space-y-3">
                {rows_html or "<p class='text-slate-400 text-sm italic bg-white p-6 rounded-2xl border text-center'>No lessons yet — add one below.</p>"}
                <button type="button" onclick="addBlankRow()" class="w-full bg-white hover:bg-slate-50 border border-dashed border-slate-300 text-slate-500 font-bold py-3 rounded-2xl text-sm transition">+ Add a Lesson</button>
                <button type="submit" class="w-full bg-emerald-700 hover:bg-emerald-800 text-white font-bold py-3.5 rounded-xl text-sm transition shadow-lg sticky bottom-4">💾 Save Changes</button>
            </form>
        </div>

        <script>
        let nextIdx = {len(rows)};
        function addBlankRow() {{
            const fields = {SCHEME_ROW_FIELDS!r};
            const shortFields = ['week_number', 'lesson_number'];
            const labels = {SCHEME_ROW_LABELS!r};
            const container = document.createElement('div');
            container.className = 'bg-white border rounded-2xl p-4 grid grid-cols-6 gap-2 relative';
            let inner = `<button type="button" onclick="this.closest('.bg-white').remove()" class="absolute top-2 right-2 text-rose-400 hover:text-rose-600 text-xs font-bold">✕ Remove</button><input type="hidden" name="row_ids" value="${{nextIdx}}">`;
            fields.forEach(f => {{
                const isShort = shortFields.includes(f);
                inner += `<div class="${{isShort ? 'col-span-1' : 'col-span-3'}}"><label class="text-[11px] font-bold text-slate-500 uppercase block mb-1">${{labels[f]}}</label>${{isShort ? `<input type='text' name='${{f}}_${{nextIdx}}' class='w-full border p-2 rounded-lg text-sm'>` : `<textarea name='${{f}}_${{nextIdx}}' rows='3' class='w-full border p-2 rounded-lg text-sm'></textarea>`}}</div>`;
            }});
            container.innerHTML = inner;
            const form = document.querySelector('form[action^="/api/v1/schemes/save"]');
            form.insertBefore(container, form.lastElementChild.previousElementSibling);
            nextIdx++;
        }}
        </script>
    </body>
    </html>
    """


@router.post("/api/v1/schemes/details/{school_id}/{copy_id}")
async def scheme_copy_save_details(school_id: int, copy_id: int, request: Request):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    form = await request.form()
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE scheme_copies SET teacher_name_override = %s, tsc_number_override = %s
                WHERE id = %s AND school_id = %s;
            """, ((form.get("teacher_name_override") or "").strip() or None, (form.get("tsc_number_override") or "").strip() or None, copy_id, school_id))
            conn.commit()

    return RedirectResponse(url=f"/schemes/edit/{school_id}/{copy_id}?saved=1", status_code=303)


@router.post("/api/v1/schemes/save/{school_id}/{copy_id}")
async def scheme_copy_save_rows(school_id: int, copy_id: int, request: Request):
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    form = await request.form()
    row_ids = form.getlist("row_ids")

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM scheme_copies WHERE id = %s AND school_id = %s;", (copy_id, school_id))
            if not cur.fetchone():
                raise HTTPException(status_code=404, detail="Scheme not found.")

            cur.execute("DELETE FROM scheme_copy_rows WHERE copy_id = %s;", (copy_id,))
            for sort_order, idx in enumerate(row_ids):
                cur.execute("""
                    INSERT INTO scheme_copy_rows (copy_id, sort_order, week_number, lesson_number, strand, sub_strand, learning_outcomes, learning_experiences, key_inquiry_questions, learning_resources, assessment_methods, reflection)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
                """, (
                    copy_id, sort_order,
                    (form.get(f"week_number_{idx}") or "").strip(),
                    (form.get(f"lesson_number_{idx}") or "").strip(),
                    (form.get(f"strand_{idx}") or "").strip(),
                    (form.get(f"sub_strand_{idx}") or "").strip(),
                    (form.get(f"learning_outcomes_{idx}") or "").strip(),
                    (form.get(f"learning_experiences_{idx}") or "").strip(),
                    (form.get(f"key_inquiry_questions_{idx}") or "").strip(),
                    (form.get(f"learning_resources_{idx}") or "").strip(),
                    (form.get(f"assessment_methods_{idx}") or "").strip(),
                    (form.get(f"reflection_{idx}") or "").strip(),
                ))
            conn.commit()

    return RedirectResponse(url=f"/schemes/edit/{school_id}/{copy_id}?saved=1", status_code=303)


@router.get("/schemes/print/{school_id}/{copy_id}", response_class=HTMLResponse)
def scheme_copy_print(school_id: int, copy_id: int, request: Request):
    """A professional printable scheme of work — teacher name/TSC number
    in the header, full lesson table below. Purely read-only."""
    auth_error = require_school_session(request, school_id)
    if auth_error:
        return auth_error

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM schools WHERE id = %s;", (school_id,))
            school = cur.fetchone()

            cur.execute("""
                SELECT c.*, u.full_name AS teacher_name_from_account
                FROM scheme_copies c LEFT JOIN users u ON c.teacher_user_id = u.id
                WHERE c.id = %s AND c.school_id = %s;
            """, (copy_id, school_id))
            copy = cur.fetchone()
            if not copy:
                raise HTTPException(status_code=404, detail="Scheme not found.")

            cur.execute("SELECT * FROM scheme_copy_rows WHERE copy_id = %s ORDER BY sort_order ASC;", (copy_id,))
            rows = cur.fetchall()

    teacher_display_name = copy['teacher_name_override'] or copy['teacher_name_from_account'] or "___________________"
    tsc_display = copy['tsc_number_override'] or "___________________"

    logo_src = school.get('logo_url') if school else None
    logo_html = ""
    if logo_src:
        final_src = logo_src if logo_src.startswith("http") else f"/{logo_src.lstrip('/')}"
        logo_html = f"<img src='{final_src}' style='width:60px;height:60px;object-fit:contain;' />"

    rows_html = ""
    for r in rows:
        def _fmt(field):
            val = r.get(field) or ""
            return esc(val).replace("\n", "<br>")
        rows_html += f"""
        <tr>
            <td style="padding:7px 8px;border:1px solid #cbd5e1;text-align:center;">{esc(r['week_number'] or '')}</td>
            <td style="padding:7px 8px;border:1px solid #cbd5e1;text-align:center;">{esc(r['lesson_number'] or '')}</td>
            <td style="padding:7px 8px;border:1px solid #cbd5e1;">{_fmt('strand')}</td>
            <td style="padding:7px 8px;border:1px solid #cbd5e1;">{_fmt('sub_strand')}</td>
            <td style="padding:7px 8px;border:1px solid #cbd5e1;">{_fmt('learning_outcomes')}</td>
            <td style="padding:7px 8px;border:1px solid #cbd5e1;">{_fmt('learning_experiences')}</td>
            <td style="padding:7px 8px;border:1px solid #cbd5e1;">{_fmt('key_inquiry_questions')}</td>
            <td style="padding:7px 8px;border:1px solid #cbd5e1;">{_fmt('learning_resources')}</td>
            <td style="padding:7px 8px;border:1px solid #cbd5e1;">{_fmt('assessment_methods')}</td>
            <td style="padding:7px 8px;border:1px solid #cbd5e1;">{_fmt('reflection')}</td>
        </tr>
        """

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Elimu Hub | Scheme of Work — {esc(copy['subject_name'])}</title>
        <style>
            @page {{ size: A4 landscape; margin: 8mm; }}
            body {{ font-family: Arial, sans-serif; color: #1e293b; padding: 16px; font-size: 14px; }}
            @media print {{ .no-print {{ display: none !important; }} body {{ padding: 0; }} }}
            table {{ width: 100%; border-collapse: collapse; margin-top: 10px; }}
            th {{ background: #f1f5f9; border: 1px solid #cbd5e1; padding: 8px; font-size: 13px; text-transform: uppercase; }}
            td {{ font-size: 13px; line-height: 1.4; }}
        </style>
    </head>
    <body>
        <div class="no-print" style="text-align:right; margin-bottom:12px;">
            <button onclick="window.print()" style="background:#4f46e5;color:white;border:none;padding:10px 18px;border-radius:8px;font-weight:bold;cursor:pointer;">🖨 Print / Save as PDF</button>
        </div>
        <div style="display:flex;align-items:center;gap:14px;border-bottom:3px double #4f46e5;padding-bottom:10px;">
            {logo_html}
            <div>
                <h1 style="margin:0;font-size:21px;">{esc(school['name']) if school else ''}</h1>
                <p style="margin:2px 0 0;font-size:15px;font-weight:bold;">SCHEME OF WORK</p>
            </div>
        </div>
        <table style="margin-top:10px;border:none;">
            <tr>
                <td style="border:none;padding:3px 0;font-size:14px;"><b>Teacher:</b> {esc(teacher_display_name)}</td>
                <td style="border:none;padding:3px 0;font-size:14px;"><b>TSC No.:</b> {esc(tsc_display)}</td>
                <td style="border:none;padding:3px 0;font-size:14px;"><b>Subject:</b> {esc(copy['subject_name'])}</td>
            </tr>
            <tr>
                <td style="border:none;padding:3px 0;font-size:14px;"><b>Grade:</b> {esc(copy['grade_name'])} {esc(copy['stream']) if copy['stream'] != 'ALL' else ''}</td>
                <td style="border:none;padding:3px 0;font-size:14px;"><b>Term:</b> {esc(copy['term'])}</td>
                <td style="border:none;padding:3px 0;font-size:14px;"><b>Year:</b> {copy['year']}</td>
            </tr>
        </table>
        <table>
            <thead>
                <tr>
                    <th>Wk</th><th>Lsn</th><th>Strand</th><th>Sub-Strand</th><th>Learning Outcomes</th>
                    <th>Learning Experiences</th><th>Key Inquiry Question(s)</th><th>Learning Resources</th>
                    <th>Assessment Methods</th><th>Reflection</th>
                </tr>
            </thead>
            <tbody>{rows_html or "<tr><td colspan='10' style='padding:20px;text-align:center;color:#94a3b8;'>No lessons added yet.</td></tr>"}</tbody>
        </table>
        <p style="margin-top:16px;font-size:12px;color:#94a3b8;text-align:center;">Generated by Elimu Hub — {esc(school['name']) if school else ''}</p>
    </body>
    </html>
    """
