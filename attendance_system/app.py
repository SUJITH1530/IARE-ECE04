import csv
import io
import os
import sqlite3
from datetime import date, datetime
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
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "ece_workshop_secret_key_change_me")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STUDENTS_DIR = os.path.join(BASE_DIR, "students")
ATTENDANCE_DIR = os.path.join(BASE_DIR, "attendance")
ATTENDANCE_FILE = os.path.join(ATTENDANCE_DIR, "attendance_records.csv")
DATABASE_PATH = os.path.join(BASE_DIR, "attendance.db")
SESSION_OPTIONS = ("FN", "AN")

USERS = {
    "ecehod": {"password": "ece@04", "role": "hod"},
    "faculty": {"password": "iare@1234", "role": "faculty"},
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

        conn.commit()
    finally:
        conn.close()


def migrate_csv_attendance_to_db_if_needed() -> None:
    if not os.path.exists(ATTENDANCE_FILE):
        return

    conn = get_db_connection()
    try:
        current_count = conn.execute("SELECT COUNT(*) FROM attendance_records").fetchone()[0]
        if current_count > 0:
            return

        with open(ATTENDANCE_FILE, "r", newline="", encoding="utf-8") as file:
            reader = csv.DictReader(file)
            rows = list(reader)

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


ensure_directories_and_files()
ensure_database()
migrate_csv_attendance_to_db_if_needed()


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


def save_uploaded_student_file(file_storage, workshop_key: str) -> None:
    filename = WORKSHOP_FILES[workshop_key]
    target_path = os.path.join(STUDENTS_DIR, filename)

    temp_name = secure_filename(file_storage.filename)
    temp_path = os.path.join(STUDENTS_DIR, f"temp_{temp_name}")
    file_storage.save(temp_path)

    roll_numbers = read_roll_numbers_from_csv(temp_path)

    with open(target_path, "w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(["roll_number"])
        for roll in roll_numbers:
            writer.writerow([roll])

    os.remove(temp_path)


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
    return read_roll_numbers_from_csv(file_path)


def build_users() -> dict:
    users = dict(USERS)
    for workshop_key in WORKSHOP_FILES:
        for roll_number in get_workshop_students(workshop_key):
            normalized_roll = roll_number.strip().upper()
            if normalized_roll:
                users.setdefault(
                    normalized_roll,
                    {"password": STUDENT_COMMON_PASSWORD, "role": "student"},
                )
    return users


def add_student_to_workshop(workshop_key: str, roll_number: str) -> tuple[bool, str]:
    if workshop_key not in WORKSHOP_FILES:
        return False, "Invalid domain selected."

    clean_roll = roll_number.strip().upper()
    if not clean_roll:
        return False, "Roll number is required."

    students = get_workshop_students(workshop_key)
    if clean_roll in students:
        return False, f"{clean_roll} already exists in {WORKSHOP_LABELS[workshop_key]}."

    students.append(clean_roll)
    file_path = os.path.join(STUDENTS_DIR, WORKSHOP_FILES[workshop_key])
    with open(file_path, "w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(["roll_number"])
        for roll in students:
            writer.writerow([roll])

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
    finally:
        conn.close()


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


@app.route("/", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        all_users = build_users()
        user = all_users.get(username)
        if not user:
            user = all_users.get(username.upper())
        if not user:
            user = all_users.get(username.lower())

        if user and user["password"] == password:
            session["username"] = username
            session["role"] = user["role"]
            flash("Login successful.", "success")
            if user["role"] == "hod":
                return redirect(url_for("hod_dashboard"))
            if user["role"] == "student":
                return redirect(url_for("student_dashboard"))
            return redirect(url_for("faculty_dashboard"))

        flash("Invalid username or password.", "error")

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("Logged out successfully.", "success")
    return redirect(url_for("login"))


@app.route("/hod/dashboard", methods=["GET", "POST"])
@role_required("hod")
def hod_dashboard():
    if request.method == "POST":
        vlsi_file = request.files.get("vlsi_file")
        embedded_file = request.files.get("embedded_file")
        not_in_workshop_file = request.files.get("not_in_workshop_file")

        try:
            if vlsi_file and vlsi_file.filename:
                save_uploaded_student_file(vlsi_file, "vlsi")
            if embedded_file and embedded_file.filename:
                save_uploaded_student_file(embedded_file, "embedded")
            if not_in_workshop_file and not_in_workshop_file.filename:
                save_uploaded_student_file(not_in_workshop_file, "not_in_workshop")
            flash("Student CSV files uploaded successfully.", "success")
        except Exception as exc:
            flash(f"Error while uploading CSV files: {exc}", "error")

    counts = {
        "vlsi": len(get_workshop_students("vlsi")),
        "embedded": len(get_workshop_students("embedded")),
        "not_in_workshop": len(get_workshop_students("not_in_workshop")),
    }
    return render_template("hod_dashboard.html", counts=counts)


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
        if total_classes > 0:
            attendance_percentage = round((present_classes / total_classes) * 100, 2)

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
        total_classes=total_classes,
    )


@app.route("/students/add", methods=["POST"])
@role_required("hod")
def add_student_hod():
    workshop_key = request.form.get("workshop", "vlsi")
    roll_number = request.form.get("roll_number", "")

    ok, message = add_student_to_workshop(workshop_key, roll_number)
    flash(message, "success" if ok else "error")
    return redirect(url_for("hod_dashboard"))


@app.route("/attendance/<workshop_key>", methods=["GET", "POST"])
@role_required("faculty")
def mark_attendance(workshop_key):
    if workshop_key not in {"vlsi", "embedded"}:
        flash("Invalid workshop selection.", "error")
        return redirect(url_for("faculty_dashboard"))

    students = get_workshop_students(workshop_key)
    current_date = date.today().isoformat()
    selected_session = request.args.get("session", "FN")
    if request.method == "POST":
        selected_session = request.form.get("session", "FN")
    if selected_session not in SESSION_OPTIONS:
        selected_session = "FN"

    if request.method == "POST":
        posted_at = datetime.now().isoformat(timespec="seconds")
        submitted_status = {}
        for roll in students:
            status = request.form.get(f"status_{roll}", "Absent")
            submitted_status[roll] = "Present" if status == "Present" else "Absent"

        existing_records = load_attendance_records()
        # Remove previous entries for this workshop and date to prevent duplicates.
        filtered_records = [
            record
            for record in existing_records
            if not (
                record.get("workshop_type") == workshop_key
                and record.get("date") == current_date
                and (record.get("session") or "FN") == selected_session
            )
        ]

        for roll, status in submitted_status.items():
            filtered_records.append(
                {
                    "roll_number": roll,
                    "status": status,
                    "date": current_date,
                    "session": selected_session,
                    "posted_at": posted_at,
                    "workshop_type": workshop_key,
                }
            )

        save_attendance_records(filtered_records)
        flash(
            (
                f"Attendance saved for {WORKSHOP_LABELS[workshop_key]} on "
                f"{current_date} ({selected_session})."
            ),
            "success",
        )
        return redirect(
            url_for(
                "mark_attendance",
                workshop_key=workshop_key,
                session=selected_session,
            )
        )

    existing_status_map = {}
    for record in load_attendance_records():
        record_session = record.get("session") or "FN"
        if (
            record.get("workshop_type") == workshop_key
            and record.get("date") == current_date
            and record_session == selected_session
        ):
            existing_status_map[record.get("roll_number")] = record.get("status")

    return render_template(
        "attendance.html",
        workshop_key=workshop_key,
        workshop_label=WORKSHOP_LABELS[workshop_key],
        students=students,
        existing_status_map=existing_status_map,
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
    selected_session = request.form.get("session", "FN")
    if selected_session not in SESSION_OPTIONS:
        selected_session = "FN"

    ok, message = add_student_to_workshop(workshop_key, roll_number)
    flash(message, "success" if ok else "error")
    return redirect(
        url_for("mark_attendance", workshop_key=workshop_key, session=selected_session)
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

    return send_file(export_path, as_attachment=True, download_name=export_filename)


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
