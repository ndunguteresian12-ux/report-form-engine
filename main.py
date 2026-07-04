import os
import re
import html
import uuid
import logging
import urllib.parse
from contextlib import contextmanager

from fastapi import FastAPI, Form, File, UploadFile, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from passlib.context import CryptContext
import psycopg2
psycopg2.errors
from psycopg.rows import dict_row
from dotenv import load_dotenv

load_dotenv()

# --- Logging ---------------------------------------------------------------
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("cbe_engine")

app = FastAPI(title="Kenyan CBE Multi-Tenant Enterprise Engine")

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# --- SAFELY ADD SUPABASE CLIENT CONDITIONAL IMPORT ------------------------
try:
    from supabase import create_client
except ImportError:
    create_client = None

# --- INITIALIZE DATABASE CONNECTION -----------------------------------------
import os
import logging
from sqlalchemy import create_engine

logger = logging.getLogger("cbe_engine")

# Get the URL directly
DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    logger.error("CRITICAL: DATABASE_URL is missing!")
    raise ValueError("DATABASE_URL must be set in Render environment variables.")

# Clean the URL to ensure it's compatible with SQLAlchemy
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg2://", 1)

# Create the engine
engine = create_engine(DATABASE_URL)
logger.info("Engine configured successfully.")
@contextmanager
def get_db_connection(row_factory=None):
    """
    Central place to obtain a database connection. Ensures failures surface as
    a clean HTTP 503 instead of an unhandled crash, and always closes the
    connection when done.
    """
    conn = None
    try:
        conn = psycopg.connect(DB_CONNECTION_STRING, row_factory=row_factory)
        try:
            yield conn
        finally:
            conn.close()
    except psycopg.OperationalError as db_conn_err:
        logger.error(f"Database connection failure: {db_conn_err}")
        raise HTTPException(status_code=503, detail="Database is temporarily unavailable. Please try again shortly.")


def esc(value) -> str:
    """Escape a value for safe interpolation into HTML templates."""
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


UPLOAD_DIR = "static/logos"
ALLOWED_LOGO_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
MAX_LOGO_SIZE_BYTES = 5 * 1024 * 1024  # 5MB
os.makedirs(UPLOAD_DIR, exist_ok=True)
if os.path.exists("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")

# --- Global error handlers ---------------------------------------------------
@app.exception_handler(psycopg2.errors.UniqueViolation)
async def handle_unique_violation(request: Request, exc: psycopg.errors.UniqueViolation):
    logger.warning(f"Unique constraint violation on {request.url.path}: {exc}")
    return PlainTextResponse(
        "That record already exists (duplicate email, admission number, or similar unique field).",
        status_code=409,
    )


@app.exception_handler(psycopg2.errors.ForeignKeyViolation)
async def handle_fk_violation(request: Request, exc: psycopg.errors.ForeignKeyViolation):
    logger.warning(f"Foreign key violation on {request.url.path}: {exc}")
    return PlainTextResponse(
        "That request references a record that doesn't exist (invalid class, student, or school reference).",
        status_code=400,
    )


@app.exception_handler(psycopg.Error)
async def handle_db_error(request: Request, exc: psycopg.Error):
    logger.error(f"Unhandled database error on {request.url.path}: {exc}")
    return PlainTextResponse(
        "A database error occurred while processing your request. Please try again.",
        status_code=500,
    )


@app.exception_handler(Exception)
async def handle_unexpected_error(request: Request, exc: Exception):
    logger.exception(f"Unhandled exception on {request.url.path}")
    return PlainTextResponse(
        "An unexpected error occurred. Please try again, and contact support if it persists.",
        status_code=500,
    )


# --- Automated Database Schema Architecture Optimization ---
def bootstrap_database_schema():
    """Initializes tables ensuring strict data schema compliance and indices."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS schools (
                    id SERIAL PRIMARY KEY,
                    name VARCHAR(255) NOT NULL,
                    sub_county VARCHAR(255) NOT NULL,
                    physical_address VARCHAR(255) NOT NULL,
                    logo_url VARCHAR(512), -- Expanded to support cloud URL length strings cleanly
                    wallet_balance NUMERIC(12, 2) DEFAULT 0.00,
                    theme_color VARCHAR(50) DEFAULT 'emerald'
                );

                CREATE TABLE IF NOT EXISTS school_settings (
                    school_id INTEGER PRIMARY KEY REFERENCES schools(id) ON DELETE CASCADE,
                    active_year INTEGER DEFAULT 2026,
                    active_term VARCHAR(20) DEFAULT 'Term 1',
                    active_cycle VARCHAR(20) DEFAULT 'End Term',
                    opening_date VARCHAR(50) DEFAULT 'To Be Announced',
                    closing_date VARCHAR(50) DEFAULT 'To Be Announced',
                    is_single_stream BOOLEAN DEFAULT FALSE
                );

                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    email VARCHAR(255) UNIQUE NOT NULL,
                    password_hash VARCHAR(255) NOT NULL,
                    role VARCHAR(50) NOT NULL, -- 'admin' or 'staff'
                    school_id INTEGER REFERENCES schools(id) ON DELETE CASCADE,
                    is_verified BOOLEAN DEFAULT TRUE
                );

                CREATE TABLE IF NOT EXISTS classes (
                    id SERIAL PRIMARY KEY,
                    grade_name VARCHAR(100) NOT NULL,         -- e.g., 'Grade 7', 'Grade 8'
                    education_level VARCHAR(100) NOT NULL    -- e.g., 'Junior School'
                );

                CREATE TABLE IF NOT EXISTS students (
                    id SERIAL PRIMARY KEY,
                    school_id INTEGER REFERENCES schools(id) ON DELETE CASCADE,
                    class_id INTEGER REFERENCES classes(id) ON DELETE CASCADE, -- Linked class reference
                    admission_number VARCHAR(100) NOT NULL,
                    first_name VARCHAR(100) NOT NULL,
                    last_name VARCHAR(100) NOT NULL,
                    stream VARCHAR(50) NOT NULL,
                    education_level VARCHAR(100) NOT NULL, -- 'Junior School', 'Upper Primary', 'Lower Primary'
                    status VARCHAR(50) DEFAULT 'ACTIVE',    -- Supports checking status != 'GRADUATED'
                    knec_lan VARCHAR(100) DEFAULT 'N/A',
                    UNIQUE(school_id, admission_number)
                );

                CREATE TABLE IF NOT EXISTS learning_areas (
                    id SERIAL PRIMARY KEY,
                    education_level VARCHAR(100) NOT NULL,
                    name VARCHAR(255) NOT NULL,
                    UNIQUE(education_level, name)
                );

                CREATE TABLE IF NOT EXISTS student_scores (
                    id SERIAL PRIMARY KEY,
                    student_id INTEGER REFERENCES students(id) ON DELETE CASCADE,
                    learning_area_id INTEGER REFERENCES learning_areas(id) ON DELETE CASCADE,
                    cycle_name VARCHAR(50) NOT NULL, -- 'Opener', 'Midterm', 'End Term'
                    raw_score NUMERIC(5, 2) NOT NULL,
                    entered_by_user_id INTEGER REFERENCES users(id),
                    UNIQUE(student_id, learning_area_id, cycle_name)
                );
            """)
            classes_payload = [
                (1, 'Grade 1', 'Lower Primary'), (2, 'Grade 2', 'Lower Primary'), (3, 'Grade 3', 'Lower Primary'),
                (4, 'Grade 4', 'Upper Primary'), (5, 'Grade 5', 'Upper Primary'), (6, 'Grade 6', 'Upper Primary'),
                (7, 'Grade 7', 'Junior School'), (8, 'Grade 8', 'Junior School'), (9, 'Grade 9', 'Junior School'),
            ]
            for class_id, grade_name, education_level in classes_payload:
                cur.execute("""
                    INSERT INTO classes (id, grade_name, education_level)
                    VALUES (%s, %s, %s) ON CONFLICT (id) DO NOTHING;
                """, (class_id, grade_name, education_level))
            cur.execute("""
                SELECT setval(pg_get_serial_sequence('classes', 'id'), COALESCE((SELECT MAX(id) FROM classes), 1));
            """)

            subjects_payload = [
                ('Junior School', 'Mathematics'), ('Junior School', 'English'), ('Junior School', 'Kiswahili'),
                ('Junior School', 'Creative arts and sports.'), ('Junior School', 'Integrated science.'),
                ('Junior School', 'Agriculture'), ('Junior School', 'Social studies'),
                ('Junior School', 'Christian religious education'), ('Junior School', 'pretechnical studies.'),
                ('Upper Primary', 'Mathematics'), ('Upper Primary', 'English'), ('Upper Primary', 'Kiswahili'),
                ('Upper Primary', 'Creative arts and sports.'), ('Upper Primary', 'Integrated science.'),
                ('Upper Primary', 'Agriculture'), ('Upper Primary', 'Social studies'),
                ('Upper Primary', 'Christian religious education'),
                ('Lower Primary', 'Mathematics'), ('Lower Primary', 'English lugha'),
                ('Lower Primary', 'Environment studies'), ('Lower Primary', 'Science'),
                ('Lower Primary', 'Creative activities'), ('Lower Primary', 'Social studies')
            ]
            for lvl, name in subjects_payload:
                cur.execute("""
                    INSERT INTO learning_areas (education_level, name) 
                    VALUES (%s, %s) ON CONFLICT (education_level, name) DO NOTHING;
                """, (lvl, name))
            conn.commit()

try:
    bootstrap_database_schema()
except Exception as schema_err:
    print(f"[Warning] Database schema bootstrap omitted or deferred: {schema_err}")

# --- Core Business & CBE Analytics Helper Logic ---
def evaluate_performance_metrics(score: float) -> dict:
    try:
        val = float(score)
    except (TypeError, ValueError):
        return {"pld": "N/A", "points": 0, "desc": "No Evaluation"}

    if 0 <= val <= 19:
        return {"pld": "BE2", "points": 1, "desc": "Below Expectations"}
    elif 20 <= val <= 29:
        return {"pld": "B1", "points": 2, "desc": "Below Expectations"}
    elif 30 <= val <= 39:
        return {"pld": "AE2", "points": 3, "desc": "Approaching Expectations"}
    elif 40 <= val <= 49:
        return {"pld": "AE1", "points": 4, "desc": "Approaching Expectations"}
    elif 50 <= val <= 59:
        return {"pld": "ME2", "points": 5, "desc": "Meeting Expectations"}
    elif 60 <= val <= 75:
        return {"pld": "ME1", "points": 6, "desc": "Meeting Expectations"}
    elif 76 <= val <= 89:
        return {"pld": "EE2", "points": 7, "desc": "Exceeding Expectations"}
    elif 90 <= val <= 100:
        return {"pld": "EE1", "points": 8, "desc": "Exceeding Expectations"}
    return {"pld": "N/A", "points": 0, "desc": "Out of Range"}

def fetch_theme_styles(color_name: str):
    themes = {
        'emerald': {'bg': 'bg-emerald-800', 'hover': 'hover:bg-emerald-900', 'text': 'text-emerald-700', 'border': 'border-emerald-600', 'hex': '#046A38'},
        'blue': {'bg': 'bg-blue-800', 'hover': 'hover:bg-blue-900', 'text': 'text-blue-700', 'border': 'border-blue-600', 'hex': '#1e40af'},
        'indigo': {'bg': 'bg-indigo-800', 'hover': 'hover:bg-indigo-900', 'text': 'text-indigo-700', 'border': 'border-indigo-600', 'hex': '#3730a3'},
        'purple': {'bg': 'bg-purple-800', 'hover': 'hover:bg-purple-900', 'text': 'text-purple-700', 'border': 'border-purple-600', 'hex': '#6b21a8'}
    }
    return themes.get(color_name, themes['emerald'])

# --- Authentication & Entry Routes ---
@app.get("/", response_class=HTMLResponse)
def landing_root():
    return RedirectResponse(url="/login")

@app.get("/login", response_class=HTMLResponse)
def login_portal():
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Multi-Tenant Hub Gateway</title>
        <script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script>
    </head>
    <body class="bg-slate-900 flex items-center justify-center h-screen font-sans">
        <div class="bg-white p-8 rounded-2xl shadow-2xl w-full max-w-md border-t-8 border-emerald-700">
            <h2 class="text-2xl font-black text-center text-slate-800 mb-2">CBE Reporting Hub</h2>
            <p class="text-xs text-center text-slate-400 mb-6">Enterprise Institutional Gateway Node</p>
            
            <form action="/api/v1/auth/login" method="post" class="space-y-4">
                <div>
                    <label class="block text-xs font-bold uppercase text-slate-600 tracking-wider">Access Email</label>
                    <input type="email" name="username" class="w-full p-3 border rounded-lg mt-1 focus:ring-2 focus:ring-emerald-600 outline-none" required>
                </div>
                <div>
                    <label class="block text-xs font-bold uppercase text-slate-600 tracking-wider">Security Passphrase</label>
                    <input type="password" name="password" class="w-full p-3 border rounded-lg mt-1 focus:ring-2 focus:ring-emerald-600 outline-none" required>
                </div>
                <button type="submit" class="w-full bg-emerald-700 text-white p-3.5 rounded-lg font-black tracking-wide hover:bg-emerald-800 transition shadow-lg">Authenticate Instance</button>
            </form>
            
            <div class="mt-6 border-t pt-4 text-center">
                <p class="text-xs text-slate-500">
                    New Institution? 
                    <a href="/register" class="text-emerald-700 font-bold hover:underline ml-1">Register Self-Service Account Node</a>
                </p>
            </div>
        </div>
    </body>
    </html>
    """

@app.post("/api/v1/auth/login")
def process_login(username: str = Form(...), password: str = Form(...)):
    safe_password = password[:72]
    
    with get_db_connection(row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE email = %s;", (username,))
            user = cur.fetchone()
            
            if user:
                is_valid = False
                try:
                    is_valid = pwd_context.verify(safe_password, user['password_hash'])
                except Exception:
                    is_valid = False
                
                if not is_valid and user['password_hash'] == password:
                    hashed_password = pwd_context.hash(safe_password)
                    cur.execute("""
                        UPDATE users 
                        SET password_hash = %s 
                        WHERE id = %s;
                    """, (hashed_password, user['id']))
                    conn.commit()
                    is_valid = True
                
                if is_valid:
                    if user['role'] == 'admin':
                        response = RedirectResponse(url=f"/admin/dashboard/{user['school_id']}", status_code=303)
                    else:
                        if not user['is_verified']:
                            raise HTTPException(status_code=403, detail="Access Denied: Staff verification pending admin approval.")
                        response = RedirectResponse(url=f"/staff/dashboard/{user['school_id']}?user_id={user['id']}", status_code=303)
                    
                    response.set_cookie(
                        key="session_school_id",
                        value=str(user['school_id']),
                        httponly=True,
                        samesite="lax",
                        secure=os.getenv("COOKIE_SECURE", "false").lower() == "true",
                        max_age=60 * 60 * 24 * 7,  # 7 days
                    )
                    return response
                    
    raise HTTPException(status_code=401, detail="Invalid credential combination provided.")


@app.get("/register", response_class=HTMLResponse)
def public_registration_portal():
    return """
    <!DOCTYPE html>
    <html>
    <head><title>Create School Tenant Account</title><script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script></head>
    <body class="bg-slate-900 flex items-center justify-center min-h-screen font-sans p-6">
        <div class="bg-white p-8 rounded-2xl shadow-2xl w-full max-w-xl border-t-8 border-emerald-700">
            <h2 class="text-2xl font-black text-slate-800">Register Institutional Tenant</h2>
            <p class="text-xs text-slate-400 mb-6">Setup your completely isolated enterprise report engine node instance.</p>
            
            <form action="/api/v1/tenant/register" method="post" enctype="multipart/form-data" class="space-y-4 text-xs">
                <div class="bg-slate-50 p-4 rounded-xl border space-y-3">
                    <h3 class="font-black text-slate-700 uppercase tracking-wide">🏫 School Profile Information</h3>
                    <div>
                        <label class="block font-bold text-slate-600">Official School Name</label>
                        <input type="text" name="school_name" placeholder="e.g. Kilimani Academy" class="w-full p-2.5 border rounded-lg mt-1 bg-white" required>
                    </div>
                    <div class="grid grid-cols-2 gap-3">
                        <div>
                            <label class="block font-bold text-slate-600">Sub-County Jurisdiction</label>
                            <input type="text" name="sub_county" placeholder="e.g. Dagoretti" class="w-full p-2.5 border rounded-lg mt-1 bg-white" required>
                        </div>
                        <div>
                            <label class="block font-bold text-slate-600">Physical Location Address</label>
                            <input type="text" name="physical_address" placeholder="e.g. Yaya Centre, Nairobi" class="w-full p-2.5 border rounded-lg mt-1 bg-white" required>
                        </div>
                    </div>
                    <div>
                        <label class="block font-bold text-slate-600">Official School Logo Image File</label>
                        <input type="file" name="logo_file" class="w-full p-2 border rounded-lg mt-1 bg-white" accept="image/*">
                    </div>
                </div>

                <div class="bg-slate-50 p-4 rounded-xl border space-y-3">
                    <h3 class="font-black text-slate-700 uppercase tracking-wide">🔒 Super-Admin Account Security Credentials</h3>
                    <div>
                        <label class="block font-bold text-slate-600">Primary Administrator Username (Email Address)</label>
                        <input type="email" name="admin_email" placeholder="admin@school.ac.ke" class="w-full p-2.5 border rounded-lg mt-1 bg-white" required>
                    </div>
                    <div>
                        <label class="block font-bold text-slate-600">Secure Access Passphrase Password</label>
                        <input type="password" name="admin_password" class="w-full p-2.5 border rounded-lg mt-1 bg-white" required>
                    </div>
                </div>

                <div class="flex items-center justify-between pt-2">
                    <a href="/login" class="text-slate-500 font-bold hover:underline">Already have an institution? Log in</a>
                    <button type="submit" class="bg-emerald-700 text-white px-6 py-3 rounded-lg font-black tracking-wide hover:bg-emerald-800 transition shadow-md">Create Account & Boot Engine</button>
                </div>
            </form>
        </div>
    </body>
    </html>
    """

@app.post("/api/v1/tenant/register")
async def register_new_tenant_pipeline(
    school_name: str = Form(...),
    sub_county: str = Form(...),
    physical_address: str = Form(...),
    admin_email: str = Form(...),
    admin_password: str = Form(...),
    logo_file: UploadFile = File(None)
):
    school_name = school_name.strip()
    sub_county = sub_county.strip()
    physical_address = physical_address.strip()
    admin_email = admin_email.strip().lower()

    if not school_name or not sub_county or not physical_address:
        raise HTTPException(status_code=400, detail="School name, sub-county, and address are all required.")
    if len(admin_password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters long.")

    logo_resolved_url = None
    if logo_file and logo_file.filename:
        file_extension = os.path.splitext(logo_file.filename)[1].lower()
        if file_extension not in ALLOWED_LOGO_EXTENSIONS:
            raise HTTPException(status_code=400, detail="Unsupported logo file type. Use PNG, JPG, GIF, or WEBP.")

        contents = await logo_file.read()
        if len(contents) > MAX_LOGO_SIZE_BYTES:
            raise HTTPException(status_code=400, detail="Logo file is too large (5MB max).")

        # Create a URL-safe structured unique filename node
        safe_filename = f"logo_{uuid.uuid4().hex}{file_extension}"

        # 1. Primary Cloud Architecture Path: Process via Supabase Client Gate
        if supabase_client:
            try:
                # Expects a storage bucket explicitly configured to PUBLIC access named 'logos'
                supabase_client.storage.from_("logos").upload(
                    path=safe_filename,
                    file=contents,
                    file_options={"content-type": logo_file.content_type}
                )
                # Capture absolute public URL reference network asset string
                logo_resolved_url = supabase_client.storage.from_("logos").get_public_url(safe_filename)
            except Exception as storage_err:
                logger.error(f"Supabase Cloud upload failed, reverting locally: {storage_err}")

        # 2. Fallback Pipeline Path: Write asset block to local server disk
        if not logo_resolved_url:
            local_path = f"{UPLOAD_DIR}/{safe_filename}"
            try:
                with open(local_path, "wb") as f:
                    f.write(contents)
                logo_resolved_url = f"/{local_path}"
            except OSError as io_err:
                logger.error(f"Failed to save uploaded logo locally: {io_err}")
                raise HTTPException(status_code=500, detail="Could not save the uploaded logo. Please try again.")

    safe_password = admin_password[:72]
    hashed_password = pwd_context.hash(safe_password)

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE email = %s;", (admin_email,))
            if cur.fetchone():
                raise HTTPException(status_code=400, detail="Registration Refused: Email already allocated.")
            
            cur.execute("""
                INSERT INTO schools (name, sub_county, physical_address, logo_url, wallet_balance, theme_color)
                VALUES (%s, %s, %s, %s, 0.00, 'emerald') RETURNING id;
            """, (school_name, sub_county, physical_address, logo_resolved_url))
            new_school_id = cur.fetchone()[0]

            cur.execute("""
                INSERT INTO school_settings (school_id, active_year, active_term, active_cycle, closing_date, opening_date)
                VALUES (%s, 2026, 'Term 1', 'End Term', '2026-04-10', '2026-05-04');
            """, (new_school_id,))

            cur.execute("""
                INSERT INTO users (email, password_hash, role, school_id, is_verified)
                VALUES (%s, %s, 'admin', %s, TRUE);
            """, (admin_email, hashed_password, new_school_id))

            conn.commit()

    return HTMLResponse("""
    <script>
        alert('Institutional Registration Complete! Dynamic Tenant Configuration Created Successfully.');
        window.location.href='/login';
    </script>
    """)


@app.get("/admin/dashboard/{school_id}", response_class=HTMLResponse)
def administrative_dashboard(school_id: int, request: Request):  
    session_school_id = request.cookies.get("session_school_id")
    
    if not session_school_id:
        return RedirectResponse(url="/login?error=Authentication+required.", status_code=303)
        
    if str(session_school_id) != str(school_id):
        raise HTTPException(
            status_code=403, 
            detail="Access Denied: You do not have administrative privileges for this institution."
        )

    with get_db_connection(row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM schools WHERE id = %s;", (school_id,))
            school = cur.fetchone()
            
            cur.execute("SELECT * FROM school_settings WHERE school_id = %s;", (school_id,))
            settings = cur.fetchone()
            
            cur.execute("""
                SELECT s.id, s.admission_number, s.first_name, s.last_name, s.stream, 
                       c.grade_name, c.education_level
                FROM students s
                JOIN classes c ON s.class_id = c.id
                WHERE s.school_id = %s AND (s.status IS NULL OR s.status != 'GRADUATED')
                ORDER BY c.id ASC, s.stream ASC, s.admission_number ASC;
            """, (school_id,))
            students = cur.fetchall()
            
            cur.execute("SELECT id, email, is_verified FROM users WHERE school_id = %s AND role='staff';", (school_id,))
            staff_members = cur.fetchall()
            
            cur.execute("""
                SELECT DISTINCT c.id, c.grade_name, s.stream, c.education_level
                FROM students s
                JOIN classes c ON s.class_id = c.id
                WHERE s.school_id = %s AND (s.status IS NULL OR s.status != 'GRADUATED')
                ORDER BY c.id ASC, s.stream ASC;
            """, (school_id,))
            classes = cur.fetchall()

    if not school:
        raise HTTPException(status_code=404, detail="Institution Tenant Context Missed.")
    
    st = settings or {
        'active_term': 'Term 1', 
        'active_cycle': 'End Term', 
        'opening_date': '', 
        'closing_date': '', 
        'is_single_stream': False
    }
    is_single_stream = st.get('is_single_stream', False)

    # Dynamic Student Rows Generation
    student_rows = ""
    for s in students:
        display_stream = "Single Stream" if (is_single_stream or not s['stream'] or s['stream'].upper() == "SINGLE STREAM") else s['stream']
        stream_td = "" if is_single_stream else f"<td class='p-4 text-slate-600 font-medium'>{display_stream}</td>"
        
        student_rows += f"""
            <tr class='border-b border-slate-100 hover:bg-slate-50/80 transition-colors text-sm text-slate-700'>
                <td class='p-4 font-mono font-medium text-slate-500'>{esc(s['admission_number'])}</td>
                <td class='p-4 font-semibold text-slate-900'>{esc(s['first_name'])} {esc(s['last_name'])}</td>
                <td class='p-4'>
                    <span class="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-semibold bg-slate-100 text-slate-800">{s['grade_name']}</span>
                    <span class="text-xs text-slate-400 ml-1">({s['education_level']})</span>
                </td>
                {stream_td}
                <td class='p-4 text-right'>
                    <a href='/admin/scores/manage/{school_id}?student_id={s['id']}' class='inline-flex items-center font-bold text-xs text-blue-600 hover:text-blue-800 transition'>Modify Scores →</a>
                </td>
            </tr>
        """

    # Dynamic Class Cards Generation
    class_blocks = []
    for c in classes:
        is_stream_blank = not c['stream'] or c['stream'].strip() == "" or c['stream'].upper() == "SINGLE STREAM"
        
        if is_single_stream or is_stream_blank:
            display_title = c['grade_name']
            stream_param = "SINGLE STREAM"
        else:
            display_title = f"{c['grade_name']} — Stream: {esc(c['stream'])}"
            stream_param = c['stream']
        
        encoded_grade = urllib.parse.quote(c['grade_name'])
        encoded_stream = urllib.parse.quote(stream_param)
        encoded_level = urllib.parse.quote(c['education_level'])
        
        class_blocks.append(f"""
            <div class='bg-white border border-slate-200/80 p-5 rounded-2xl shadow-xs hover:shadow-md transition-all flex flex-col justify-between group'>
                <div>
                    <span class='text-[10px] bg-slate-100 text-slate-600 px-2.5 py-1 rounded-md font-bold uppercase tracking-wider'>{c['education_level']}</span>
                    <h3 class='text-base font-black text-slate-800 mt-2.5 group-hover:text-slate-900'>{display_title}</h3>
                </div>
                <div class='grid grid-cols-2 gap-2 mt-5'>
                    <a href='/staff/bulk-entry/{school_id}?grade_name={encoded_grade}&stream={encoded_stream}&education_level={encoded_level}' class='bg-blue-950 hover:bg-blue-900 text-white text-center text-xs py-2 rounded-xl font-semibold transition shadow-xs'>Bulk Entry</a>
                    <a href='/api/v1/reports/bulk-print/{school_id}?grade_name={encoded_grade}&stream={encoded_stream}&education_level={encoded_level}' target='_blank' class='bg-emerald-600 text-white text-center text-xs py-2 rounded-xl font-semibold hover:bg-emerald-700 transition shadow-xs'>Bulk Print</a>
                </div>
            </div>
        """)
    class_blocks_html = "".join(class_blocks)
    stream_header_th = "" if is_single_stream else "<th class='p-4 text-slate-500 font-semibold'>Stream</th>"

    # Robust Logo configuration injection
    logo_html = ""
    logo_src = school.get('logo_url')
    if logo_src:
        final_src = logo_src if logo_src.startswith("http") else f"/{logo_src.lstrip('/')}"
        logo_html = f"""
        <div class='w-11 h-11 rounded-xl bg-slate-50 border border-slate-200 flex items-center justify-center p-1.5 shadow-2xs'>
            <img src='{final_src}' class='max-w-full max-h-full object-contain' />
        </div>
        """

    return HTMLResponse(f"""
    <!DOCTYPE html>
    <html class="h-full">
    <head>
        <title>Control Deck - {esc(school['name'])}</title>
        <script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script>
        <link rel="preconnect" href="https://fonts.googleapis.com">
        <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
        <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap" rel="stylesheet">
        <style>body {{ font-family: 'Plus Jakarta Sans', sans-serif; }}</style>
    </head>
    <body class="bg-[#F8FAFC] text-slate-800 antialiased min-h-full flex flex-col relative">
        
        <header class="bg-white border-b border-slate-200/80 px-8 py-4 flex justify-between items-center sticky top-0 z-40 backdrop-blur-md bg-white/90 shadow-2xs">
            <div class="flex items-center space-x-4">
                {logo_html}
                <div>
                    <h1 class="text-base font-bold text-slate-900 tracking-tight">{esc(school['name'])}</h1>
                    <p class="text-xs text-slate-500">{esc(school['physical_address'])} • {esc(school['sub_county'])} Sub-County</p>
                </div>
            </div>
            <div class="flex items-center space-x-3 text-xs font-semibold mr-14">
                <span class="bg-slate-50 text-slate-700 px-3 py-2 rounded-xl border border-slate-200/60 shadow-2xs">System Wallet: <span class="text-slate-900 font-bold">KSh {float(school['wallet_balance']):,.2f}</span></span>
                <span class="bg-blue-950 text-white px-3 py-2 rounded-xl shadow-xs">{st['active_term']} • {st['active_cycle']}</span>
            </div>
        </header>

        <div class="fixed top-4 right-4 z-50">
            <button onclick="document.getElementById('settingsModal').classList.remove('hidden')" class="bg-white hover:bg-slate-100 text-slate-700 p-2.5 rounded-full border border-slate-200 shadow-md transition duration-200 cursor-pointer flex items-center justify-center">
                <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor" class="w-5 h-5 animate-[spin_12s_linear_infinite]">
                    <path stroke-linecap="round" stroke-linejoin="round" d="M9.594 3.94c.09-.542.56-.94 1.11-.94h2.593c.55 0 1.02.398 1.11.94l.213 1.281c.063.374.313.686.645.87.074.04.147.083.22.127.324.196.72.257 1.075.124l1.217-.456a1.125 1.125 0 0 1 1.37.49l1.296 2.247a1.125 1.125 0 0 1-.26 1.43l-1.003.767a1.123 1.123 0 0 0-.417 1.03c.004.074.006.148.006.222 0 .074-.002.148-.006.222a1.123 1.123 0 0 0 .417 1.03l1.003.767a1.125 1.125 0 0 1 .26 1.43l-1.296 2.247a1.125 1.125 0 0 1-1.37.49l-1.216-.456a1.125 1.125 0 0 0-1.076.124a6.57 6.57 0 0 1-.22.128c-.331.183-.581.495-.644.869l-.213 1.28c-.09.543-.56.94-1.11.94h-2.594c-.55 0-1.02-.398-1.11-.94l-.213-1.281a1.125 1.125 0 0 0-.646-.87a6.512 6.512 0 0 1-.22-.127c-.331-.182-.581-.495-.644-.869l-.213-1.281a1.125 1.125 0 0 1 .26-1.43l1.003-.767a1.12 1.12 0 0 0 .417-1.03a6.445 6.445 0 0 1-.006-.222c0-.074.002-.148.006-.222a1.12 1.12 0 0 0-.417-1.03l-1.003-.767a1.125 1.125 0 0 1-.26-1.43l1.296-2.247a1.125 1.125 0 0 1 1.37-.49l1.216.456c.356.133.751.072 1.076-.124.072-.044.146-.087.22-.128c.332-.183.582-.495.644-.869l.214-1.28Z" />
                    <path stroke-linecap="round" stroke-linejoin="round" d="M15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0Z" />
                </svg>
            </button>
        </div>

        <div class="p-8 max-w-7xl mx-auto w-full grid grid-cols-1 lg:grid-cols-3 gap-8 flex-1">
            <div class="lg:col-span-2 space-y-8">
                <div>
                    <div class="flex items-center justify-between mb-4">
                        <h2 class="text-xs font-bold uppercase tracking-wider text-slate-400">🏫 Classroom Cohorts Grouping</h2>
                    </div>
                    <div class="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 gap-4">
                        {class_blocks_html or "<p class='text-slate-400 text-xs italic col-span-full text-center py-8 bg-white border border-dashed rounded-2xl'>No registered student profiles logged inside streams.</p>"}
                    </div>
                </div>

                <div class="bg-white rounded-2xl border border-slate-200/80 shadow-xs overflow-hidden">
                    <div class="p-5 border-b border-slate-100 flex justify-between items-center bg-slate-50/40">
                        <div>
                            <h2 class="text-base font-bold text-slate-900">🗂️ Master School Ledger Roster</h2>
                            <p class="text-xs text-slate-400 mt-0.5">Active database context monitoring active operational tiers</p>
                        </div>
                        <a href="/admin/student/new/{school_id}" class="bg-blue-950 hover:bg-blue-900 text-white text-xs px-3.5 py-2 rounded-xl font-semibold transition shadow-xs">+ Register New Student</a>
                    </div>
                    <div class="overflow-x-auto">
                        <table class="w-full text-left border-collapse">
                            <thead>
                                <tr class="bg-slate-50 text-slate-500 text-xs font-semibold border-b border-slate-100">
                                    <th class="p-4">Adm No.</th>
                                    <th class="p-4">Full Student Name</th>
                                    <th class="p-4">Education Segment</th>
                                    {stream_header_th}
                                    <th class="p-4 text-right">Operations</th>
                                </tr>
                            </tbody>
                            <tbody>
                                {student_rows or "<tr><td colspan='5' class='text-center p-8 text-slate-400 text-sm italic'>No active institutional records logged.</td></tr>"}
                            </tbody>
                        </table>
                    </div>
                </div>
            </div>

            <div class="space-y-6">
                <div class="bg-white p-6 rounded-2xl border border-slate-200/80 shadow-xs space-y-4">
                    <h2 class="text-sm font-bold uppercase tracking-wider text-slate-400 border-b border-slate-100 pb-3">⚙️ Core Parameters</h2>
                    <form action="/api/v1/settings/update/{school_id}" method="post" class="space-y-4">
                        <div class="grid grid-cols-2 gap-3">
                            <div>
                                <label class="text-[11px] font-semibold text-slate-500 block mb-1">Academic Term</label>
                                <select name="active_term" class="w-full border border-slate-200 bg-white p-2 rounded-xl text-xs font-semibold text-slate-800 outline-none focus:border-slate-400">
                                    <option value="Term 1" {"selected" if st['active_term'] == 'Term 1' else ""}>Term 1</option>
                                    <option value="Term 2" {"selected" if st['active_term'] == 'Term 2' else ""}>Term 2</option>
                                    <option value="Term 3" {"selected" if st['active_term'] == 'Term 3' else ""}>Term 3</option>
                                </select>
                            </div>
                            <div>
                                <label class="text-[11px] font-semibold text-slate-500 block mb-1">Assessment Phase</label>
                                <select name="active_cycle" class="w-full border border-slate-200 bg-white p-2 rounded-xl text-xs font-semibold text-slate-800 outline-none focus:border-slate-400">
                                    <option value="Opener" {"selected" if st['active_cycle'] == 'Opener' else ""}>Opener Exam</option>
                                    <option value="Midterm" {"selected" if st['active_cycle'] == 'Midterm' else ""}>Midterm Exam</option>
                                    <option value="End Term" {"selected" if st['active_cycle'] == 'End Term' else ""}>End Term Synthesis</option>
                                </select>
                            </div>
                        </div>
                        
                        <div class="bg-slate-50 p-3 rounded-xl border border-slate-100 flex items-center justify-between">
                            <div>
                                <label class="text-xs font-bold text-slate-800 block">Single Stream Mode</label>
                                <span class="text-[10px] text-slate-400 block">Hides class sorting columns</span>
                            </div>
                            <input type="checkbox" name="is_single_stream" value="true" {"checked" if is_single_stream else ""} class="w-4 h-4 text-emerald-600 border-slate-300 rounded focus:ring-emerald-500 cursor-pointer">
                        </div>

                        <div class="grid grid-cols-2 gap-2">
                            <div>
                                <label class="text-[11px] font-semibold text-slate-500 block mb-1">Opening Date</label>
                                <input type="date" name="opening_date" value="{esc(st['opening_date'])}" class="w-full border border-slate-200 p-2 rounded-xl text-xs outline-none focus:border-slate-400">
                            </div>
                            <div>
                                <label class="text-[11px] font-semibold text-slate-500 block mb-1">Closing Date</label>
                                <input type="date" name="closing_date" value="{esc(st['closing_date'])}" class="w-full border border-slate-200 p-2 rounded-xl text-xs outline-none focus:border-slate-400">
                            </div>
                        </div>
                        <input type="hidden" name="theme_color" value="emerald">
                        <button type="submit" class="w-full bg-blue-950 hover:bg-blue-900 text-white text-xs py-2.5 rounded-xl font-semibold transition shadow-xs cursor-pointer">Commit Engine Settings</button>
                    </form>

                    <div class="mt-2 pt-4 border-t border-slate-100">
                        <form action="/api/v1/school/promote-classes/{school_id}" method="post" 
                              onsubmit="return confirm('CRITICAL WARNING: Are you sure you want to promote all active student cohorts up 1 Grade Level? Grade 9 cohorts will safely move into Graduated Status.');">
                            <button type="submit" class="w-full bg-amber-50 border border-amber-200/80 text-amber-700 text-xs py-2.5 rounded-xl font-semibold hover:bg-amber-100/70 transition cursor-pointer">
                                🔄 Advance All Classes 1 Year
                            </button>
                        </form>
                    </div>
                </div>

                <div class="bg-white p-6 rounded-2xl border border-slate-200/80 shadow-xs space-y-4">
                    <h2 class="text-sm font-bold uppercase tracking-wider text-slate-400 border-b border-slate-100 pb-3">💳 Wallet Billing</h2>
                    <form action="/api/v1/wallet/stkpush/{school_id}" method="post" class="space-y-3">
                        <div>
                            <label class="text-[11px] font-semibold text-slate-500 block mb-1">Lipa na M-PESA Phone Number</label>
                            <input type="text" name="phone_number" placeholder="07XXXXXXXX" class="w-full border border-slate-200 p-2 rounded-xl text-xs outline-none focus:border-slate-400" required>
                        </div>
                        <div>
                            <label class="text-[11px] font-semibold text-slate-500 block mb-1">Topup Amount (KSh)</label>
                            <input type="number" name="amount" value="500" min="10" class="w-full border border-slate-200 p-2 rounded-xl text-xs outline-none focus:border-slate-400" required>
                        </div>
                        <button type="submit" class="w-full bg-emerald-600 text-white text-xs py-2.5 rounded-xl font-semibold hover:bg-emerald-700 transition shadow-xs cursor-pointer">🚀 Request STK Push</button>
                    </form>
                </div>
            </div>
        </div>

        <div id="settingsModal" class="hidden fixed inset-0 bg-slate-900/40 backdrop-blur-xs flex items-center justify-center z-50 p-4">
            <div class="bg-white rounded-2xl border shadow-xl max-w-md w-full overflow-hidden">
                <div class="p-5 border-b flex justify-between items-center bg-slate-50">
                    <h3 class="font-black text-slate-800 text-base">⚙️ System Node Configurations</h3>
                    <button onclick="document.getElementById('settingsModal').classList.add('hidden')" class="text-slate-400 hover:text-slate-600 font-bold text-sm cursor-pointer">✕</button>
                </div>
                
                <form action="/api/v1/settings/update/{school_id}" method="post" class="p-6 space-y-4 text-xs">
                    <div>
                        <label class="block font-bold text-slate-600 mb-1">Active Operations Term</label>
                        <select name="active_term" class="w-full border border-slate-200 p-2 rounded-lg font-semibold bg-white">
                            <option value="Term 1" {"selected" if st['active_term'] == 'Term 1' else ""}>Term 1</option>
                            <option value="Term 2" {"selected" if st['active_term'] == 'Term 2' else ""}>Term 2</option>
                            <option value="Term 3" {"selected" if st['active_term'] == 'Term 3' else ""}>Term 3</option>
                        </select>
                    </div>

                    <div>
                        <label class="block font-bold text-slate-600 mb-1">Active Evaluation Cycle</label>
                        <select name="active_cycle" class="w-full border border-slate-200 p-2 rounded-lg font-semibold bg-white">
                            <option value="Opener" {"selected" if st['active_cycle'] == 'Opener' else ""}>Opener Phase</option>
                            <option value="Midterm" {"selected" if st['active_cycle'] == 'Midterm' else ""}>Midterm Cycle</option>
                            <option value="End Term" {"selected" if st['active_cycle'] == 'End Term' else ""}>End Term Synthesis</option>
                        </select>
                    </div>

                    <div class="grid grid-cols-2 gap-3">
                        <div>
                            <label class="block font-bold text-slate-600 mb-1">Opening Date</label>
                            <input type="date" name="opening_date" value="{esc(st['opening_date'])}" required class="w-full border border-slate-200 p-2 rounded-lg font-semibold text-slate-700">
                        </div>
                        <div>
                            <label class="block font-bold text-slate-600 mb-1">Closing Date</label>
                            <input type="date" name="closing_date" value="{esc(st['closing_date'])}" required class="w-full border border-slate-200 p-2 rounded-lg font-semibold text-slate-700">
                        </div>
                    </div>

                    <div>
                        <label class="block font-bold text-slate-600 mb-1">Theme Branding Color</label>
                        <select name="theme_color" class="w-full border border-slate-200 p-2 rounded-lg font-semibold bg-white">
                            <option value="emerald">Emerald Dynamic Green</option>
                            <option value="indigo">Indigo Corporate Blue</option>
                            <option value="slate">Slate Minimalistic Gray</option>
                        </select>
                    </div>

                    <div class="pt-4 border-t flex justify-end space-x-2">
                        <button type="button" onclick="document.getElementById('settingsModal').classList.add('hidden')" class="bg-slate-100 hover:bg-slate-200 text-slate-700 font-bold py-2 px-4 rounded-xl cursor-pointer">Cancel</button>
                        <button type="submit" class="bg-emerald-600 hover:bg-emerald-700 text-white font-bold py-2 px-5 rounded-xl shadow-xs cursor-pointer">Save Settings</button>
                    </div>
                </form>
            </div>
        </div>
    </body>
    </html>
    """)
# --- GET View Routes for Administration Subsystems ---
@app.get("/admin/student/new/{school_id}", response_class=HTMLResponse)
def add_student_view(school_id: int):
    return f"""
    <!DOCTYPE html>
    <html>
    <head><title>Add New Student Record</title><script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script></head>
    <body class="bg-slate-100 flex items-center justify-center min-h-screen">
        <div class="bg-white p-8 rounded-2xl border shadow-md w-full max-w-lg">
            <h2 class="text-xl font-bold mb-4 text-slate-800">Add New Learner Profile</h2>
            <form action="/api/v1/students/add/{school_id}" method="post" class="space-y-4">
                <div class="grid grid-cols-2 gap-4">
                    <div><label class="text-xs font-bold text-slate-600">First Name</label><input type="text" name="first_name" class="w-full border p-2 rounded mt-1" required></div>
                    <div><label class="text-xs font-bold text-slate-600">Last Name</label><input type="text" name="last_name" class="w-full border p-2 rounded mt-1" required></div>
                </div>
                <div><label class="text-xs font-bold text-slate-600">Admission Number</label><input type="text" name="admission_number" class="w-full border p-2 rounded mt-1" required></div>
                <div class="grid grid-cols-2 gap-4">
                    <div>
                        <label class="text-xs font-bold text-slate-600">Education Track Segment</label>
                        <select name="class_id" class="w-full border p-2 rounded mt-1 bg-white text-sm font-medium text-slate-800" required>
                            <option value="" disabled selected>Select Grade...</option>
                            <option value="1">Grade 1</option>
                            <option value="2">Grade 2</option>
                            <option value="3">Grade 3</option>
                            <option value="4">Grade 4</option>
                            <option value="5">Grade 5</option>
                            <option value="6">Grade 6</option>
                            <option value="7">Grade 7</option>
                            <option value="8">Grade 8</option>
                            <option value="9">Grade 9</option>
                        </select>
                    </div>
                    <div><label class="text-xs font-bold text-slate-600">Class Stream Assignment</label><input type="text" name="stream" placeholder="e.g. N" class="w-full border p-2 rounded mt-1" required></div>
                </div>
                <div class="flex gap-3 pt-2">
                    <button type="submit" class="bg-emerald-700 text-white font-bold py-2 px-4 rounded hover:bg-emerald-800 transition">Save Student</button>
                    <a href="/admin/dashboard/{school_id}" class="bg-slate-200 text-slate-700 py-2 px-4 rounded hover:bg-slate-300 font-bold transition">Cancel</a>
                </div>
            </form>
        </div>
    </body>
    </html>
    """

@app.get("/staff/register-panel/{school_id}", response_class=HTMLResponse)
def staff_registration_panel(school_id: int):
    return f"""
    <!DOCTYPE html>
    <html>
    <head><title>Add Staff</title><script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script></head>
    <body class="bg-slate-100 flex items-center justify-center min-h-screen">
        <div class="bg-white p-6 rounded-xl border shadow w-full max-w-sm">
            <h2 class="text-lg font-black mb-3">Onboard Educator Node</h2>
            <form action="/api/v1/staff/add/{school_id}" method="post" class="space-y-3">
                <div><label class="text-xs font-bold text-slate-600">Staff Email Address</label><input type="email" name="email" class="w-full border p-2 rounded text-sm mt-1" required></div>
                <div><label class="text-xs font-bold text-slate-600">Initial Password String</label><input type="password" name="password" class="w-full border p-2 rounded text-sm mt-1" required></div>
                <button type="submit" class="w-full bg-slate-900 text-white font-bold py-2 rounded text-sm hover:bg-black transition">Create Staff Account</button>
            </form>
        </div>
    </body>
    </html>
    """
@app.post("/api/v1/staff/toggle-status/{staff_id}/{school_id}")
def toggle_staff_active_status(staff_id: int, school_id: int):
    """Safely disables or enables a staff account without wiping historical records."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            # Flips the current boolean value of is_verified (acting as our active flag)
            cur.execute("""
                UPDATE users 
                SET is_verified = NOT is_verified 
                WHERE id = %s AND school_id = %s;
            """, (staff_id, school_id))
            conn.commit()
            
    return RedirectResponse(url=f"/admin/dashboard/{school_id}", status_code=303)


@app.post("/api/v1/staff/delete/{staff_id}/{school_id}")
def delete_staff_permanently(staff_id: int, school_id: int):
    """Hard deletes a staff record. Use cautiously."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM users WHERE id = %s AND school_id = %s AND role = 'staff';", (staff_id, school_id))
            conn.commit()
            
    return RedirectResponse(url=f"/admin/dashboard/{school_id}", status_code=303)

@app.get("/admin/scores/manage/{school_id}", response_class=HTMLResponse)
def manage_individual_scores_view(school_id: int, student_id: int):
    with get_db_connection(row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM students WHERE id = %s AND school_id = %s;", (student_id, school_id))
            student = cur.fetchone()
            if not student:
                raise HTTPException(status_code=404, detail="Student target missing.")
            
            cur.execute("SELECT id, name FROM learning_areas WHERE education_level = %s ORDER BY name ASC;", (student['education_level'],))
            subjects = cur.fetchall()
            
            cur.execute("""
                SELECT ss.id as score_id, la.name as subject_name, ss.cycle_name, ss.raw_score 
                FROM student_scores ss
                JOIN learning_areas la ON ss.learning_area_id = la.id
                WHERE ss.student_id = %s;
            """, (student_id,))
            existing_scores = cur.fetchall()

    subject_options = "".join([f"<option value='{s['id']}'>{s['name']}</option>" for s in subjects])
    score_rows = "".join([f"""
        <tr class='border-b text-xs'>
            <td class='p-2 font-bold'>{s['subject_name']}</td>
            <td class='p-2'>{s['cycle_name']}</td>
            <td class='p-2 font-black text-emerald-800'>{float(s['raw_score'])}%</td>
            <td class='p-2 flex gap-2'>
                <form action='/api/v1/scores/delete/{school_id}' method='post' onsubmit="return confirm('Drop this evaluation parameter entirely?');">
                    <input type='hidden' name='score_id' value='{s['score_id']}'>
                    <input type='hidden' name='student_id' value='{student_id}'>
                    <button type='submit' class='text-red-600 font-bold hover:underline'>Drop Score</button>
                </form>
            </td>
        </tr>
    """ for s in existing_scores])

    return f"""
    <!DOCTYPE html>
    <html>
    <head><title>Edit Matrix for {esc(student['first_name'])}</title><script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script></head>
    <body class="bg-slate-50 p-8 min-h-screen max-w-3xl mx-auto space-y-6">
        <div class="bg-white p-6 rounded-2xl border shadow-xs flex justify-between items-center">
            <div>
                <h1 class="text-xl font-black">Score Management Engine Matrix</h1>
                <p class="text-xs text-slate-500 mt-1">Student context: <strong>{esc(student['first_name'])} {esc(student['last_name'])} ({esc(student['admission_number'])})</strong></p>
            </div>
            <a href="/admin/dashboard/{school_id}" class="bg-slate-200 px-4 py-1.5 rounded-lg text-xs font-bold hover:bg-slate-300">Return Deck</a>
        </div>

        <div class="grid grid-cols-1 md:grid-cols-2 gap-6">
            <div class="bg-white p-6 rounded-2xl border shadow-xs space-y-3">
                <h3 class="font-bold border-b pb-2 text-sm text-slate-700">✏️ Commit/Update Specific Assessment Area</h3>
                <form action="/api/v1/scores/upsert/{school_id}" method="post" class="space-y-3 text-xs">
                    <input type="hidden" name="student_id" value="{student_id}">
                    <div>
                        <label class="font-bold block text-slate-500">Learning Area</label>
                        <select name="learning_area_id" class="w-full border p-2 rounded mt-1">{subject_options}</select>
                    </div>
                    <div>
                        <label class="font-bold block text-slate-500">Phase Cycle</label>
                        <select name="cycle_name" class="w-full border p-2 rounded mt-1">
                            <option value="Opener">Opener</option>
                            <option value="Midterm">Midterm</option>
                            <option value="End Term">End Term</option>
                        </select>
                    </div>
                    <div>
                        <label class="font-bold block text-slate-500">Raw Mark Value (0 - 100%)</label>
                        <input type="number" step="0.01" min="0" max="100" name="raw_score" class="w-full border p-2 rounded mt-1" required>
                    </div>
                    <button type="submit" class="bg-slate-900 text-white py-2 px-4 rounded font-bold hover:bg-black transition">Commit Performance Mark</button>
                </form>
            </div>

            <div class="bg-white rounded-2xl border shadow-xs overflow-hidden">
                <h3 class="font-bold p-4 bg-slate-50 border-b text-sm text-slate-700">📋 Logged Scores for Student</h3>
                <table class="w-full text-left">
                    <thead>
                        <tr class="bg-slate-100 text-[10px] uppercase font-bold text-slate-500 border-b"><th class="p-2">Area</th><th class="p-2">Cycle</th><th class="p-2">Score</th><th class="p-2">Action</th></tr>
                    </thead>
                    <tbody>{score_rows or "<tr><td colspan='4' class='text-center p-4 text-xs italic text-slate-400'>No values captured.</td></tr>"}</tbody>
                </table>
            </div>
        </div>
    </body>
    </html>
    """
# --- 1. FIXED BULK SHEET ENTRY WORKSPACE ---
@app.get("/staff/bulk-entry/{school_id}", response_class=HTMLResponse)
def educators_bulk_entry_grid(
    school_id: int, 
    grade_name: str, 
    stream: str, 
    education_level: str, 
    learning_area_id: int = None, 
    cycle_name: str = "End Term"
):
    with get_db_connection(row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM schools WHERE id = %s;", (school_id,))
            school = cur.fetchone()
            
            # Fetch relevant subjects matched by the educational segment level
            cur.execute("SELECT id, name FROM learning_areas WHERE education_level = %s ORDER BY name ASC;", (education_level,))
            subjects = cur.fetchall()
            
            selected_area_id = learning_area_id or (subjects[0]['id'] if subjects else None)
            
            # FIXED: Added explicit JOIN onto classes table to resolve missing 'education_level' column runtime issue
            cur.execute("""
                SELECT s.id, s.admission_number, s.first_name, s.last_name 
                FROM students s
                JOIN classes c ON s.class_id = c.id
                WHERE s.school_id = %s 
                  AND c.grade_name = %s 
                  AND s.stream = %s 
                  AND (s.status IS NULL OR s.status != 'GRADUATED')
                ORDER BY s.admission_number ASC;
            """, (school_id, grade_name, stream))
            students = cur.fetchall()

            score_map = {}
            if selected_area_id:
                cur.execute("""
                    SELECT student_id, raw_score FROM student_scores 
                    WHERE learning_area_id = %s AND cycle_name = %s;
                """, (selected_area_id, cycle_name))
                for scr in cur.fetchall():
                    score_map[scr['student_id']] = float(scr['raw_score'])

    subject_options = "".join([f"<option value='{sub['id']}' {'selected' if sub['id'] == selected_area_id else ''}>{sub['name']}</option>" for sub in subjects])
    
    student_rows = ""
    for s in students:
        existing_val = score_map.get(s['id'], "")
        student_rows += f"""
        <tr class="border-b text-sm">
            <td class="p-3 font-semibold text-slate-600">{esc(s['admission_number'])}</td>
            <td class="p-3 font-bold text-slate-800">{esc(s['first_name'])} {esc(s['last_name'])}</td>
            <td class="p-3">
                <input type="number" step="0.01" min="0" max="100" name="score_{s['id']}" value="{existing_val}" class="border p-1.5 rounded w-32 focus:border-emerald-600 font-bold text-center" placeholder="-%">
            </td>
        </tr>
        """

    return f"""
    <!DOCTYPE html>
    <html>
    <head><title>Bulk Sheet Entry Deck</title><script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script></head>
    <body class="bg-slate-100 p-8 min-h-screen max-w-4xl mx-auto space-y-6">
        <div class="bg-white p-6 rounded-2xl border shadow-xs flex justify-between items-center">
            <div>
                <h1 class="text-xl font-black text-slate-900">⚡ Bulk Marks Management Interface</h1>
                <p class="text-xs text-slate-500 mt-1">Cohort Segment target: <strong>{esc(grade_name)} — {esc(education_level)} (Stream {esc(stream)})</strong></p>
            </div>
            <a href="/admin/dashboard/{school_id}" class="bg-slate-200 px-4 py-2 rounded-lg text-xs font-black hover:bg-slate-300">Exit Workspace</a>
        </div>

        <div class="bg-white p-6 rounded-2xl border shadow-xs">
            <form method="get" action="/staff/bulk-entry/{school_id}" class="grid grid-cols-1 sm:grid-cols-3 gap-4 text-xs">
                <input type="hidden" name="grade_name" value="{esc(grade_name)}">
                <input type="hidden" name="education_level" value="{esc(education_level)}">
                <input type="hidden" name="stream" value="{esc(stream)}">
                <div>
                    <label class="font-bold text-slate-500">Target Learning Subject</label>
                    <select name="learning_area_id" onchange="this.form.submit()" class="w-full border p-2 rounded mt-1 font-semibold">{subject_options}</select>
                </div>
                <div>
                    <label class="font-bold text-slate-500">Evaluation Phase</label>
                    <select name="cycle_name" onchange="this.form.submit()" class="w-full border p-2 rounded mt-1 font-semibold">
                        <option value="Opener" {"selected" if cycle_name == 'Opener' else ""}>Opener Phase</option>
                        <option value="Midterm" {"selected" if cycle_name == 'Midterm' else ""}>Midterm Cycle</option>
                        <option value="End Term" {"selected" if cycle_name == 'End Term' else ""}>End Term Synthesis</option>
                    </select>
                </div>
                <div class="flex items-end text-slate-400 text-[11px] italic pb-2">Changing dropdown values auto-updates student listing map.</div>
            </form>
        </div>

        <form action="/api/v1/scores/bulk-save/{school_id}" method="post" class="bg-white rounded-2xl border shadow-xs overflow-hidden">
            <input type="hidden" name="grade_name" value="{esc(grade_name)}">
            <input type="hidden" name="education_level" value="{esc(education_level)}">
            <input type="hidden" name="stream" value="{esc(stream)}">
            <input type="hidden" name="learning_area_id" value="{selected_area_id}">
            <input type="hidden" name="cycle_name" value="{cycle_name}">
            
            <table class="w-full text-left">
                <thead>
                    <tr class="bg-slate-50 text-slate-500 text-xs font-bold uppercase tracking-wider border-b"><th class="p-3">Admission ID</th><th class="p-3">Learner Name</th><th class="p-3">Awarded Score Percentage</th></tr>
                </thead>
                <tbody>{student_rows or "<tr><td colspan='3' class='text-center p-6 text-slate-400 italic text-xs'>No registered class matching criterion.</td></tr>"}</tbody>
            </table>
            
            {f'<div class="p-4 bg-slate-50 border-t text-right"><button type="submit" class="bg-[#046A38] hover:bg-emerald-900 text-white font-bold py-2 px-6 rounded-xl text-xs shadow-md">Batch Commit Class Sheet</button></div>' if students else ""}
        </form>
    </body>
    </html>
    """


@app.get("/api/v1/reports/bulk-print/{school_id}", response_class=HTMLResponse)
def output_batch_class_report_forms(school_id: int, grade_name: str, education_level: str, stream: str):
    # Utilizing connection context manager/pool cleanly
    with get_db_connection(row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            # 1. Look up institutional profiles dynamically
            cur.execute("SELECT * FROM schools WHERE id = %s;", (school_id,))
            school = cur.fetchone()
            
            cur.execute("SELECT * FROM school_settings WHERE school_id = %s;", (school_id,))
            settings = cur.fetchone()
            
            st = settings or {'active_year': 2026, 'active_term': 'Term 1', 'active_cycle': 'End Term', 'opening_date': 'TBD', 'closing_date': 'TBD'}
            theme = fetch_theme_styles(school.get('theme_color', 'emerald') if school else 'emerald')

            if not school:
                raise HTTPException(status_code=404, detail="Institution Tenant context missing.")

            # 🌟 Fixed Subject Average Aggregation to align perfectly with report card loop calculations
            cur.execute("""
                WITH subject_averages AS (
                    SELECT 
                        sc.student_id,
                        sc.learning_area_id,
                        AVG(sc.raw_score) AS subject_avg
                    FROM student_scores sc
                    WHERE sc.cycle_name IN ('Opener', 'Midterm', 'End Term')
                    GROUP BY sc.student_id, sc.learning_area_id
                ),
                student_mean_scores AS (
                    SELECT 
                        s.id AS student_id,
                        s.admission_number,
                        s.first_name,
                        s.last_name,
                        s.knec_lan,
                        s.stream,
                        c.grade_name,
                        COALESCE(AVG(sa.subject_avg), 0) AS final_calculated_mean
                    FROM students s
                    JOIN classes c ON s.class_id = c.id
                    LEFT JOIN subject_averages sa ON s.id = sa.student_id
                    WHERE s.school_id = %s 
                      AND c.grade_name = %s
                      AND (s.status IS NULL OR s.status != 'GRADUATED')
                    GROUP BY s.id, s.admission_number, s.first_name, s.last_name, s.knec_lan, s.stream, c.grade_name
                ),
                cohort_rankings AS (
                    SELECT 
                        *,
                        RANK() OVER (
                            PARTITION BY grade_name, stream 
                            ORDER BY final_calculated_mean DESC
                        ) AS stream_position,
                        COUNT(*) OVER (
                            PARTITION BY grade_name, stream
                        ) AS total_in_stream,
                        
                        RANK() OVER (
                            PARTITION BY grade_name 
                            ORDER BY final_calculated_mean DESC
                        ) AS grade_position,
                        COUNT(*) OVER (
                            PARTITION BY grade_name
                        ) AS total_in_grade
                    FROM student_mean_scores
                )
                SELECT * FROM cohort_rankings
                WHERE stream = %s
                ORDER BY admission_number;
            """, (school_id, grade_name, stream))
            students = cur.fetchall()
            
            if not students:
                return "<h2 style='font-family:sans-serif; text-align:center; padding:50px;'>No students registered in this segment group stream yet.</h2>"

            # 2. Extract curriculum guidelines dynamically based on structural segment parameters
            cur.execute("SELECT id, name FROM learning_areas WHERE education_level = %s ORDER BY name ASC;", (education_level,))
            subjects = cur.fetchall()

            report_cards_html = []
            for s in students:
                cur.execute("SELECT learning_area_id, cycle_name, raw_score FROM student_scores WHERE student_id = %s;", (s['student_id'],))
                scores = cur.fetchall()
                
                score_map = {}
                for sc in scores:
                    if sc['learning_area_id'] not in score_map:
                        score_map[sc['learning_area_id']] = {}
                    score_map[sc['learning_area_id']][sc['cycle_name']] = float(sc['raw_score'])

                rows_markup = ""
                total_evaluated_weight = 0
                total_subjects_count = 0
                accumulated_scale_points = 0
                
                # Performance tracking metrics across assessment milestones
                opener_sum, midterm_sum, endterm_sum = 0, 0, 0
                op_count, mid_count, end_count = 0, 0, 0

                for sub in subjects:
                    op = score_map.get(sub['id'], {}).get('Opener')
                    mid = score_map.get(sub['id'], {}).get('Midterm')
                    end = score_map.get(sub['id'], {}).get('End Term')

                    if op is not None:
                        opener_sum += op; op_count += 1
                    if mid is not None:
                        midterm_sum += mid; mid_count += 1
                    if end is not None:
                        endterm_sum += end; end_count += 1

                    active_cycles = [v for v in [op, mid, end] if v is not None]
                    if active_cycles:
                        weighted_total = sum(active_cycles) / len(active_cycles)
                        meta = evaluate_performance_metrics(weighted_total)
                        pld, pts, descriptor = meta['pld'], f"{meta['points']} Pt", meta['desc']
                        
                        total_evaluated_weight += weighted_total
                        total_subjects_count += 1
                        accumulated_scale_points += meta['points']
                    else:
                        pld, pts, descriptor = "-", "-", "-"

                    op_str = f"{op:.1f}%" if op is not None else "0%"
                    mid_str = f"{mid:.1f}%" if mid is not None else "0%"
                    end_str = f"{end:.1f}%" if end is not None else "0%"
                    weighted_str = f"{weighted_total:.1f}%" if active_cycles else "0%"

                    rows_markup += f"""
                    <tr>
                        <td style="padding: 4px 6px; border: 1px solid #222; font-weight:bold;">{sub['name']}</td>
                        <td style="padding: 4px 6px; border: 1px solid #222; text-align:center;">{op_str}</td>
                        <td style="padding: 4px 6px; border: 1px solid #222; text-align:center;">{mid_str}</td>
                        <td style="padding: 4px 6px; border: 1px solid #222; text-align:center;">{end_str}</td>
                        <td style="padding: 4px 6px; border: 1px solid #222; text-align:center; background:#f9f9f9; font-weight:bold;">{weighted_str}</td>
                        <td style="padding: 4px 6px; border: 1px solid #222; text-align:center; font-weight:bold;">{pld}</td>
                        <td style="padding: 4px 6px; border: 1px solid #222; text-align:center; font-weight:bold;">{pts}</td>
                        <td style="padding: 4px 6px; border: 1px solid #222; font-size:10px; line-height: 1.1;">{descriptor}</td>
                    </tr>
                    """

                avg_summary_percentage = total_evaluated_weight / total_subjects_count if total_subjects_count > 0 else 0.0
                summary_meta = evaluate_performance_metrics(avg_summary_percentage)

                # Compute baseline averages safely for graph generation
                op_avg = (opener_sum / op_count) if op_count > 0 else 0.0
                mid_avg = (midterm_sum / mid_count) if mid_count > 0 else 0.0
                end_avg = (endterm_sum / end_count) if end_count > 0 else 0.0

                logo_markup = f'<img src="/{school["logo_url"]}" style="width:105px; height:105px; object-fit:contain; margin-right:16px;" />' if school['logo_url'] else f'<div style="width:105px; height:105px; border:3px solid {theme["hex"]}; display:flex; align-items:center; justify-content:center; font-weight:bold; margin-right:16px; font-size:14px;">CREST</div>'

                report_cards_html.append(f"""
                <div class="report-card-container" style="background: white; padding: 24px; border: 5px solid {theme['hex']}; border-radius: 12px; max-width: 820px; box-sizing: border-box; margin: 0 auto; font-family: 'Arial', sans-serif; display: flex; flex-direction: column; justify-content: space-between;">
                    <div>
                        <div style="display: flex; align-items: center; border-bottom: 4px double {theme['hex']}; padding-bottom: 8px; margin-bottom: 12px;">
                            {logo_markup}
                            <div style="flex-grow:1; text-align:center;">
                                <h1 style="color:{theme['hex']}; font-size:28px; font-weight:900; margin:0 0 2px 0; text-transform:uppercase; letter-spacing:0.5px;">{esc(school['name'])}</h1>
                                <p style="margin:2px 0; font-size:12px; color:#222;"><b>Location Address:</b> {esc(school['physical_address'])} &nbsp;|&nbsp; <b>Sub-County:</b> {esc(school['sub_county'])}</p>
                                <div style="background:{theme['hex']}; color:white; font-weight:bold; font-size:13px; padding:4px; margin-top:6px; text-transform:uppercase; letter-spacing:1px; border-radius:4px;">Official {st['active_cycle']} Progress Analytics Report</div>
                            </div>
                        </div>

                        <div style="display:grid; grid-template-columns: 1fr 1fr; gap:4px 12px; background:#f8fafc; border:1px solid #cbd5e1; padding:10px; border-radius:6px; font-size:11px; margin-bottom:12px; line-height:1.4;">
                            <div><b>Learner Name:</b> <span style="font-weight:bold; text-transform:uppercase;">{esc(s['first_name'])} {esc(s['last_name'])}</span></div>
                            <div><b>Admission Identifier Number:</b> <span style="font-weight:bold;">{esc(s['admission_number'])}</span></div>
                            <div><b>Education Bracket:</b> {esc(grade_name)} ({esc(education_level)}) — Stream: <b>{esc(stream)}</b></div>
                            <div><b>KNEC Assessment Identifier (LAN):</b> {s['knec_lan'] or 'N/A'}</div>
                            <div><b>Calendar Timeline Context:</b> Year {st['active_year']} — {st['active_term']}</div>
                            <div style="color:{theme['hex']}; font-weight:bold;">Class Track Standings: Verified Dynamic Completion</div>
                        </div>

                        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-bottom: 12px;">
                            <div style="border: 1px dashed #475569; padding: 4px 8px; border-radius: 6px; background: #f8fafc; text-align: center;">
                                <span style="font-size: 10px; text-transform: uppercase; font-weight: bold; color: #64748b;">Position In Stream</span>
                                <div style="font-size: 15px; font-weight: 900; color: #1e3a8a; margin-top: 1px;">
                                    {s['stream_position']} <span style="font-size: 11px; font-weight: normal; color: #475569;">out of {s['total_in_stream']}</span>
                                </div>
                            </div>
                            <div style="border: 1px dashed #059669; padding: 4px 8px; border-radius: 6px; background: #f0fdf4; text-align: center;">
                                <span style="font-size: 10px; text-transform: uppercase; font-weight: bold; color: #059669;">Overall Position In Grade</span>
                                <div style="font-size: 15px; font-weight: 900; color: #065f46; margin-top: 1px;">
                                    {s['grade_position']} <span style="font-size: 11px; font-weight: normal; color: #475569;">out of {s['total_in_grade']}</span>
                                </div>
                            </div>
                        </div>

                        <table style="width:100%; border-collapse:collapse; font-size:11px;">
                            <thead>
                                <tr style="background:{theme['hex']}; color:white; text-transform:uppercase; font-size:10.5px;">
                                    <th style="padding:6px; border:1px solid #222; text-align:left;">CBE Learning Domain Area</th>
                                    <th style="padding:6px; border:1px solid #222; width:65px; text-align:center;">Opener</th>
                                    <th style="padding:6px; border:1px solid #222; width:65px; text-align:center;">Midterm</th>
                                    <th style="padding:6px; border:1px solid #222; width:65px; text-align:center;">End Term</th>
                                    <th style="padding:6px; border:1px solid #222; width:90px; text-align:center;">Weighted Avg</th>
                                    <th style="padding:6px; border:1px solid #222; width:70px; text-align:center;">CBE Code</th>
                                    <th style="padding:6px; border:1px solid #222; width:65px; text-align:center;">Scale Pts</th>
                                    <th style="padding:6px; border:1px solid #222; text-align:left;">Competence Descriptor Status</th>
                                </tr>
                            </thead>
                            <tbody>{rows_markup}</tbody>
                        </table>
                    </div>

                    <div style="margin-top: 10px;">
                        <div style="display: grid; grid-template-columns: 240px 1fr; gap: 14px; align-items: center; margin-bottom: 8px;">
                            <div style="border: 1px solid #cbd5e1; border-radius: 6px; padding: 6px; background: #f8fafc; text-align: center;">
                                <span style="font-size: 9.5px; font-weight: bold; text-transform: uppercase; color: #475569; display: block; margin-bottom: 4px;">Performance Milestone Graph</span>
                                <svg viewBox="0 0 200 80" style="width: 100%; height: 58px; overflow: visible;">
                                    <line x1="20" y1="10" x2="190" y2="10" stroke="#e2e8f0" stroke-width="0.5" />
                                    <line x1="20" y1="35" x2="190" y2="35" stroke="#e2e8f0" stroke-width="0.5" />
                                    <line x1="20" y1="60" x2="190" y2="60" stroke="#cbd5e1" stroke-width="1" />
                                    
                                    <text x="5" y="13" font-size="7" fill="#64748b" font-family="sans-serif">100%</text>
                                    <text x="5" y="38" font-size="7" fill="#64748b" font-family="sans-serif">50%</text>
                                    <text x="8" y="63" font-size="7" fill="#64748b" font-family="sans-serif">0%</text>
                                    
                                    <path d="M 40 {60 - (op_avg * 0.5)} L 105 {60 - (mid_avg * 0.5)} L 170 {60 - (end_avg * 0.5)}" fill="none" stroke="{theme['hex']}" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" />
                                    
                                    <circle cx="40" cy="{60 - (op_avg * 0.5)}" r="3.5" fill="#0f172a" />
                                    <circle cx="105" cy="{60 - (mid_avg * 0.5)}" r="3.5" fill="#0f172a" />
                                    <circle cx="170" cy="{60 - (end_avg * 0.5)}" r="3.5" fill="#0f172a" />
                                    
                                    <text x="28" y="73" font-size="7.5" font-weight="bold" fill="#334155" font-family="sans-serif">Opener ({op_avg:.1f}%)</text>
                                    <text x="90" y="73" font-size="7.5" font-weight="bold" fill="#334155" font-family="sans-serif">Mid ({mid_avg:.1f}%)</text>
                                    <text x="155" y="73" font-size="7.5" font-weight="bold" fill="#334155" font-family="sans-serif">End ({end_avg:.1f}%)</text>
                                </svg>
                            </div>

                            <div style="border:1px solid {theme['hex']}; background:#f4faf6; padding:10px; border-radius:6px; display:flex; flex-direction:column; justify-content:center; gap:6px; height:72px; box-sizing:border-box;">
                                <div style="display:flex; justify-content:space-between; font-weight:bold; font-size:11.5px;">
                                    <span>Cumulative Scale Points:</span>
                                    <span style="color:{theme['hex']}; font-weight:800;">{accumulated_scale_points} Pts</span>
                                </div>
                                <div style="display:flex; justify-content:space-between; font-weight:bold; font-size:11.5px;">
                                    <span>Mean Performance Score:</span>
                                    <span style="font-weight:800;">{avg_summary_percentage:.1f}%</span>
                                </div>
                                <div style="display:flex; justify-content:space-between; font-weight:bold; font-size:11.5px;">
                                    <span>Aggregated Summary Level:</span>
                                    <span style="background:white; padding:1px 6px; border:1px solid #333; border-radius:4px; color:{theme['hex']}; font-weight:800; font-size:10.5px;">{summary_meta['pld']}</span>
                                </div>
                            </div>
                        </div>

                        <div style="border: 1px solid #cbd5e1; padding: 8px; border-radius: 6px; background: #fafafa; font-size: 11px; line-height: 1.4;">
                            <div style="padding-bottom:4px; border-bottom:1px dashed #e2e8f0;"><b>Class Instructor Remarks:</b> Learner demonstrates comprehensive tracking capabilities across the specified competency tasks.</div>
                            <div style="padding-top:4px;"><b>Headteacher Institutional Verdict:</b> Satisfactory progress metrics established. Certified report presentation.</div>
                        </div>

                        <div style="display:flex; justify-content:space-between; margin-top: 8px; padding-top: 6px; border-top: 1.5px solid #cbd5e1; font-size: 11px; font-style: italic; color: #475569;">
                            <div><b>Current Term Closing Date:</b> {st['closing_date']}</div>
                            <div><b>Next Term Opening Date:</b> {st['opening_date']}</div>
                        </div>
                    </div>
                </div>
                """)

            joined_report_pages = "\n".join(report_cards_html)
            return f"""
            <!DOCTYPE html>
            <html>
            <head>
                <title>Print Out Queue Pipeline</title>
                <style>
                    * {{ box-sizing: border-box; }}
                    html, body {{ margin: 0; padding: 0; width: 100%; }}
                    
                    .report-card-container {{
                        page-break-inside: avoid !important;
                    }}
                    
                    .report-card-container:not(:last-child) {{
                        page-break-after: always !important;
                    }}
                    
                    @media print {{
                        @page {{
                            size: A4 portrait;
                            margin: 0;
                        }}
                        .no-print {{ display: none !important; }}
                        body {{ background: white !important; padding: 0 !important; }}
                        .report-card-container {{
                            border-radius: 0 !important;
                            box-shadow: none !important;
                            padding: 25px 30px !important;
                            margin: 0 auto !important;
                            border-width: 6px !important;
                        }}
                    }}
                </style>
            </head>
            <body style="background:#64748b; padding:30px 20px; margin:0;">
                <div class="no-print" style="max-width:820px; margin: 0 auto 20px auto; text-align:right;">
                    <button onclick="window.print()" style="background:#0f172a; color:white; border:none; padding:11px 22px; font-weight:bold; font-size:13px; border-radius:6px; cursor:pointer; box-shadow:0 3px 6px rgba(0,0,0,0.15);">🖨️ Commit Print Batch to Paper</button>
                </div>
                {joined_report_pages}
            </body>
            </html>
            """
@app.post("/api/v1/settings/update/{school_id}")
def update_settings_endpoint(
    school_id: int, 
    active_term: str = Form(...), 
    active_cycle: str = Form(...), 
    opening_date: str = Form(...), 
    closing_date: str = Form(...), 
    theme_color: str = Form(...)
):
    # Constrain free-text-ish fields to known-good values so a crafted POST
    # can't smuggle unexpected data (defense in depth beyond output escaping).
    allowed_terms = {"Term 1", "Term 2", "Term 3"}
    allowed_cycles = {"Opener", "Midterm", "End Term"}
    allowed_themes = {"emerald", "blue", "indigo", "purple", "slate"}

    if active_term not in allowed_terms:
        raise HTTPException(status_code=400, detail="Invalid academic term selected.")
    if active_cycle not in allowed_cycles:
        raise HTTPException(status_code=400, detail="Invalid assessment cycle selected.")
    if theme_color not in allowed_themes:
        theme_color = "emerald"

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            # FIXED: Exactly 5 columns now map directly to 5 variables in the tuple
            cur.execute("""
                INSERT INTO school_settings (school_id, active_term, active_cycle, opening_date, closing_date)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (school_id) DO UPDATE 
                SET active_term = EXCLUDED.active_term, 
                    active_cycle = EXCLUDED.active_cycle, 
                    opening_date = EXCLUDED.opening_date, 
                    closing_date = EXCLUDED.closing_date;
            """, (school_id, active_term, active_cycle, opening_date, closing_date))
            
            # Sync the modern Tailwind color layout across the institution node
            cur.execute("UPDATE schools SET theme_color = %s WHERE id = %s;", (theme_color, school_id))
            conn.commit()
            
    return RedirectResponse(url=f"/admin/dashboard/{school_id}", status_code=303)

@app.post("/api/v1/students/add/{school_id}")
def backend_add_student(
    school_id: int, 
    first_name: str = Form(...), 
    last_name: str = Form(...), 
    admission_number: str = Form(...), 
    class_id: int = Form(...), 
    stream: str = Form(None)
):
    # Clean up the string input value
    raw_stream = stream.strip().upper() if stream else ""
    
    # If it's left blank, assign the standard "SINGLE STREAM" token flag
    if not raw_stream or raw_stream == "":
        processed_stream = "SINGLE STREAM"
    else:
        # If they wrote '2N' instead of just 'N', strip off the number part gracefully
        processed_stream = raw_stream.replace("GRADE", "").replace(str(class_id), "").strip()
        if not processed_stream:
            processed_stream = "SINGLE STREAM"

    admission_number = admission_number.strip().upper()
    first_name = first_name.strip()
    last_name = last_name.strip()
    if not admission_number or not first_name or not last_name:
        raise HTTPException(status_code=400, detail="First name, last name, and admission number are required.")

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            # The `students` table requires education_level (NOT NULL); derive
            # it from the chosen class rather than leaving it unset, which
            # would otherwise raise an unhandled NOT NULL violation.
            cur.execute("SELECT education_level FROM classes WHERE id = %s;", (class_id,))
            class_row = cur.fetchone()
            if not class_row:
                raise HTTPException(status_code=400, detail="The selected grade/class does not exist.")
            education_level = class_row[0]

            cur.execute("""
                INSERT INTO students (school_id, admission_number, first_name, last_name, class_id, stream, education_level, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'ACTIVE');
            """, (school_id, admission_number, first_name, last_name, class_id, processed_stream, education_level))
            conn.commit()

    return RedirectResponse(url=f"/admin/dashboard/{school_id}", status_code=303)


@app.post("/api/v1/staff/add/{school_id}")
def add_staff_node(school_id: int, email: str = Form(...), password: str = Form(...)):
    email = email.strip().lower()
    if not email:
        raise HTTPException(status_code=400, detail="A staff email address is required.")
    if len(password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters long.")

    safe_staff_password = password[:72]
    hashed_password = pwd_context.hash(safe_staff_password)
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO users (email, password_hash, role, school_id, is_verified)
                VALUES (%s, %s, 'staff', %s, TRUE);
            """, (email, hashed_password, school_id))
            conn.commit()
    return RedirectResponse(url=f"/admin/dashboard/{school_id}", status_code=303)

@app.post("/api/v1/staff/toggle-verification/{school_id}")
def toggle_staff_verification(school_id: int, user_id: int = Form(...)):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET is_verified = NOT is_verified WHERE id = %s AND school_id = %s;", (user_id, school_id))
            conn.commit()
    return RedirectResponse(url=f"/admin/dashboard/{school_id}", status_code=303)

@app.post("/api/v1/scores/upsert/{school_id}")
def upsert_individual_score(school_id: int, student_id: int = Form(...), learning_area_id: int = Form(...), cycle_name: str = Form(...), raw_score: float = Form(...)):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM students WHERE id = %s AND school_id = %s;", (student_id, school_id))
            if not cur.fetchone():
                raise HTTPException(status_code=403, detail="Cross-tenant execution attempt blocked.")
            
            cur.execute("""
                INSERT INTO student_scores (student_id, learning_area_id, cycle_name, raw_score)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (student_id, learning_area_id, cycle_name) DO UPDATE SET raw_score = EXCLUDED.raw_score;
            """, (student_id, learning_area_id, cycle_name, raw_score))
            conn.commit()
    return RedirectResponse(url=f"/admin/scores/manage/{school_id}?student_id={student_id}", status_code=303)

@app.post("/api/v1/scores/delete/{school_id}")
def drop_individual_score(school_id: int, score_id: int = Form(...), student_id: int = Form(...)):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM students WHERE id = %s AND school_id = %s;", (student_id, school_id))
            if not cur.fetchone():
                raise HTTPException(status_code=403, detail="Cross-tenant execution attempt blocked.")
            cur.execute("DELETE FROM student_scores WHERE id = %s AND student_id = %s;", (score_id, student_id))
            conn.commit()
    return RedirectResponse(url=f"/admin/scores/manage/{school_id}?student_id={student_id}", status_code=303)

@app.post("/api/v1/scores/bulk-save/{school_id}")
async def batch_save_class_marks_matrix(school_id: int, request: Request):
    # Declared `async def` so we can await the form parse directly instead of
    # spinning up a nested event loop with asyncio.run() inside a sync route
    # (which is wasteful and can misbehave under some ASGI server setups).
    form_data = await request.form()

    try:
        learning_area_id = int(form_data.get('learning_area_id'))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="A valid learning area must be selected before saving.")

    cycle_name = (form_data.get('cycle_name') or "").strip()
    if not cycle_name:
        raise HTTPException(status_code=400, detail="An assessment cycle must be selected before saving.")

    skipped_entries = 0
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            for key, val in form_data.items():
                if not (key.startswith("score_") and str(val).strip() != ""):
                    continue

                # Guard against malformed field names or non-numeric scores so
                # one bad row doesn't abort the whole batch commit.
                try:
                    target_student_id = int(key.split("_")[1])
                    raw_score = float(val)
                except (IndexError, ValueError):
                    skipped_entries += 1
                    continue

                if not (0 <= raw_score <= 100):
                    skipped_entries += 1
                    continue

                cur.execute("SELECT id FROM students WHERE id = %s AND school_id = %s;", (target_student_id, school_id))
                if cur.fetchone():
                    cur.execute("""
                        INSERT INTO student_scores (student_id, learning_area_id, cycle_name, raw_score)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (student_id, learning_area_id, cycle_name) DO UPDATE SET raw_score = EXCLUDED.raw_score;
                    """, (target_student_id, learning_area_id, cycle_name, raw_score))
                else:
                    skipped_entries += 1
            conn.commit()

    if skipped_entries:
        logger.warning(f"Bulk score save for school {school_id} skipped {skipped_entries} invalid/mismatched entries.")

    return RedirectResponse(url=f"/admin/dashboard/{school_id}", status_code=303)

@app.post("/api/v1/wallet/stkpush/{school_id}")
def process_simulated_mpesa_stk_push(school_id: int, phone_number: str = Form(...), amount: float = Form(...)):
    phone_number = phone_number.strip()
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Top-up amount must be greater than zero.")
    if not re.fullmatch(r"[0-9+\s]{7,15}", phone_number):
        raise HTTPException(status_code=400, detail="Please provide a valid phone number.")

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE schools SET wallet_balance = wallet_balance + %s WHERE id = %s;", (amount, school_id))
            conn.commit()

    # Encode the user-supplied phone number as a JSON string literal (not just
    # HTML-escaped) before splicing it into inline JavaScript, since it sits
    # inside a `<script>` block where HTML-escaping alone would not stop
    # someone from breaking out of the quoted string.
    import json as _json
    safe_phone_js = _json.dumps(phone_number)
    return HTMLResponse(f"""
    <script>
        alert('STK Push Triggered to ' + {safe_phone_js} + ' successfully! Mock transaction completed.');
        window.location.href='/admin/dashboard/{school_id}';
    </script>
    """)

@app.post("/api/v1/school/promote-classes/{school_id}")
def promote_school_classes(school_id: int):
    # Map current class_id to the next consecutive class_id
    # 1: Grade 1 -> 2: Grade 2, ..., 9: Grade 9
    promotion_map = {
        1: 2,
        2: 3,
        3: 4,
        4: 5,
        5: 6,
        6: 7,
        7: 8,
        8: 9
    }
    
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            # Step 1: Safely graduate Grade 9 out of the active pool
            cur.execute("""
                UPDATE students 
                SET class_id = NULL, status = 'GRADUATED' 
                WHERE school_id = %s AND class_id = 9;
            """, (school_id,))
            
            # Step 2: Update remaining cohorts starting from the top down
            for current_class, next_class in sorted(promotion_map.items(), reverse=True):
                cur.execute("""
                    UPDATE students 
                    SET class_id = %s 
                    WHERE school_id = %s AND class_id = %s 
                      AND (status IS NULL OR status != 'GRADUATED');
                """, (next_class, school_id, current_class))
                
            conn.commit()
            
    # Redirect cleanly back to the administrative control panel
    return RedirectResponse(url=f"/admin/dashboard/{school_id}", status_code=303)
