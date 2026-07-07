"""
database.py
-------------------------------------------------
StudySync data layer.

Handles SQLite connection, schema creation, and small
helper functions used by app.py. Kept dependency-free
(stdlib sqlite3 only) so the project runs with nothing
more than `pip install -r requirements.txt`.
-------------------------------------------------
"""

import sqlite3
import os
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "studysync.db")


def get_db():
    """Return a sqlite3 connection with row access by column name."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    """Create all tables if they do not already exist, and seed a default user."""
    conn = get_db()
    cur = conn.cursor()

    # ---------------- Users ----------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL DEFAULT 'Student',
            email TEXT UNIQUE,
            password_hash TEXT,
            role TEXT NOT NULL DEFAULT 'student',   -- student, parent, (teacher/admin future-ready)
            invite_code TEXT UNIQUE,                -- student's code for parents to link with
            google_id TEXT,
            is_active INTEGER DEFAULT 1,
            theme TEXT DEFAULT 'dark',
            reminder_settings TEXT DEFAULT '{}',
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)

    # Additive migration for DBs created before auth existed.
    existing_cols = {row["name"] for row in cur.execute("PRAGMA table_info(users)").fetchall()}
    migrations = {
        "password_hash": "ALTER TABLE users ADD COLUMN password_hash TEXT",
        "role": "ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'student'",
        "invite_code": "ALTER TABLE users ADD COLUMN invite_code TEXT",
        "is_active": "ALTER TABLE users ADD COLUMN is_active INTEGER DEFAULT 1",
    }
    for col, ddl in migrations.items():
        if col not in existing_cols:
            cur.execute(ddl)

    # ---------------- Parent <-> Student links ----------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS parent_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            parent_id INTEGER NOT NULL,
            student_id INTEGER NOT NULL,
            status TEXT DEFAULT 'approved',
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (parent_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (student_id) REFERENCES users(id) ON DELETE CASCADE,
            UNIQUE(parent_id, student_id)
        )
    """)

    # ---------------- Subjects ----------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS subjects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            subject_name TEXT NOT NULL,
            instructor TEXT,
            room TEXT,
            color TEXT DEFAULT '#7C6FF0',
            attendance_percentage REAL DEFAULT 100,
            classes_attended INTEGER DEFAULT 0,
            classes_missed INTEGER DEFAULT 0,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)

    # ---------------- Schedule (timetable) ----------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS schedule (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            type TEXT DEFAULT 'Class',        -- Class, Study Session, Break, Event
            subject_id INTEGER,
            day TEXT NOT NULL,                -- Monday..Sunday
            start_time TEXT NOT NULL,         -- HH:MM
            end_time TEXT NOT NULL,           -- HH:MM
            recurring INTEGER DEFAULT 1,
            off_day INTEGER DEFAULT 0,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (subject_id) REFERENCES subjects(id) ON DELETE SET NULL
        )
    """)

    # ---------------- Tasks (assignments / quizzes / exams) ----------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            subject TEXT,
            title TEXT NOT NULL,
            type TEXT DEFAULT 'Assignment',   -- Assignment, Quiz, Exam, Lab, Presentation
            due_date TEXT,
            due_time TEXT,
            estimated_hours REAL DEFAULT 1,
            priority TEXT DEFAULT 'Medium',   -- Low, Medium, High, Urgent
            progress INTEGER DEFAULT 0,
            status TEXT DEFAULT 'Not Started',-- Not Started, In Progress, Completed, Submitted, Late, Missing
            notes TEXT,
            completed INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)

    # ---------------- Off days ----------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS off_days (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            day TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            UNIQUE(user_id, day)
        )
    """)

    # ---------------- Settings ----------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            user_id INTEGER PRIMARY KEY,
            wakeup_offset INTEGER DEFAULT 90,   -- minutes before class
            leave_offset INTEGER DEFAULT 30,    -- minutes before class
            reminder_offsets TEXT DEFAULT '[10,60,1440]', -- minutes before, JSON list
            dark_mode INTEGER DEFAULT 1,
            daily_goal_hours REAL DEFAULT 3,
            weekly_goal_hours REAL DEFAULT 20,
            attendance_goal REAL DEFAULT 85,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)

    # ---------------- Notifications ----------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            body TEXT,
            category TEXT DEFAULT 'general',
            is_read INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)

    # ---------------- Notes ----------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            body TEXT,
            link TEXT,
            subject_id INTEGER,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)

    # ---------------- Productivity log (for streaks / analytics) ----------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS productivity_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            log_date TEXT NOT NULL,
            study_minutes INTEGER DEFAULT 0,
            tasks_completed INTEGER DEFAULT 0,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            UNIQUE(user_id, log_date)
        )
    """)

    # ---------------- Goals (daily/weekly/monthly/semester/gpa/attendance) ----
    cur.execute("""
        CREATE TABLE IF NOT EXISTS goals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            goal_type TEXT NOT NULL,        -- daily, weekly, monthly, semester, gpa, attendance
            title TEXT NOT NULL,
            target_value REAL NOT NULL,     -- hours, %, or GPA points depending on goal_type
            period_start TEXT,              -- YYYY-MM-DD, optional
            period_end TEXT,                -- YYYY-MM-DD, optional
            status TEXT DEFAULT 'active',   -- active, completed, missed
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)

    # ---------------- Grades (for GPA tracking) --------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS grades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            subject_id INTEGER NOT NULL,
            title TEXT NOT NULL,            -- e.g. "Midterm", "Homework 3"
            marks_obtained REAL NOT NULL,
            marks_total REAL NOT NULL,
            weight REAL DEFAULT 1.0,        -- relative weight toward the subject's final grade
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (subject_id) REFERENCES subjects(id) ON DELETE CASCADE
        )
    """)

    # Additive migration: subjects need credit_hours + a per-subject target grade
    # for the GPA calculator, without breaking existing rows.
    existing_subject_cols = {row["name"] for row in cur.execute("PRAGMA table_info(subjects)").fetchall()}
    subject_migrations = {
        "credit_hours": "ALTER TABLE subjects ADD COLUMN credit_hours REAL DEFAULT 3",
        "target_grade_percent": "ALTER TABLE subjects ADD COLUMN target_grade_percent REAL DEFAULT 90",
    }
    for col, ddl in subject_migrations.items():
        if col not in existing_subject_cols:
            cur.execute(ddl)

    # ---------------- Community / leaderboard opt-in ---------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS leaderboard_optin (
            user_id INTEGER PRIMARY KEY,
            display_name TEXT,
            opted_in INTEGER DEFAULT 0,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)

    conn.commit()
    conn.close()


def create_default_settings(user_id):
    """Insert a settings row for a newly registered user."""
    conn = get_db()
    conn.execute("INSERT OR IGNORE INTO settings (user_id) VALUES (?)", (user_id,))
    conn.commit()
    conn.close()


def row_to_dict(row):
    """Convert a sqlite3.Row (or None) into a plain dict (or None)."""
    if row is None:
        return None
    return dict(row)


def rows_to_list(rows):
    """Convert a list of sqlite3.Row into a list of plain dicts."""
    return [dict(r) for r in rows]


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# GPA helpers — standard 4.0-scale percentage -> letter grade -> grade points.
# Shared between the /api/gpa routes and the GPA calculator.
# ---------------------------------------------------------------------------
GPA_SCALE = [
    (93, "A", 4.0), (90, "A-", 3.7),
    (87, "B+", 3.3), (83, "B", 3.0), (80, "B-", 2.7),
    (77, "C+", 2.3), (73, "C", 2.0), (70, "C-", 1.7),
    (67, "D+", 1.3), (63, "D", 1.0), (60, "D-", 0.7),
    (0, "F", 0.0),
]


def percent_to_letter_and_points(pct):
    """Map a percentage (0-100) to (letter_grade, gpa_points) on a 4.0 scale."""
    if pct is None:
        return None, None
    for threshold, letter, points in GPA_SCALE:
        if pct >= threshold:
            return letter, points
    return "F", 0.0
