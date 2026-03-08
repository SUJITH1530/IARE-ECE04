import csv
import io
import os
import re
import shutil
import sqlite3
from datetime import date, datetime, timedelta
from functools import wraps

from flask import (
    Response,
    Flask,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "ece_workshop_secret_key_change_me")
app.permanent_session_lifetime = timedelta(minutes=20)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SOURCE_STUDENTS_DIR = os.path.join(BASE_DIR, "students")
STORAGE_ROOT = os.environ.get("ATTENDANCE_STORAGE_DIR") or os.environ.get("RENDER_DISK_PATH")
if STORAGE_ROOT:
    STORAGE_ROOT = os.path.join(STORAGE_ROOT, "ece_attendance")
else:
    STORAGE_ROOT = BASE_DIR

STUDENTS_DIR = os.path.join(STORAGE_ROOT, "students")
ATTENDANCE_DIR = os.path.join(STORAGE_ROOT, "attendance")
ATTENDANCE_FILE = os.path.join(ATTENDANCE_DIR, "attendance_records.csv")
LEGACY_ATTENDANCE_FILE = os.path.join(BASE_DIR, "attendance", "attendance_records.csv")
DATABASE_PATH = os.path.join(STORAGE_ROOT, "attendance.db")
SESSION_OPTIONS = ("FN", "AN")
LOCK_WINDOW_MINUTES = 15
MAX_LOGIN_ATTEMPTS = 5
LOW_ATTENDANCE_THRESHOLD = 75.0

DEFAULT_STAFF_USERS = {
    "ecehod": {
        "password": "ece@04",
        "role": "hod",
        "email": "ecehod@example.com",
        "security_question": "What is your department code?",
        "security_answer": "ECE",
    },
    "faculty": {
        "password": "iare@1234",
        "role": "faculty",
        "email": "faculty@example.com",
        "security_question": "What is your department code?",
        "security_answer": "ECE",
    },
    "edit": {
        "password": "edit@95",
        "role": "editor",
        "email": "edit@example.com",
        "security_question": "What is your department code?",
        "security_answer": "ECE",
    },
}

STUDENT_COMMON_PASSWORD = os.environ.get("STUDENT_COMMON_PASSWORD", "IARE@2026")

WORKSHOP_FILES = {
    "vlsi": "vlsi_students.csv",
    "embedded": "embedded_students.csv",
    "not_in_workshop": "not_in_workshop.csv",
}

WORKSHOP_LABELS = {
    "vlsi": "VLSI Workshop",
    "embedded": "Embedded Systems Workshop",
    "not_in_workshop": "Students Not in Workshop",
}

SAMPLE_ROLL_NUMBERS = {
    "vlsi": ["22A91A0401", "22A91A0402", "22A91A0403"],
    "embedded": ["22A91A0411", "22A91A0412", "22A91A0413"],
    "not_in_workshop": ["22A91A0491", "22A91A0492", "22A91A0493"],
}


def ensure_directories_and_files() -> None:
    os.makedirs(STUDENTS_DIR, exist_ok=True)
    os.makedirs(ATTENDANCE_DIR, exist_ok=True)

    # Seed persistent student files from repo defaults on first run.
    for file_name in WORKSHOP_FILES.values():
        target_path = os.path.join(STUDENTS_DIR, file_name)
        source_path = os.path.join(SOURCE_STUDENTS_DIR, file_name)
        if os.path.exists(target_path):
            continue
        if os.path.exists(source_path) and os.path.abspath(source_path) != os.path.abspath(target_path):
            shutil.copyfile(source_path, target_path)


def get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_database() -> None:
    conn = get_db_connection()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS attendance_records (
                roll_number TEXT NOT NULL,
                status TEXT NOT NULL,
                date TEXT NOT NULL,
                session TEXT NOT NULL DEFAULT 'FN',
                posted_at TEXT,
                workshop_type TEXT NOT NULL,
                PRIMARY KEY (roll_number, date, session, workshop_type)
            )
            """
        )

        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(attendance_records)").fetchall()
        }
        if "posted_at" not in columns:
            conn.execute("ALTER TABLE attendance_records ADD COLUMN posted_at TEXT")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                username TEXT PRIMARY KEY,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL,
                email TEXT,
                security_question TEXT,
                security_answer_hash TEXT,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS login_attempts (
                username TEXT PRIMARY KEY,
                failed_attempts INTEGER NOT NULL DEFAULT 0,
                lock_until TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor TEXT NOT NULL,
                action TEXT NOT NULL,
                details TEXT,
                created_at TEXT NOT NULL
            )
            """
        )

        now_iso = datetime.now().isoformat(timespec="seconds")
        for username, data in DEFAULT_STAFF_USERS.items():
            existing = conn.execute(
                "SELECT username FROM users WHERE username = ?",
                (username,),
            ).fetchone()
            if existing:
                continue
            conn.execute(
                """
                INSERT INTO users
                (username, password_hash, role, email, security_question, security_answer_hash, is_active, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    username,
                    generate_password_hash(data["password"]),
                    data["role"],
                    data.get("email"),
                    data.get("security_question"),
                    generate_password_hash(data.get("security_answer", "")),
                    now_iso,
                    now_iso,
                ),
            )

        conn.commit()
    finally:
        conn.close()


def migrate_csv_attendance_to_db_if_needed() -> None:
    conn = get_db_connection()
    try:
        current_count = conn.execute("SELECT COUNT(*) FROM attendance_records").fetchone()[0]
        if current_count > 0:
            return

        csv_candidates = [ATTENDANCE_FILE, LEGACY_ATTENDANCE_FILE]
        rows = []
        for csv_path in csv_candidates:
            if not os.path.exists(csv_path):
                continue
            with open(csv_path, "r", newline="", encoding="utf-8") as file:
                reader = csv.DictReader(file)
                rows = list(reader)
            if rows:
                break

        if not rows:
            return

        for row in rows:
            roll_number = row.get("roll_number", "").strip()
            status = row.get("status", "Absent").strip() or "Absent"
            record_date = row.get("date", "").strip()
            session_name = (row.get("session") or "FN").strip() or "FN"
            posted_at = row.get("posted_at", "").strip() or None
            workshop_type = row.get("workshop_type", "").strip()

            if not (roll_number and record_date and workshop_type):
                continue

            conn.execute(
                """
                INSERT OR REPLACE INTO attendance_records
                (roll_number, status, date, session, posted_at, workshop_type)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    roll_number,
                    status,
                    record_date,
                    session_name,
                    posted_at,
                    workshop_type,
                ),
            )

        conn.commit()
    finally:
        conn.close()


def normalize_username(value: str) -> str:
    return (value or "").strip().lower()


def get_staff_user(username: str):
    normalized = normalize_username(username)
    if not normalized:
        return None
    conn = get_db_connection()
    try:
        row = conn.execute(
            """
            SELECT username, password_hash, role, email, security_question, security_answer_hash, is_active
            FROM users
            WHERE username = ?
            """,
            (normalized,),
        ).fetchone()
    finally:
        conn.close()
    return row


def list_staff_users(role: str | None = None) -> list[dict]:
    conn = get_db_connection()
    try:
        if role:
            rows = conn.execute(
                """
                SELECT username, role, email, is_active, created_at
                FROM users
                WHERE role = ?
                ORDER BY username
                """,
                (role,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT username, role, email, is_active, created_at
                FROM users
                ORDER BY role, username
                """
            ).fetchall()
    finally:
        conn.close()

    return [dict(row) for row in rows]


def create_faculty_user(username: str, password: str, email: str, security_answer: str) -> tuple[bool, str]:
    normalized = normalize_username(username)
    if not normalized or not re.fullmatch(r"[a-z0-9._-]{3,40}", normalized):
        return False, "Faculty username must be 3-40 chars using letters, numbers, dot, underscore or hyphen."
    if len(password) < 8:
        return False, "Faculty password must be at least 8 characters."

    conn = get_db_connection()
    try:
        existing = conn.execute("SELECT username FROM users WHERE username = ?", (normalized,)).fetchone()
        if existing:
            return False, "Faculty username already exists."
        now_iso = datetime.now().isoformat(timespec="seconds")
        conn.execute(
            """
            INSERT INTO users
            (username, password_hash, role, email, security_question, security_answer_hash, is_active, created_at, updated_at)
            VALUES (?, ?, 'faculty', ?, 'What is your department code?', ?, 1, ?, ?)
            """,
            (
                normalized,
                generate_password_hash(password),
                email.strip() or None,
                generate_password_hash((security_answer or "ECE").strip() or "ECE"),
                now_iso,
                now_iso,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return True, f"Faculty user {normalized} created."


def update_staff_password(username: str, new_password: str) -> tuple[bool, str]:
    normalized = normalize_username(username)
    if len(new_password) < 8:
        return False, "Password must be at least 8 characters."

    conn = get_db_connection()
    try:
        now_iso = datetime.now().isoformat(timespec="seconds")
        updated = conn.execute(
            """
            UPDATE users
            SET password_hash = ?, updated_at = ?
            WHERE username = ?
            """,
            (generate_password_hash(new_password), now_iso, normalized),
        ).rowcount
        conn.commit()
    finally:
        conn.close()

    if updated:
        return True, "Password updated successfully."
    return False, "User not found."


def remove_faculty_user(username: str) -> tuple[bool, str]:
    normalized = normalize_username(username)
    if normalized == "ecehod":
        return False, "HOD account cannot be removed."

    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT username, role FROM users WHERE username = ?",
            (normalized,),
        ).fetchone()
        if not row:
            return False, "Faculty user not found."
        if row["role"] != "faculty":
            return False, "Only faculty accounts can be removed from this action."
        conn.execute("DELETE FROM users WHERE username = ?", (normalized,))
        conn.commit()
    finally:
        conn.close()
    return True, f"Faculty user {normalized} removed."


def get_login_attempt_state(username: str) -> tuple[int, datetime | None]:
    normalized = normalize_username(username)
    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT failed_attempts, lock_until FROM login_attempts WHERE username = ?",
            (normalized,),
        ).fetchone()
    finally:
        conn.close()

    if not row:
        return 0, None

    lock_until = None
    if row["lock_until"]:
        try:
            lock_until = datetime.fromisoformat(row["lock_until"])
        except ValueError:
            lock_until = None
    return int(row["failed_attempts"] or 0), lock_until


def clear_login_attempts(username: str) -> None:
    normalized = normalize_username(username)
    conn = get_db_connection()
    try:
        now_iso = datetime.now().isoformat(timespec="seconds")
        conn.execute(
            """
            INSERT INTO login_attempts (username, failed_attempts, lock_until, updated_at)
            VALUES (?, 0, NULL, ?)
            ON CONFLICT(username) DO UPDATE SET
                failed_attempts = 0,
                lock_until = NULL,
                updated_at = excluded.updated_at
            """,
            (normalized, now_iso),
        )
        conn.commit()
    finally:
        conn.close()


def register_failed_attempt(username: str) -> tuple[int, datetime | None]:
    normalized = normalize_username(username)
    failed_attempts, _ = get_login_attempt_state(normalized)
    failed_attempts += 1
    lock_until = None
    if failed_attempts >= MAX_LOGIN_ATTEMPTS:
        lock_until = datetime.now() + timedelta(minutes=LOCK_WINDOW_MINUTES)

    conn = get_db_connection()
    try:
        now_iso = datetime.now().isoformat(timespec="seconds")
        conn.execute(
            """
            INSERT INTO login_attempts (username, failed_attempts, lock_until, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(username) DO UPDATE SET
                failed_attempts = excluded.failed_attempts,
                lock_until = excluded.lock_until,
                updated_at = excluded.updated_at
            """,
            (normalized, failed_attempts, lock_until.isoformat(timespec="seconds") if lock_until else None, now_iso),
        )
        conn.commit()
    finally:
        conn.close()

    return failed_attempts, lock_until


def add_audit_log(action: str, details: str = "", actor: str | None = None) -> None:
    actor_name = actor or session.get("username") or "system"
    conn = get_db_connection()
    try:
        conn.execute(
            "INSERT INTO audit_logs (actor, action, details, created_at) VALUES (?, ?, ?, ?)",
            (
                actor_name,
                action,
                details,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_recent_audit_logs(limit: int = 25) -> list[dict]:
    conn = get_db_connection()
    try:
        rows = conn.execute(
            """
            SELECT actor, action, details, created_at
            FROM audit_logs
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def is_valid_roll_number(roll_number: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9]{6,20}", (roll_number or "").strip()))


def get_all_students_with_workshop() -> list[dict]:
    rows = []
    for workshop_key in WORKSHOP_FILES:
        for roll in get_workshop_students(workshop_key):
            rows.append(
                {
                    "roll_number": roll,
                    "workshop": workshop_key,
                    "workshop_label": WORKSHOP_LABELS[workshop_key],
                }
            )
    rows.sort(key=lambda item: (item["workshop"], item["roll_number"]))
    return rows


def remove_student_from_workshop(workshop_key: str, roll_number: str) -> tuple[bool, str]:
    if workshop_key not in WORKSHOP_FILES:
        return False, "Invalid domain selected."
    clean_roll = (roll_number or "").strip().upper()
    students = get_workshop_students(workshop_key)
    if clean_roll not in students:
        return False, "Student not found in selected domain."

    updated_students = [item for item in students if item != clean_roll]
    file_path = os.path.join(STUDENTS_DIR, WORKSHOP_FILES[workshop_key])
    _write_students_csv(file_path, updated_students)
    return True, f"{clean_roll} removed from {WORKSHOP_LABELS[workshop_key]}."


def update_student_roll_number(workshop_key: str, old_roll: str, new_roll: str) -> tuple[bool, str]:
    if workshop_key not in WORKSHOP_FILES:
        return False, "Invalid domain selected."

    old_clean = (old_roll or "").strip().upper()
    new_clean = (new_roll or "").strip().upper()
    if not is_valid_roll_number(new_clean):
        return False, "New roll number format is invalid."

    students = get_workshop_students(workshop_key)
    if old_clean not in students:
        return False, "Original roll number not found in selected domain."
    if new_clean in students and new_clean != old_clean:
        return False, "New roll number already exists in selected domain."

    updated_students = [new_clean if roll == old_clean else roll for roll in students]
    file_path = os.path.join(STUDENTS_DIR, WORKSHOP_FILES[workshop_key])
    _write_students_csv(file_path, updated_students)
    return True, f"Updated {old_clean} to {new_clean}."


def move_student_between_workshops(source_key: str, target_key: str, roll_number: str) -> tuple[bool, str]:
    if source_key not in WORKSHOP_FILES or target_key not in WORKSHOP_FILES:
        return False, "Invalid domain selected."
    if source_key == target_key:
        return False, "Source and destination domains are the same."

    clean_roll = (roll_number or "").strip().upper()
    source_students = get_workshop_students(source_key)
    target_students = get_workshop_students(target_key)

    if clean_roll not in source_students:
        return False, "Student not found in source domain."
    if clean_roll in target_students:
        return False, "Student already exists in destination domain."

    source_students = [roll for roll in source_students if roll != clean_roll]
    target_students.append(clean_roll)

    _write_students_csv(os.path.join(STUDENTS_DIR, WORKSHOP_FILES[source_key]), source_students)
    _write_students_csv(os.path.join(STUDENTS_DIR, WORKSHOP_FILES[target_key]), target_students)
    return True, f"Moved {clean_roll} from {WORKSHOP_LABELS[source_key]} to {WORKSHOP_LABELS[target_key]}."


def validate_roll_numbers(roll_numbers: list[str]) -> dict:
    invalid = [roll for roll in roll_numbers if not is_valid_roll_number(roll)]
    seen = set()
    duplicates = []
    for roll in roll_numbers:
        if roll in seen and roll not in duplicates:
            duplicates.append(roll)
        seen.add(roll)
    return {
        "total_rows": len(roll_numbers),
        "unique_count": len(set(roll_numbers)),
        "invalid_rows": invalid,
        "duplicates": duplicates,
    }


def build_hod_analytics() -> dict:
    records = load_attendance_records()
    today = date.today().isoformat()
    cards = []

    for workshop_key in ("vlsi", "embedded"):
        students = get_workshop_students(workshop_key)
        status_map = {
            record["roll_number"]: record["status"]
            for record in records
            if record.get("workshop_type") == workshop_key and record.get("date") == today
        }
        present = sum(1 for roll in students if status_map.get(roll) == "Present")
        absent = max(0, len(students) - present)
        percentage = round((present / len(students)) * 100, 2) if students else 0.0
        cards.append(
            {
                "workshop_key": workshop_key,
                "workshop_label": WORKSHOP_LABELS[workshop_key],
                "present": present,
                "absent": absent,
                "attendance_percentage": percentage,
            }
        )

    low_attendance_students = []
    for workshop_key in ("vlsi", "embedded"):
        for roll in get_workshop_students(workshop_key):
            student_records = [
                record
                for record in records
                if record.get("workshop_type") == workshop_key and record.get("roll_number") == roll
            ]
            total_classes = len(student_records)
            if total_classes == 0:
                continue
            present_classes = sum(1 for record in student_records if record.get("status") == "Present")
            percentage = round((present_classes / total_classes) * 100, 2)
            if percentage < LOW_ATTENDANCE_THRESHOLD:
                low_attendance_students.append(
                    {
                        "roll_number": roll,
                        "workshop_label": WORKSHOP_LABELS[workshop_key],
                        "percentage": percentage,
                    }
                )

    trend_map = {}
    for record in records:
        workshop_key = record.get("workshop_type")
        if workshop_key not in {"vlsi", "embedded"}:
            continue
        trend_key = (record.get("date"), workshop_key)
        if trend_key not in trend_map:
            trend_map[trend_key] = {"present": 0, "total": 0}
        trend_map[trend_key]["total"] += 1
        if record.get("status") == "Present":
            trend_map[trend_key]["present"] += 1

    trend_rows = []
    for (record_date, workshop_key), data in sorted(trend_map.items(), reverse=True)[:10]:
        percentage = round((data["present"] / data["total"]) * 100, 2) if data["total"] else 0.0
        trend_rows.append(
            {
                "date": record_date,
                "workshop_label": WORKSHOP_LABELS.get(workshop_key, workshop_key),
                "present": data["present"],
                "total": data["total"],
                "percentage": percentage,
            }
        )

    return {
        "today": today,
        "cards": cards,
        "low_attendance_students": sorted(low_attendance_students, key=lambda item: item["percentage"]),
        "trend_rows": trend_rows,
    }


ensure_directories_and_files()
ensure_database()
migrate_csv_attendance_to_db_if_needed()


@app.before_request
def apply_session_timeout() -> None:
    if request.endpoint in {"login", "forgot_password", "static"}:
        return

    username = session.get("username")
    if not username:
        return

    last_active_raw = session.get("last_activity")
    if last_active_raw:
        try:
            last_active = datetime.fromisoformat(last_active_raw)
            if datetime.now() - last_active > app.permanent_session_lifetime:
                session.clear()
                flash("Session expired due to inactivity. Please login again.", "error")
                return redirect(url_for("login"))
        except ValueError:
            pass

    session["last_activity"] = datetime.now().isoformat(timespec="seconds")
    session.permanent = True


def role_required(expected_role):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            if "username" not in session:
                flash("Please login first.", "error")
                return redirect(url_for("login"))
            if session.get("role") != expected_role:
                flash("You do not have access to this page.", "error")
                return redirect(url_for("login"))
            return func(*args, **kwargs)

        return wrapper

    return decorator


def roles_required(*expected_roles):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            if "username" not in session:
                flash("Please login first.", "error")
                return redirect(url_for("login"))
            if session.get("role") not in expected_roles:
                flash("You do not have access to this page.", "error")
                return redirect(url_for("login"))
            return func(*args, **kwargs)

        return wrapper

    return decorator


def save_uploaded_student_file(file_storage, workshop_key: str) -> None:
    filename = WORKSHOP_FILES[workshop_key]
    target_path = os.path.join(STUDENTS_DIR, filename)

    temp_name = secure_filename(file_storage.filename)
    temp_path = os.path.join(STUDENTS_DIR, f"temp_{temp_name}")
    file_storage.save(temp_path)

    roll_numbers = read_roll_numbers_from_csv(temp_path)

    try:
        _write_students_csv(target_path, roll_numbers)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


def _write_students_csv(file_path: str, roll_numbers: list[str]) -> None:
    # Write to a temp file and atomically replace to avoid partially written CSV files.
    temp_path = f"{file_path}.tmp"
    with open(temp_path, "w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(["roll_number"])
        for roll in roll_numbers:
            writer.writerow([roll])
    os.replace(temp_path, file_path)


def read_roll_numbers_from_csv(path: str) -> list:
    if not os.path.exists(path):
        return []

    roll_numbers = []
    with open(path, "r", newline="", encoding="utf-8-sig") as file:
        reader = csv.reader(file)
        for row in reader:
            if not row:
                continue
            value = row[0].strip()
            if not value:
                continue
            if value.lower() in {"roll_number", "rollnumber", "roll no", "roll_no", "roll"}:
                continue
            roll_numbers.append(value)

    # Preserve order while removing duplicates.
    seen = set()
    unique_rolls = []
    for roll in roll_numbers:
        if roll not in seen:
            seen.add(roll)
            unique_rolls.append(roll)
    return unique_rolls


def get_workshop_students(workshop_key: str) -> list:
    file_name = WORKSHOP_FILES[workshop_key]
    file_path = os.path.join(STUDENTS_DIR, file_name)
    roll_numbers = read_roll_numbers_from_csv(file_path)
    if roll_numbers:
        return roll_numbers

    # Fall back to packaged defaults if persistent storage file is missing/empty.
    source_path = os.path.join(SOURCE_STUDENTS_DIR, file_name)
    return read_roll_numbers_from_csv(source_path)


def build_users() -> dict:
    users = {}
    for workshop_key in WORKSHOP_FILES:
        for roll_number in get_workshop_students(workshop_key):
            normalized_roll = roll_number.strip().upper()
            if normalized_roll:
                users.setdefault(
                    normalized_roll,
                    {
                        "username": normalized_roll,
                        "password": STUDENT_COMMON_PASSWORD,
                        "role": "student",
                    },
                )
    return users


def add_student_to_workshop(workshop_key: str, roll_number: str) -> tuple[bool, str]:
    if workshop_key not in WORKSHOP_FILES:
        return False, "Invalid domain selected."

    clean_roll = roll_number.strip().upper()
    if not clean_roll:
        return False, "Roll number is required."
    if not is_valid_roll_number(clean_roll):
        return False, "Invalid roll number format."

    students = get_workshop_students(workshop_key)
    if clean_roll in students:
        return False, f"{clean_roll} already exists in {WORKSHOP_LABELS[workshop_key]}."

    students.append(clean_roll)
    file_path = os.path.join(STUDENTS_DIR, WORKSHOP_FILES[workshop_key])
    _write_students_csv(file_path, students)

    return True, f"{clean_roll} added to {WORKSHOP_LABELS[workshop_key]}."


def load_attendance_records() -> list:
    conn = get_db_connection()
    try:
        rows = conn.execute(
            """
            SELECT roll_number, status, date, session, posted_at, workshop_type
            FROM attendance_records
            """
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        # Re-seed from CSV snapshot if DB exists but has no rows.
        migrate_csv_attendance_to_db_if_needed()
        conn = get_db_connection()
        try:
            rows = conn.execute(
                """
                SELECT roll_number, status, date, session, posted_at, workshop_type
                FROM attendance_records
                """
            ).fetchall()
        finally:
            conn.close()

    return [
        {
            "roll_number": row["roll_number"],
            "status": row["status"],
            "date": row["date"],
            "session": row["session"],
            "posted_at": row["posted_at"],
            "workshop_type": row["workshop_type"],
        }
        for row in rows
    ]


def build_report_rows(selected_date: str, workshop_key: str, selected_session: str) -> tuple[list, int, int]:
    records = [
        record
        for record in load_attendance_records()
        if (
            record.get("date") == selected_date
            and record.get("workshop_type") == workshop_key
            and (record.get("session") or "FN") == selected_session
        )
    ]

    status_map = {record["roll_number"]: record["status"] for record in records}
    all_students = get_workshop_students(workshop_key)

    report_rows = []
    present_count = 0
    absent_count = 0
    for roll in all_students:
        status = status_map.get(roll, "Absent")
        if status == "Present":
            present_count += 1
        else:
            absent_count += 1
        report_rows.append({"roll_number": roll, "status": status})

    return report_rows, present_count, absent_count


def save_attendance_records(records: list) -> None:
    conn = get_db_connection()
    try:
        conn.execute("DELETE FROM attendance_records")
        for record in records:
            conn.execute(
                """
                INSERT OR REPLACE INTO attendance_records
                (roll_number, status, date, session, posted_at, workshop_type)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    record.get("roll_number", ""),
                    record.get("status", "Absent"),
                    record.get("date", ""),
                    record.get("session", "FN"),
                    record.get("posted_at"),
                    record.get("workshop_type", ""),
                ),
            )
        conn.commit()
    finally:
        conn.close()

    # Store a CSV snapshot for portability and optional recovery.
    with open(ATTENDANCE_FILE, "w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(["roll_number", "status", "date", "session", "workshop_type", "posted_at"])
        for record in records:
            writer.writerow(
                [
                    record.get("roll_number", ""),
                    record.get("status", "Absent"),
                    record.get("date", ""),
                    record.get("session", "FN"),
                    record.get("workshop_type", ""),
                    record.get("posted_at") or "",
                ]
            )


def _pdf_escape_text(value: str) -> str:
    safe_value = (value or "").replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    return safe_value.encode("latin-1", "replace").decode("latin-1")


def build_simple_attendance_pdf(
    selected_date: str,
    workshop_label: str,
    selected_session: str,
    report_rows: list,
    present_count: int,
    absent_count: int,
    report_title: str = "Complete Report (Present & Absent)",
) -> bytes:
    def draw_text(commands: list, x: float, y: float, text: str, font: str, size: int) -> None:
        commands.append(
            (
                f"BT /{font} {size} Tf 1 0 0 1 {x:.2f} {y:.2f} Tm "
                f"({_pdf_escape_text(text)}) Tj ET"
            )
        )

    def center_x(text: str, size: int, page_width: float = 612.0) -> float:
        estimated_width = len(text) * size * 0.5
        return max(30.0, (page_width - estimated_width) / 2.0)

    def format_date_for_header(value: str) -> str:
        try:
            return datetime.strptime(value, "%Y-%m-%d").strftime("%d-%m-%Y")
        except ValueError:
            return value

    title_upper = workshop_label.upper()
    if "VLSI" in title_upper:
        workshop_title = "VLSI Attendance Summary"
    elif "EMBEDDED" in title_upper:
        workshop_title = "Embedded Systems Attendance Summary"
    else:
        workshop_title = f"{workshop_label} Attendance Summary"

    page_width = 612.0
    left_margin = 36.0
    table_width = page_width - (left_margin * 2)
    row_height = 20.0
    table_start_y = 560.0
    min_bottom_y = 60.0
    rows_per_page = max(1, int((table_start_y - min_bottom_y) // row_height) - 1)

    pages = []
    for start in range(0, len(report_rows), rows_per_page):
        pages.append(report_rows[start : start + rows_per_page])
    if not pages:
        pages = [[]]

    objects = []

    def add_object(content: bytes) -> int:
        objects.append(content)
        return len(objects)

    catalog_obj = add_object(b"<< /Type /Catalog /Pages 2 0 R >>")
    pages_obj = add_object(b"<< /Type /Pages /Kids [] /Count 0 >>")

    page_object_numbers = []

    for page_index, page_rows in enumerate(pages, start=1):
        content_lines = []

        content_lines.append("0.7 w")
        content_lines.append("0 0 0 RG")
        content_lines.append("24 24 564 744 re S")

        content_lines.append("0 0 0 rg")
        draw_text(
            content_lines,
            center_x("Institute of Aeronautical Engineering", 16),
            748,
            "Institute of Aeronautical Engineering",
            "F2",
            16,
        )
        draw_text(
            content_lines,
            center_x("Electronics and Communication Engineering", 12),
            728,
            "Electronics and Communication Engineering",
            "F1",
            12,
        )
        draw_text(
            content_lines,
            center_x(workshop_title, 12),
            706,
            workshop_title,
            "F2",
            12,
        )

        date_text = f"Date: {format_date_for_header(selected_date)}"
        session_text = f"Session: {selected_session}"
        draw_text(content_lines, center_x(date_text, 10), 688, date_text, "F1", 10)
        draw_text(content_lines, page_width - 160, 688, session_text, "F1", 10)

        content_lines.append("0 0 0 RG")
        content_lines.append("24 670 m 588 670 l S")
        draw_text(
            content_lines,
            center_x(report_title, 10),
            640,
            report_title,
            "F2",
            10,
        )

        summary_text = f"Present: {present_count}      Absent: {absent_count}"
        draw_text(content_lines, center_x(summary_text, 10), 622, summary_text, "F1", 10)

        header_y = table_start_y
        content_lines.append("0.20 0.29 0.41 rg")
        content_lines.append(f"{left_margin:.2f} {header_y:.2f} {table_width:.2f} {row_height:.2f} re f")

        content_lines.append("1 1 1 rg")
        draw_text(content_lines, left_margin + 8, header_y + 6, "S.No", "F2", 10)
        draw_text(content_lines, left_margin + 70, header_y + 6, "Roll No", "F2", 10)
        draw_text(content_lines, left_margin + 370, header_y + 6, "Status", "F2", 10)

        current_y = header_y - row_height
        for idx, row in enumerate(page_rows):
            row_number = ((page_index - 1) * rows_per_page) + idx + 1
            status = row.get("status", "Absent")
            roll_number = row.get("roll_number", "")

            if idx % 2 == 0:
                content_lines.append("0.96 0.96 0.96 rg")
            else:
                content_lines.append("0.92 0.92 0.92 rg")
            content_lines.append(f"{left_margin:.2f} {current_y:.2f} {table_width:.2f} {row_height:.2f} re f")

            content_lines.append("0 0 0 rg")
            draw_text(content_lines, left_margin + 8, current_y + 6, str(row_number), "F1", 10)
            draw_text(content_lines, left_margin + 70, current_y + 6, roll_number, "F1", 10)

            if status == "Present":
                content_lines.append("0.10 0.45 0.20 rg")
            else:
                content_lines.append("0.65 0.13 0.13 rg")
            draw_text(content_lines, left_margin + 370, current_y + 6, status, "F2", 10)

            current_y -= row_height

        table_height = row_height * (len(page_rows) + 1)
        table_bottom = header_y - table_height + row_height
        content_lines.append("0 0 0 RG")
        content_lines.append("0.5 w")
        content_lines.append(
            f"{left_margin:.2f} {table_bottom:.2f} {table_width:.2f} {table_height:.2f} re S"
        )
        content_lines.append(
            f"{left_margin + 60:.2f} {table_bottom:.2f} m {left_margin + 60:.2f} {header_y + row_height:.2f} l S"
        )
        content_lines.append(
            f"{left_margin + 350:.2f} {table_bottom:.2f} m {left_margin + 350:.2f} {header_y + row_height:.2f} l S"
        )

        draw_text(content_lines, page_width - 125, 34, f"Page {page_index}", "F1", 9)

        content_stream = "\n".join(content_lines).encode("latin-1", "replace")

        content_obj = add_object(
            b"<< /Length "
            + str(len(content_stream)).encode("ascii")
            + b" >>\nstream\n"
            + content_stream
            + b"\nendstream"
        )
        page_obj = add_object(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 "
            + str(0).encode("ascii")
            + b" 0 R /F2 "
            + str(0).encode("ascii")
            + b" 0 R >> >> /Contents "
            + str(content_obj).encode("ascii")
            + b" 0 R >>"
        )
        page_object_numbers.append(page_obj)

    font_obj = add_object(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    bold_font_obj = add_object(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>")

    for page_obj_num in page_object_numbers:
        original = objects[page_obj_num - 1]
        objects[page_obj_num - 1] = (
            original.replace(b"/F1 0 0 R", f"/F1 {font_obj} 0 R".encode("ascii"))
            .replace(b"/F2 0 0 R", f"/F2 {bold_font_obj} 0 R".encode("ascii"))
        )

    kids = b"[" + b" ".join(f"{num} 0 R".encode("ascii") for num in page_object_numbers) + b"]"
    objects[pages_obj - 1] = (
        b"<< /Type /Pages /Kids "
        + kids
        + b" /Count "
        + str(len(page_object_numbers)).encode("ascii")
        + b" >>"
    )

    result = bytearray()
    result.extend(b"%PDF-1.4\n")
    offsets = [0]

    for index, obj_content in enumerate(objects, start=1):
        offsets.append(len(result))
        result.extend(f"{index} 0 obj\n".encode("ascii"))
        result.extend(obj_content)
        result.extend(b"\nendobj\n")

    xref_start = len(result)
    result.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    result.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        result.extend(f"{offset:010d} 00000 n \n".encode("ascii"))

    result.extend(
        b"trailer\n<< /Size "
        + str(len(objects) + 1).encode("ascii")
        + b" /Root "
        + str(catalog_obj).encode("ascii")
        + b" 0 R >>\nstartxref\n"
        + str(xref_start).encode("ascii")
        + b"\n%%EOF"
    )

    return bytes(result)


@app.route("/", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        normalized_username = normalize_username(username)

        failed_attempts, lock_until = get_login_attempt_state(normalized_username)
        if lock_until and datetime.now() < lock_until:
            flash(
                (
                    f"Account is locked. Try again after "
                    f"{lock_until.strftime('%Y-%m-%d %I:%M %p')}."
                ),
                "error",
            )
            return render_template("login.html")

        staff_user = get_staff_user(normalized_username)
        if staff_user and int(staff_user["is_active"]) != 1:
            flash("This account is inactive. Contact administrator.", "error")
            return render_template("login.html")

        staff_authenticated = bool(
            staff_user and check_password_hash(staff_user["password_hash"], password)
        )
        if staff_authenticated:
            clear_login_attempts(normalized_username)
            role = staff_user["role"]
            session.clear()
            session["username"] = normalized_username
            session["role"] = role
            session["last_activity"] = datetime.now().isoformat(timespec="seconds")
            session.permanent = True
            add_audit_log("LOGIN_SUCCESS", f"Role={role}", actor=normalized_username)
            flash("Login successful.", "success")
            if role in {"hod", "editor"}:
                return redirect(url_for("hod_dashboard"))
            return redirect(url_for("faculty_dashboard"))

        all_users = build_users()
        user = all_users.get((username or "").strip().upper())

        if user and user["password"] == password:
            clear_login_attempts((username or "").strip())
            session.clear()
            session["username"] = user["username"]
            session["role"] = user["role"]
            session["last_activity"] = datetime.now().isoformat(timespec="seconds")
            session.permanent = True
            add_audit_log("LOGIN_SUCCESS", "Role=student", actor=user["username"])
            flash("Login successful.", "success")
            return redirect(url_for("student_dashboard"))

        attempts, new_lock_until = register_failed_attempt(normalized_username)
        remaining = max(0, MAX_LOGIN_ATTEMPTS - attempts)
        if new_lock_until:
            add_audit_log("LOGIN_LOCKED", "Account temporarily locked after repeated failures.", actor=normalized_username)
            flash(
                (
                    f"Too many failed attempts. Account locked until "
                    f"{new_lock_until.strftime('%Y-%m-%d %I:%M %p')}."
                ),
                "error",
            )
        else:
            flash(f"Invalid username or password. Attempts remaining: {remaining}", "error")

        add_audit_log("LOGIN_FAILED", "Invalid credentials.", actor=normalized_username)

    return render_template("login.html")

@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    security_question = "What is your department code?"
    if request.method == "POST":
        username = normalize_username(request.form.get("username", ""))
        security_answer = request.form.get("security_answer", "").strip()
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")

        user = get_staff_user(username)
        if not user:
            flash("User not found.", "error")
            return render_template("forgot_password.html", security_question=security_question)

        if user["role"] not in {"hod", "faculty", "editor"}:
            flash("Password reset is available only for HOD, Faculty, and Edit accounts.", "error")
            return render_template("forgot_password.html", security_question=security_question)

        if new_password != confirm_password:
            flash("New password and confirmation do not match.", "error")
            return render_template("forgot_password.html", security_question=security_question)

        answer_hash = user["security_answer_hash"] or ""
        valid_answer = any(
            check_password_hash(answer_hash, candidate)
            for candidate in {security_answer, security_answer.lower(), security_answer.upper()}
            if candidate
        )
        if not valid_answer:
            add_audit_log("FORGOT_PASSWORD_FAILED", "Security answer mismatch.", actor=username)
            flash("Security answer is incorrect.", "error")
            return render_template("forgot_password.html", security_question=security_question)

        ok, message = update_staff_password(username, new_password)
        if ok:
            clear_login_attempts(username)
            add_audit_log("PASSWORD_RESET", "Password changed through forgot password flow.", actor=username)
            flash("Password reset successful. Please login.", "success")
            return redirect(url_for("login"))

        flash(message, "error")

    return render_template("forgot_password.html", security_question=security_question)


@app.route("/logout")
def logout():
    actor = session.get("username")
    session.clear()
    if actor:
        add_audit_log("LOGOUT", "User logged out.", actor=actor)
    flash("Logged out successfully.", "success")
    return redirect(url_for("login"))


@app.route("/hod/dashboard", methods=["GET", "POST"])
@roles_required("hod", "editor")
def hod_dashboard():
    can_manage_accounts = session.get("role") == "editor"

    if request.method == "POST":
        if request.form.get("confirm_csv_upload") == "1":
            preview_payload = session.get("csv_preview_payload") or {}
            if not preview_payload:
                flash("No preview data found. Upload CSV files first.", "error")
            else:
                for workshop_key in WORKSHOP_FILES:
                    payload = preview_payload.get(workshop_key)
                    if not payload:
                        continue
                    _write_students_csv(
                        os.path.join(STUDENTS_DIR, WORKSHOP_FILES[workshop_key]),
                        payload.get("roll_numbers", []),
                    )
                session.pop("csv_preview_payload", None)
                session.pop("csv_preview_report", None)
                add_audit_log("CSV_UPLOAD_CONFIRMED", "Student CSV files uploaded after validation preview.")
                flash("Student CSV files uploaded successfully.", "success")
        elif request.form.get("cancel_csv_preview") == "1":
            session.pop("csv_preview_payload", None)
            session.pop("csv_preview_report", None)
            flash("CSV upload preview canceled.", "success")
        else:
            files_map = {
                "vlsi": request.files.get("vlsi_file"),
                "embedded": request.files.get("embedded_file"),
                "not_in_workshop": request.files.get("not_in_workshop_file"),
            }

            preview_payload = {}
            preview_report = {}
            uploaded_any = False

            try:
                for workshop_key, file_storage in files_map.items():
                    if not (file_storage and file_storage.filename):
                        continue
                    uploaded_any = True

                    temp_name = secure_filename(file_storage.filename)
                    temp_path = os.path.join(STUDENTS_DIR, f"preview_{workshop_key}_{temp_name}")
                    file_storage.save(temp_path)
                    try:
                        roll_numbers = [roll.strip().upper() for roll in read_roll_numbers_from_csv(temp_path) if roll.strip()]
                    finally:
                        if os.path.exists(temp_path):
                            os.remove(temp_path)

                    validation = validate_roll_numbers(roll_numbers)
                    existing_students = set(get_workshop_students(workshop_key))
                    validation["already_existing"] = sorted(list(set(roll_numbers) & existing_students))
                    validation["valid_rows"] = [roll for roll in roll_numbers if is_valid_roll_number(roll)]

                    preview_payload[workshop_key] = {"roll_numbers": roll_numbers}
                    preview_report[workshop_key] = validation

                if not uploaded_any:
                    flash("Please choose at least one CSV file.", "error")
                else:
                    session["csv_preview_payload"] = preview_payload
                    session["csv_preview_report"] = preview_report
                    flash("CSV validation preview generated. Please review and confirm upload.", "success")
            except Exception as exc:
                flash(f"Error while preparing CSV preview: {exc}", "error")

    counts = {
        "vlsi": len(get_workshop_students("vlsi")),
        "embedded": len(get_workshop_students("embedded")),
        "not_in_workshop": len(get_workshop_students("not_in_workshop")),
    }
    return render_template(
        "hod_dashboard.html",
        counts=counts,
        analytics=build_hod_analytics(),
        faculty_users=list_staff_users(role="faculty"),
        students=get_all_students_with_workshop(),
        audit_logs=get_recent_audit_logs(limit=20),
        csv_preview_report=session.get("csv_preview_report") or {},
        can_manage_accounts=can_manage_accounts,
    )


@app.route("/faculty/dashboard")
@role_required("faculty")
def faculty_dashboard():
    counts = {
        "vlsi": len(get_workshop_students("vlsi")),
        "embedded": len(get_workshop_students("embedded")),
        "not_in_workshop": len(get_workshop_students("not_in_workshop")),
    }
    return render_template("faculty_dashboard.html", counts=counts)


@app.route("/student/dashboard")
@role_required("student")
def student_dashboard():
    username = session.get("username", "").strip().upper()
    selected_workshop_key = None
    workshop_label = "Not in workshop"
    for key in WORKSHOP_FILES:
        if username in get_workshop_students(key):
            workshop_label = WORKSHOP_LABELS[key]
            selected_workshop_key = key
            break

    current_date = date.today().isoformat()
    checked_at = datetime.now().strftime("%Y-%m-%d %I:%M:%S %p")
    posted_attendance = None
    posted_at_display = None
    attendance_percentage = None
    present_classes = 0
    total_classes = 0
    absent_classes = 0
    attendance_timeline = []

    if selected_workshop_key:
        student_records = [
            record
            for record in load_attendance_records()
            if (
                record.get("roll_number") == username
                and record.get("workshop_type") == selected_workshop_key
            )
        ]
        total_classes = len(student_records)
        present_classes = sum(
            1 for record in student_records if record.get("status") == "Present"
        )
        absent_classes = total_classes - present_classes
        if total_classes > 0:
            attendance_percentage = round((present_classes / total_classes) * 100, 2)

        def _timeline_sort_key(item: dict) -> tuple:
            raw_date = item.get("date") or ""
            try:
                parsed_date = datetime.strptime(raw_date, "%Y-%m-%d")
            except ValueError:
                parsed_date = datetime.min
            session_rank = 1 if (item.get("session") or "FN") == "AN" else 0
            return parsed_date, session_rank

        for record in sorted(student_records, key=_timeline_sort_key, reverse=True):
            raw_date = record.get("date") or ""
            try:
                parsed = datetime.strptime(raw_date, "%Y-%m-%d")
                date_label = parsed.strftime("%d %b %Y")
                weekday_label = parsed.strftime("%a")
            except ValueError:
                date_label = raw_date
                weekday_label = "-"

            posted_at_label = "N/A"
            raw_posted_at = record.get("posted_at")
            if raw_posted_at:
                try:
                    posted_at_label = datetime.fromisoformat(raw_posted_at).strftime(
                        "%d %b %Y %I:%M %p"
                    )
                except (ValueError, TypeError):
                    posted_at_label = str(raw_posted_at)

            attendance_timeline.append(
                {
                    "date": raw_date,
                    "date_label": date_label,
                    "weekday": weekday_label,
                    "session": record.get("session") or "FN",
                    "status": record.get("status") or "Absent",
                    "posted_at_label": posted_at_label,
                }
            )

        today_records = [
            record
            for record in student_records
            if (
                record.get("date") == current_date
            )
        ]
        if today_records:
            # Prefer AN over FN for same-day display when both exist.
            posted_attendance = sorted(
                today_records,
                key=lambda item: 1 if (item.get("session") or "FN") == "AN" else 0,
            )[-1]

        if posted_attendance and posted_attendance.get("posted_at"):
            raw_posted_at = posted_attendance.get("posted_at")
            try:
                posted_at_display = datetime.fromisoformat(raw_posted_at).strftime(
                    "%Y-%m-%d %I:%M:%S %p"
                )
            except (ValueError, TypeError):
                posted_at_display = str(raw_posted_at)

    if attendance_percentage is None:
        attendance_percentage = 0.0

    return render_template(
        "student_dashboard.html",
        roll_number=username,
        workshop_label=workshop_label,
        checked_at=checked_at,
        posted_attendance=posted_attendance,
        posted_at_display=posted_at_display,
        attendance_percentage=attendance_percentage,
        present_classes=present_classes,
        absent_classes=absent_classes,
        total_classes=total_classes,
        attendance_timeline=attendance_timeline,
    )


@app.route("/students/add", methods=["POST"])
@role_required("hod")
def add_student_hod():
    workshop_key = request.form.get("workshop", "vlsi")
    roll_number = request.form.get("roll_number", "")

    ok, message = add_student_to_workshop(workshop_key, roll_number)
    if ok:
        add_audit_log("STUDENT_ADDED", f"Added {roll_number.strip().upper()} to {workshop_key}.")
    flash(message, "success" if ok else "error")
    return redirect(url_for("hod_dashboard"))


@app.route("/hod/change-password", methods=["POST"])
@role_required("hod")
def change_hod_password():
    current_password = request.form.get("current_password", "")
    new_password = request.form.get("new_password", "")
    confirm_password = request.form.get("confirm_password", "")
    username = normalize_username(session.get("username", ""))

    user = get_staff_user(username)
    if not user or user["role"] != "hod":
        flash("HOD account not found.", "error")
        return redirect(url_for("hod_dashboard"))

    if not check_password_hash(user["password_hash"], current_password):
        flash("Current password is incorrect.", "error")
        return redirect(url_for("hod_dashboard"))

    if new_password != confirm_password:
        flash("New password and confirmation do not match.", "error")
        return redirect(url_for("hod_dashboard"))

    ok, message = update_staff_password(username, new_password)
    if ok:
        add_audit_log("PASSWORD_CHANGED", "HOD changed account password.")
    flash(message, "success" if ok else "error")
    return redirect(url_for("hod_dashboard"))


@app.route("/faculty/add", methods=["POST"])
@role_required("editor")
def add_faculty_account():
    username = request.form.get("faculty_username", "")
    password = request.form.get("faculty_password", "")
    email = request.form.get("faculty_email", "")
    security_answer = request.form.get("faculty_security_answer", "ECE")

    ok, message = create_faculty_user(username, password, email, security_answer)
    if ok:
        add_audit_log("FACULTY_ADDED", f"Created faculty account {normalize_username(username)}.")
    flash(message, "success" if ok else "error")
    return redirect(url_for("hod_dashboard"))


@app.route("/faculty/remove", methods=["POST"])
@role_required("editor")
def remove_faculty_account():
    username = request.form.get("faculty_username", "")
    ok, message = remove_faculty_user(username)
    if ok:
        add_audit_log("FACULTY_REMOVED", f"Removed faculty account {normalize_username(username)}.")
    flash(message, "success" if ok else "error")
    return redirect(url_for("hod_dashboard"))


@app.route("/faculty/reset-password", methods=["POST"])
@role_required("editor")
def reset_faculty_password():
    username = request.form.get("faculty_username", "")
    new_password = request.form.get("new_password", "")
    normalized = normalize_username(username)
    user = get_staff_user(normalized)
    if not user or user["role"] != "faculty":
        flash("Faculty account not found.", "error")
        return redirect(url_for("hod_dashboard"))

    ok, message = update_staff_password(normalized, new_password)
    if ok:
        add_audit_log("FACULTY_PASSWORD_RESET", f"Reset password for faculty {normalized}.")
    flash(message, "success" if ok else "error")
    return redirect(url_for("hod_dashboard"))


@app.route("/students/remove", methods=["POST"])
@role_required("editor")
def remove_student_hod():
    workshop_key = request.form.get("workshop", "vlsi")
    roll_number = request.form.get("roll_number", "")
    ok, message = remove_student_from_workshop(workshop_key, roll_number)
    if ok:
        add_audit_log("STUDENT_REMOVED", f"Removed {roll_number.strip().upper()} from {workshop_key}.")
    flash(message, "success" if ok else "error")
    return redirect(url_for("hod_dashboard"))


@app.route("/students/move", methods=["POST"])
@role_required("editor")
def move_student_hod():
    source_workshop = request.form.get("source_workshop", "vlsi")
    target_workshop = request.form.get("target_workshop", "embedded")
    roll_number = request.form.get("roll_number", "")
    ok, message = move_student_between_workshops(source_workshop, target_workshop, roll_number)
    if ok:
        add_audit_log(
            "STUDENT_MOVED",
            (
                f"Moved {roll_number.strip().upper()} "
                f"from {source_workshop} to {target_workshop}."
            ),
        )
    flash(message, "success" if ok else "error")
    return redirect(url_for("hod_dashboard"))


@app.route("/students/update-roll", methods=["POST"])
@role_required("editor")
def update_student_roll_hod():
    workshop_key = request.form.get("workshop", "vlsi")
    old_roll = request.form.get("old_roll_number", "")
    new_roll = request.form.get("new_roll_number", "")
    ok, message = update_student_roll_number(workshop_key, old_roll, new_roll)
    if ok:
        add_audit_log(
            "STUDENT_ROLL_UPDATED",
            f"Updated roll {old_roll.strip().upper()} to {new_roll.strip().upper()} in {workshop_key}.",
        )
    flash(message, "success" if ok else "error")
    return redirect(url_for("hod_dashboard"))


@app.route("/attendance/<workshop_key>", methods=["GET", "POST"])
@role_required("faculty")
def mark_attendance(workshop_key):
    if workshop_key not in {"vlsi", "embedded"}:
        flash("Invalid workshop selection.", "error")
        return redirect(url_for("faculty_dashboard"))

    students = get_workshop_students(workshop_key)
    selected_date = request.args.get("date", date.today().isoformat())
    selected_session = request.args.get("session", "FN")
    if request.method == "POST":
        selected_date = request.form.get("date", date.today().isoformat())
        selected_session = request.form.get("session", "FN")

    # Guard against invalid date values and fall back to today.
    try:
        datetime.strptime(selected_date, "%Y-%m-%d")
    except ValueError:
        selected_date = date.today().isoformat()

    if selected_session not in SESSION_OPTIONS:
        selected_session = "FN"

    if request.method == "POST":
        existing_records = load_attendance_records()
        already_posted = any(
            record.get("workshop_type") == workshop_key
            and record.get("date") == selected_date
            and (record.get("session") or "FN") == selected_session
            for record in existing_records
        )

        if already_posted:
            flash(
                f"Attendance already marked for {selected_session}.",
                "error",
            )
            return redirect(
                url_for(
                    "mark_attendance",
                    workshop_key=workshop_key,
                    date=selected_date,
                    session=selected_session,
                )
            )

        posted_at = datetime.now().isoformat(timespec="seconds")
        absent_roll_numbers = {
            (roll or "").strip().upper()
            for roll in request.form.getlist("absent_roll_numbers")
            if (roll or "").strip()
        }
        submitted_status = {}
        for roll in students:
            submitted_status[roll] = "Absent" if roll in absent_roll_numbers else "Present"

        filtered_records = list(existing_records)

        for roll, status in submitted_status.items():
            filtered_records.append(
                {
                    "roll_number": roll,
                    "status": status,
                    "date": selected_date,
                    "session": selected_session,
                    "posted_at": posted_at,
                    "workshop_type": workshop_key,
                }
            )

        save_attendance_records(filtered_records)
        add_audit_log(
            "ATTENDANCE_POSTED",
            f"Workshop={workshop_key}, date={selected_date}, session={selected_session}",
        )
        flash(
            (
                f"Attendance saved for {WORKSHOP_LABELS[workshop_key]} on "
                f"{selected_date} ({selected_session})."
            ),
            "success",
        )
        return redirect(
            url_for(
                "mark_attendance",
                workshop_key=workshop_key,
                date=selected_date,
                session=selected_session,
            )
        )

    existing_status_map = {}
    for record in load_attendance_records():
        record_session = record.get("session") or "FN"
        if (
            record.get("workshop_type") == workshop_key
            and record.get("date") == selected_date
            and record_session == selected_session
        ):
            existing_status_map[record.get("roll_number")] = record.get("status")

    return render_template(
        "attendance.html",
        workshop_key=workshop_key,
        workshop_label=WORKSHOP_LABELS[workshop_key],
        students=students,
        existing_status_map=existing_status_map,
        selected_date=selected_date,
        selected_session=selected_session,
        session_options=SESSION_OPTIONS,
        read_only=False,
    )


@app.route("/attendance/<workshop_key>/add-student", methods=["POST"])
@role_required("faculty")
def add_student_faculty(workshop_key):
    if workshop_key not in {"vlsi", "embedded"}:
        flash("Invalid workshop selection.", "error")
        return redirect(url_for("faculty_dashboard"))

    roll_number = request.form.get("roll_number", "")
    selected_date = request.form.get("date", date.today().isoformat())
    selected_session = request.form.get("session", "FN")
    if selected_session not in SESSION_OPTIONS:
        selected_session = "FN"

    try:
        datetime.strptime(selected_date, "%Y-%m-%d")
    except ValueError:
        selected_date = date.today().isoformat()

    ok, message = add_student_to_workshop(workshop_key, roll_number)
    flash(message, "success" if ok else "error")
    return redirect(
        url_for(
            "mark_attendance",
            workshop_key=workshop_key,
            date=selected_date,
            session=selected_session,
        )
    )


@app.route("/students/not-in-workshop")
@role_required("faculty")
def not_in_workshop_students():
    students = get_workshop_students("not_in_workshop")
    return render_template(
        "attendance.html",
        workshop_key="not_in_workshop",
        workshop_label=WORKSHOP_LABELS["not_in_workshop"],
        students=students,
        existing_status_map={},
        read_only=True,
    )


@app.route("/attendance/report", methods=["GET"])
@role_required("hod")
def attendance_report():
    selected_date = request.args.get("date", date.today().isoformat())
    workshop_key = request.args.get("workshop", "vlsi")
    selected_session = request.args.get("session", "FN")
    if workshop_key not in {"vlsi", "embedded"}:
        workshop_key = "vlsi"
    if selected_session not in SESSION_OPTIONS:
        selected_session = "FN"

    report_rows, present_count, absent_count = build_report_rows(
        selected_date,
        workshop_key,
        selected_session,
    )

    return render_template(
        "report.html",
        selected_date=selected_date,
        workshop_key=workshop_key,
        workshop_label=WORKSHOP_LABELS[workshop_key],
        selected_session=selected_session,
        session_options=SESSION_OPTIONS,
        report_rows=report_rows,
        present_count=present_count,
        absent_count=absent_count,
    )


@app.route("/attendance/export", methods=["GET"])
@role_required("hod")
def export_report_csv():
    selected_date = request.args.get("date", date.today().isoformat())
    workshop_key = request.args.get("workshop", "vlsi")
    selected_session = request.args.get("session", "FN")
    if workshop_key not in {"vlsi", "embedded"}:
        workshop_key = "vlsi"
    if selected_session not in SESSION_OPTIONS:
        selected_session = "FN"

    records = [
        record
        for record in load_attendance_records()
        if (
            record.get("date") == selected_date
            and record.get("workshop_type") == workshop_key
            and (record.get("session") or "FN") == selected_session
        )
    ]

    status_map = {record["roll_number"]: record["status"] for record in records}
    all_students = get_workshop_students(workshop_key)

    export_filename = f"attendance_{workshop_key}_{selected_date}_{selected_session}.csv"
    export_path = os.path.join(ATTENDANCE_DIR, export_filename)

    with open(export_path, "w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(["roll_number", "status", "date", "session", "workshop_type"])
        for roll in all_students:
            writer.writerow([
                roll,
                status_map.get(roll, "Absent"),
                selected_date,
                selected_session,
                workshop_key,
            ])

    add_audit_log(
        "REPORT_EXPORTED_CSV",
        f"Workshop={workshop_key}, date={selected_date}, session={selected_session}",
    )

    return send_file(export_path, as_attachment=True, download_name=export_filename)


@app.route("/attendance/export-pdf", methods=["GET"])
@role_required("hod")
def export_report_pdf():
    selected_date = request.args.get("date", date.today().isoformat())
    workshop_key = request.args.get("workshop", "vlsi")
    selected_session = request.args.get("session", "FN")
    if workshop_key not in {"vlsi", "embedded"}:
        workshop_key = "vlsi"
    if selected_session not in SESSION_OPTIONS:
        selected_session = "FN"

    report_rows, present_count, absent_count = build_report_rows(
        selected_date,
        workshop_key,
        selected_session,
    )

    pdf_bytes = build_simple_attendance_pdf(
        selected_date=selected_date,
        workshop_label=WORKSHOP_LABELS[workshop_key],
        selected_session=selected_session,
        report_rows=report_rows,
        present_count=present_count,
        absent_count=absent_count,
        report_title="Complete Report (Present & Absent)",
    )
    file_name = f"attendance_{workshop_key}_{selected_date}_{selected_session}.pdf"
    add_audit_log(
        "REPORT_EXPORTED_PDF",
        f"Workshop={workshop_key}, date={selected_date}, session={selected_session}",
    )
    return Response(
        pdf_bytes,
        mimetype="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={file_name}"},
    )


@app.route("/attendance/export-absentees-pdf", methods=["GET"])
@roles_required("faculty", "hod")
def export_absentees_report_pdf():
    selected_date = request.args.get("date", date.today().isoformat())
    workshop_key = request.args.get("workshop", "vlsi")
    selected_session = request.args.get("session", "FN")
    if workshop_key not in {"vlsi", "embedded"}:
        workshop_key = "vlsi"
    if selected_session not in SESSION_OPTIONS:
        selected_session = "FN"

    report_rows, _, absent_count = build_report_rows(
        selected_date,
        workshop_key,
        selected_session,
    )
    absentees_rows = [row for row in report_rows if row.get("status") == "Absent"]

    pdf_bytes = build_simple_attendance_pdf(
        selected_date=selected_date,
        workshop_label=WORKSHOP_LABELS[workshop_key],
        selected_session=selected_session,
        report_rows=absentees_rows,
        present_count=0,
        absent_count=absent_count,
        report_title="Absentees Report",
    )
    file_name = f"absentees_{workshop_key}_{selected_date}_{selected_session}.pdf"
    return Response(
        pdf_bytes,
        mimetype="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={file_name}"},
    )


@app.route("/attendance/export-presentees-pdf", methods=["GET"])
@roles_required("faculty", "hod")
def export_presentees_report_pdf():
    selected_date = request.args.get("date", date.today().isoformat())
    workshop_key = request.args.get("workshop", "vlsi")
    selected_session = request.args.get("session", "FN")
    if workshop_key not in {"vlsi", "embedded"}:
        workshop_key = "vlsi"
    if selected_session not in SESSION_OPTIONS:
        selected_session = "FN"

    report_rows, present_count, _ = build_report_rows(
        selected_date,
        workshop_key,
        selected_session,
    )
    presentees_rows = [row for row in report_rows if row.get("status") == "Present"]

    pdf_bytes = build_simple_attendance_pdf(
        selected_date=selected_date,
        workshop_label=WORKSHOP_LABELS[workshop_key],
        selected_session=selected_session,
        report_rows=presentees_rows,
        present_count=present_count,
        absent_count=0,
        report_title="Presentees Report",
    )
    file_name = f"presentees_{workshop_key}_{selected_date}_{selected_session}.pdf"
    return Response(
        pdf_bytes,
        mimetype="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={file_name}"},
    )


@app.route("/attendance/update", methods=["POST"])
@role_required("hod")
def update_attendance_record():
    roll_number = request.form.get("roll_number", "").strip()
    selected_date = request.form.get("date", date.today().isoformat())
    workshop_key = request.form.get("workshop", "vlsi")
    selected_session = request.form.get("session", "FN")
    new_status = request.form.get("status", "Absent")

    if workshop_key not in {"vlsi", "embedded"}:
        workshop_key = "vlsi"
    if selected_session not in SESSION_OPTIONS:
        selected_session = "FN"
    if new_status not in {"Present", "Absent"}:
        new_status = "Absent"

    updated = False
    records = load_attendance_records()
    for record in records:
        if (
            record.get("roll_number") == roll_number
            and record.get("date") == selected_date
            and record.get("workshop_type") == workshop_key
            and (record.get("session") or "FN") == selected_session
        ):
            record["status"] = new_status
            record["session"] = selected_session
            updated = True
            break

    if not updated:
        records.append(
            {
                "roll_number": roll_number,
                "status": new_status,
                "date": selected_date,
                "session": selected_session,
                "workshop_type": workshop_key,
            }
        )

    save_attendance_records(records)
    add_audit_log(
        "ATTENDANCE_UPDATED",
        (
            f"Roll={roll_number}, workshop={workshop_key}, date={selected_date}, "
            f"session={selected_session}, status={new_status}"
        ),
    )
    flash(f"Attendance updated for {roll_number}.", "success")
    return redirect(
        url_for(
            "attendance_report",
            date=selected_date,
            workshop=workshop_key,
            session=selected_session,
        )
    )


@app.route("/attendance/delete", methods=["POST"])
@role_required("hod")
def delete_attendance_record():
    roll_number = request.form.get("roll_number", "").strip()
    selected_date = request.form.get("date", date.today().isoformat())
    workshop_key = request.form.get("workshop", "vlsi")
    selected_session = request.form.get("session", "FN")

    if workshop_key not in {"vlsi", "embedded"}:
        workshop_key = "vlsi"
    if selected_session not in SESSION_OPTIONS:
        selected_session = "FN"

    records = load_attendance_records()
    filtered_records = [
        record
        for record in records
        if not (
            record.get("roll_number") == roll_number
            and record.get("date") == selected_date
            and record.get("workshop_type") == workshop_key
            and (record.get("session") or "FN") == selected_session
        )
    ]

    if len(filtered_records) != len(records):
        save_attendance_records(filtered_records)
        add_audit_log(
            "ATTENDANCE_DELETED",
            (
                f"Roll={roll_number}, workshop={workshop_key}, date={selected_date}, "
                f"session={selected_session}"
            ),
        )
        flash(f"Attendance deleted for {roll_number}.", "success")
    else:
        flash("No matching attendance record found to delete.", "error")

    return redirect(
        url_for(
            "attendance_report",
            date=selected_date,
            workshop=workshop_key,
            session=selected_session,
        )
    )


@app.route("/attendance/delete-batch", methods=["POST"])
@role_required("hod")
def delete_attendance_batch():
    selected_date = request.form.get("date", date.today().isoformat())
    workshop_key = request.form.get("workshop", "vlsi")
    selected_session = request.form.get("session", "FN")

    if workshop_key not in {"vlsi", "embedded"}:
        workshop_key = "vlsi"
    if selected_session not in SESSION_OPTIONS:
        selected_session = "FN"

    records = load_attendance_records()
    filtered_records = [
        record
        for record in records
        if not (
            record.get("date") == selected_date
            and record.get("workshop_type") == workshop_key
            and (record.get("session") or "FN") == selected_session
        )
    ]

    deleted_count = len(records) - len(filtered_records)
    if deleted_count > 0:
        save_attendance_records(filtered_records)
        add_audit_log(
            "ATTENDANCE_BATCH_DELETED",
            (
                f"Deleted={deleted_count}, workshop={workshop_key}, "
                f"date={selected_date}, session={selected_session}"
            ),
        )
        flash(
            (
                f"Deleted {deleted_count} records for {WORKSHOP_LABELS[workshop_key]} "
                f"on {selected_date} ({selected_session})."
            ),
            "success",
        )
    else:
        flash("No attendance records found for the selected batch.", "error")

    return redirect(
        url_for(
            "attendance_report",
            date=selected_date,
            workshop=workshop_key,
            session=selected_session,
        )
    )


@app.route("/samples/<workshop_key>")
@role_required("hod")
def download_sample_csv(workshop_key):
    if workshop_key not in WORKSHOP_FILES:
        flash("Invalid sample CSV request.", "error")
        return redirect(url_for("hod_dashboard"))

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["roll_number"])
    for roll in SAMPLE_ROLL_NUMBERS.get(workshop_key, []):
        writer.writerow([roll])

    csv_data = output.getvalue()
    output.close()

    filename = f"sample_{WORKSHOP_FILES[workshop_key]}"
    return Response(
        csv_data,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
