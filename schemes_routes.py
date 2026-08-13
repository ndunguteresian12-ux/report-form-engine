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
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from shared import (
    esc,
    get_db_connection,
    RealDictCursor,
    require_school_session,
    require_admin_session,
    require_superadmin_session,
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
    "lesson_number": ["lsn", "lssn"],
    "strand": ["strand", "mada kuu", "theme"],
    "sub_strand": ["substrand", "sub strand", "mada ndogo"],
    "learning_outcomes": ["learningoutcomes", "specificlearningoutcomes", "objectives", "shabaha", "lessonlearningoutcomes"],
    "learning_experiences": ["learningexperiences", "coreactivities", "shughuli"],
    "key_inquiry_questions": ["keyinquiryquestion", "keyinquiryquestions", "kiq", "maswalidadisi"],
    "learning_resources": ["learningresources", "resources", "nyenzo"],
    "assessment_methods": ["assessmentmethods", "assessment", "tathmini"],
    "reflection": ["reflection", "remarks", "maoni"],
}


def _normalize_header(text):
    return re.sub(r"[^a-z]", "", (text or "").lower())


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
    for field, patterns in HEADER_MATCH_PATTERNS.items():
        if normalized in patterns:
            return field
    # Fall back to substring matching, but only for patterns that are at
    # least 5 characters — short patterns are exactly what causes false
    # substring collisions like the one above.
    for field, patterns in HEADER_MATCH_PATTERNS.items():
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
        <div class="max-w-lg mx-auto bg-white p-6 rounded-2xl border shadow-xs">
            <h2 class="text-lg font-black text-slate-800">📘 Upload Master Scheme of Work</h2>
            <p class="text-xs text-slate-400 mb-4">Upload a PDF — the system will try to detect the table and split it into rows automatically. You'll review and correct every row on the next screen before this becomes available to schools.</p>
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
            cur.execute("""
                INSERT INTO scheme_masters (subject_name, grade_name, education_level, term, year, title, source_pdf_filename, parse_review_status, created_by_user_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'draft', %s) RETURNING id;
            """, (subject_name.strip(), grade_name.strip(), education_level, term, year, f"{subject_name.strip()} — {grade_name.strip()}", pdf_file.filename, recorded_by))
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

    warning_param = urllib.parse.quote("|".join(parse_warnings)) if parse_warnings else ""
    return RedirectResponse(url=f"/superadmin/schemes/review/{master_id}?warnings={warning_param}", status_code=303)


@router.get("/superadmin/schemes/review/{master_id}", response_class=HTMLResponse)
def schemes_review_form(master_id: int, request: Request, warnings: str = ""):
    auth_error = require_superadmin_session(request)
    if auth_error:
        return auth_error

    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM scheme_masters WHERE id = %s;", (master_id,))
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
                <label class="text-[10px] font-bold text-slate-400 uppercase block mb-0.5">{esc(label)}</label>
                {"<input type='text' name='" + field + "_" + str(idx) + "' value='" + value + "' class='w-full border p-1.5 rounded-lg text-xs'>" if is_short else "<textarea name='" + field + "_" + str(idx) + "' rows='2' class='w-full border p-1.5 rounded-lg text-xs'>" + value + "</textarea>"}
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

    return f"""
    <!DOCTYPE html>
    <html>
    <head><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Elimu Hub | Review Scheme</title><script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script></head>
    <body class="bg-slate-100 min-h-screen p-4 sm:p-8">
        <div class="max-w-5xl mx-auto space-y-4">
            <div class="bg-white p-6 rounded-2xl border shadow-xs">
                <h2 class="text-lg font-black text-slate-800">📘 Review — {esc(master['subject_name'])} — {esc(master['grade_name'])} ({esc(master['term'])} {master['year']})</h2>
                <p class="text-xs text-slate-400 mt-1">{len(rows)} row(s) detected. Check every row carefully — this parse is best-effort, not guaranteed accurate. Fix anything wrong before publishing; schools will import exactly what you approve here.</p>
                {f"<div class='bg-amber-50 border border-amber-200 text-amber-800 text-xs px-4 py-3 rounded-xl mt-3'><b>⚠️ Parser flagged these concerns:</b><ul class='list-disc list-inside mt-1'>{warnings_html}</ul></div>" if warning_list else ""}
            </div>

            <form action="/api/v1/superadmin/schemes/publish/{master_id}" method="post" class="space-y-3">
                {rows_html or "<p class='text-slate-400 text-sm italic bg-white p-6 rounded-2xl border text-center'>No rows were detected — add them manually using the button below, or re-upload a clearer PDF.</p>"}
                <button type="button" onclick="addBlankRow()" class="w-full bg-white hover:bg-slate-50 border border-dashed border-slate-300 text-slate-500 font-bold py-3 rounded-2xl text-sm transition">+ Add a Row Manually</button>
                <div class="flex gap-3 sticky bottom-4">
                    <button type="submit" class="flex-1 bg-emerald-700 hover:bg-emerald-800 text-white font-bold py-3.5 rounded-xl text-sm transition shadow-lg">✅ Approve &amp; Publish to Schools</button>
                </div>
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
                inner += `<div class="${{isShort ? 'col-span-1' : 'col-span-3'}}"><label class="text-[10px] font-bold text-slate-400 uppercase block mb-0.5">${{labels[f]}}</label>${{isShort ? `<input type='text' name='${{f}}_${{nextIdx}}' class='w-full border p-1.5 rounded-lg text-xs'>` : `<textarea name='${{f}}_${{nextIdx}}' rows='2' class='w-full border p-1.5 rounded-lg text-xs'></textarea>`}}</div>`;
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
    """Saves the (human-reviewed and corrected) rows as the final master
    content, replacing whatever the initial parse produced, and marks the
    scheme published — visible to schools from this point on."""
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

    rows_html = "".join(f"""
        <div class="bg-white rounded-2xl border shadow-xs p-4 flex items-center justify-between flex-wrap gap-2">
            <div>
                <h3 class="text-sm font-bold text-slate-800">{esc(m['subject_name'])} — {esc(m['grade_name'])} <span class="text-slate-400 font-normal">({esc(m['education_level'])})</span></h3>
                <p class="text-xs text-slate-400">{esc(m['term'])} {m['year']} — {m['row_count']} row(s) — imported by {m['import_count']} school(s)</p>
            </div>
            <div class="flex gap-2">
                <span class="text-[10px] font-bold px-2 py-1 rounded-full {'bg-emerald-50 text-emerald-700 border border-emerald-200' if m['parse_review_status'] == 'published' else 'bg-amber-50 text-amber-700 border border-amber-200'}">{esc(m['parse_review_status'])}</span>
                <a href="/superadmin/schemes/review/{m['id']}" class="text-indigo-700 hover:underline text-xs font-bold">Edit →</a>
            </div>
        </div>
    """ for m in masters)

    return f"""
    <!DOCTYPE html>
    <html>
    <head><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Elimu Hub | Master Schemes</title><script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script></head>
    <body class="bg-[#F7F9F8] min-h-screen">
        <header class="bg-white border-b px-6 sm:px-8 py-4 flex justify-between items-center">
            <h1 class="text-base font-bold text-slate-900">📘 Master Schemes of Work</h1>
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
            cur.execute("SELECT * FROM scheme_masters WHERE id = %s AND parse_review_status = 'published';", (master_id,))
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
        <div class="max-w-lg mx-auto bg-white p-6 rounded-2xl border shadow-xs">
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
            cur.execute("SELECT * FROM scheme_masters WHERE id = %s;", (master_id,))
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

    rows_html = ""
    for c in copies:
        staff_options = "".join(f"<option value='{s['id']}' {'selected' if s['id'] == c['teacher_user_id'] else ''}>{esc(s['full_name'] or s['email'])}</option>" for s in staff_members)
        rows_html += f"""
        <div class="bg-white rounded-2xl border shadow-xs p-4 flex items-center justify-between flex-wrap gap-3">
            <div>
                <h3 class="text-sm font-bold text-slate-800">{esc(c['subject_name'])} — {esc(c['grade_name'])} {esc(c['stream']) if c['stream'] != 'ALL' else ''}</h3>
                <p class="text-xs text-slate-400">{esc(c['term'])} {c['year']}</p>
            </div>
            <form action="/api/v1/schemes/assign/{school_id}/{c['id']}" method="post" class="flex items-center gap-2">
                <select name="teacher_user_id" class="border p-2 rounded-lg text-xs bg-white">
                    <option value="">— Unassigned —</option>{staff_options}
                </select>
                <button type="submit" class="bg-indigo-700 hover:bg-indigo-800 text-white px-3 py-2 rounded-lg text-xs font-bold transition">Save</button>
                <a href="/schemes/edit/{school_id}/{c['id']}" class="text-indigo-700 hover:underline text-xs font-bold ml-1">Edit →</a>
            </form>
        </div>
        """

    return f"""
    <!DOCTYPE html>
    <html>
    <head><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Elimu Hub | Manage Schemes</title><script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script></head>
    <body class="bg-[#F7F9F8] min-h-screen">
        <header class="bg-white border-b px-6 sm:px-8 py-4 flex flex-col sm:flex-row sm:justify-between sm:items-center gap-2">
            <div>
                <h1 class="text-base font-bold text-slate-900">📋 Manage Schemes — {esc(school['name']) if school else ''}</h1>
                <p class="text-xs text-slate-400">Imported schemes and who they're assigned to.</p>
            </div>
            <a href="/schemes/available/{school_id}" class="bg-indigo-800 hover:bg-indigo-900 text-white px-4 py-2 rounded-xl text-xs font-bold transition">+ Import More</a>
        </header>
        <div class="p-4 sm:p-8 max-w-3xl mx-auto space-y-3">
            {rows_html or "<p class='text-slate-400 text-sm italic text-center py-8'>No schemes imported yet.</p>"}
        </div>
    </body>
    </html>
    """


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
                WHERE c.school_id = %s AND c.teacher_user_id = %s
                ORDER BY c.grade_name ASC, c.subject_name ASC;
            """, (school_id, user_id))
            my_schemes = cur.fetchall()

    rows_html = "".join(f"""
        <a href="/schemes/edit/{school_id}/{s['id']}" class="bg-white rounded-2xl border shadow-xs p-4 flex items-center justify-between flex-wrap gap-2 hover:shadow-md transition-shadow block">
            <div>
                <h3 class="text-sm font-bold text-slate-800">{esc(s['subject_name'])} — {esc(s['grade_name'])} {esc(s['stream']) if s['stream'] != 'ALL' else ''}</h3>
                <p class="text-xs text-slate-400">{esc(s['term'])} {s['year']} — {s['row_count']} lesson(s)</p>
            </div>
            <span class="text-xs text-indigo-700 font-bold">Open →</span>
        </a>
    """ for s in my_schemes)

    return f"""
    <!DOCTYPE html>
    <html>
    <head><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Elimu Hub | My Schemes of Work</title><script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script></head>
    <body class="bg-[#F7F9F8] min-h-screen">
        <header class="bg-white border-b px-6 sm:px-8 py-4">
            <h1 class="text-base font-bold text-slate-900">📘 My Schemes of Work — {esc(school['name']) if school else ''}</h1>
            <p class="text-xs text-slate-400">Schemes assigned to you — open one to review, customize, or print it.</p>
        </header>
        <div class="p-4 sm:p-8 max-w-3xl mx-auto space-y-3">
            {rows_html or "<p class='text-slate-400 text-sm italic text-center py-8'>No schemes assigned to you yet — ask your admin to assign one.</p>"}
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
                <label class="text-[10px] font-bold text-slate-400 uppercase block mb-0.5">{esc(label)}</label>
                {"<input type='text' name='" + field + "_" + str(idx) + "' value='" + value + "' class='w-full border p-1.5 rounded-lg text-xs'>" if is_short else "<textarea name='" + field + "_" + str(idx) + "' rows='2' class='w-full border p-1.5 rounded-lg text-xs'>" + value + "</textarea>"}
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

    return f"""
    <!DOCTYPE html>
    <html>
    <head><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Elimu Hub | {esc(copy['subject_name'])} Scheme</title><script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script></head>
    <body class="bg-slate-100 min-h-screen p-4 sm:p-8">
        <div class="max-w-5xl mx-auto space-y-4">
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
                inner += `<div class="${{isShort ? 'col-span-1' : 'col-span-3'}}"><label class="text-[10px] font-bold text-slate-400 uppercase block mb-0.5">${{labels[f]}}</label>${{isShort ? `<input type='text' name='${{f}}_${{nextIdx}}' class='w-full border p-1.5 rounded-lg text-xs'>` : `<textarea name='${{f}}_${{nextIdx}}' rows='2' class='w-full border p-1.5 rounded-lg text-xs'></textarea>`}}</div>`;
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
            <td style="padding:5px 6px;border:1px solid #cbd5e1;text-align:center;">{esc(r['week_number'] or '')}</td>
            <td style="padding:5px 6px;border:1px solid #cbd5e1;text-align:center;">{esc(r['lesson_number'] or '')}</td>
            <td style="padding:5px 6px;border:1px solid #cbd5e1;">{_fmt('strand')}</td>
            <td style="padding:5px 6px;border:1px solid #cbd5e1;">{_fmt('sub_strand')}</td>
            <td style="padding:5px 6px;border:1px solid #cbd5e1;">{_fmt('learning_outcomes')}</td>
            <td style="padding:5px 6px;border:1px solid #cbd5e1;">{_fmt('learning_experiences')}</td>
            <td style="padding:5px 6px;border:1px solid #cbd5e1;">{_fmt('key_inquiry_questions')}</td>
            <td style="padding:5px 6px;border:1px solid #cbd5e1;">{_fmt('learning_resources')}</td>
            <td style="padding:5px 6px;border:1px solid #cbd5e1;">{_fmt('assessment_methods')}</td>
            <td style="padding:5px 6px;border:1px solid #cbd5e1;">{_fmt('reflection')}</td>
        </tr>
        """

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Elimu Hub | Scheme of Work — {esc(copy['subject_name'])}</title>
        <style>
            @page {{ size: A3 landscape; margin: 10mm; }}
            body {{ font-family: Arial, sans-serif; color: #1e293b; padding: 16px; font-size: 10px; }}
            @media print {{ .no-print {{ display: none !important; }} body {{ padding: 0; }} }}
            table {{ width: 100%; border-collapse: collapse; margin-top: 10px; }}
            th {{ background: #f1f5f9; border: 1px solid #cbd5e1; padding: 6px; font-size: 9px; text-transform: uppercase; }}
        </style>
    </head>
    <body>
        <div class="no-print" style="text-align:right; margin-bottom:12px;">
            <button onclick="window.print()" style="background:#4f46e5;color:white;border:none;padding:10px 18px;border-radius:8px;font-weight:bold;cursor:pointer;">🖨 Print / Save as PDF</button>
        </div>
        <div style="display:flex;align-items:center;gap:14px;border-bottom:3px double #4f46e5;padding-bottom:10px;">
            {logo_html}
            <div>
                <h1 style="margin:0;font-size:16px;">{esc(school['name']) if school else ''}</h1>
                <p style="margin:2px 0 0;font-size:12px;font-weight:bold;">SCHEME OF WORK</p>
            </div>
        </div>
        <table style="margin-top:10px;border:none;">
            <tr>
                <td style="border:none;padding:2px 0;"><b>Teacher:</b> {esc(teacher_display_name)}</td>
                <td style="border:none;padding:2px 0;"><b>TSC No.:</b> {esc(tsc_display)}</td>
                <td style="border:none;padding:2px 0;"><b>Subject:</b> {esc(copy['subject_name'])}</td>
            </tr>
            <tr>
                <td style="border:none;padding:2px 0;"><b>Grade:</b> {esc(copy['grade_name'])} {esc(copy['stream']) if copy['stream'] != 'ALL' else ''}</td>
                <td style="border:none;padding:2px 0;"><b>Term:</b> {esc(copy['term'])}</td>
                <td style="border:none;padding:2px 0;"><b>Year:</b> {copy['year']}</td>
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
        <p style="margin-top:16px;font-size:9px;color:#94a3b8;text-align:center;">Generated by Elimu Hub — {esc(school['name']) if school else ''}</p>
    </body>
    </html>
    """
