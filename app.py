"""
app.py
-------------------------------------------------
StudySync — Flask backend.

Multi-user app with real authentication (email/password + Google
OAuth), student and parent roles, and session-backed access control.
Every /api/* route (other than the auth routes themselves) requires
login; parent accounts get read-only access to a linked student's
data via ?student_id=. See auth.py for the implementation.

Google Classroom *data* sync still requires the Classroom API scopes
and is not wired up yet — see /api/google-sync for details. Google
*login* (auth/google/*) is fully implemented.
-------------------------------------------------
"""

import os
import json
import secrets
from datetime import datetime, timedelta

from flask import Flask, render_template, request, jsonify, session, redirect, url_for, g
from dotenv import load_dotenv

import database as db
import ai
import auth

load_dotenv()

app = Flask(__name__)
app.secret_key = auth.SECRET_KEY

# Session / cookie security
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("FLASK_ENV") == "production",
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),  # used when "remember me" is checked
)

auth.init_oauth(app)

DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
PRIORITY_RANK = {"Urgent": 0, "High": 1, "Medium": 2, "Low": 3}


# =========================================================
# APP INITIALIZATION
# =========================================================
db.init_db()


# =========================================================
# HELPERS
# =========================================================
def parse_time(t):
    """'HH:MM' -> minutes since midnight (int). Returns None on bad input."""
    try:
        h, m = t.split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return None


def minutes_to_hhmm(mins):
    mins = mins % (24 * 60)
    return f"{mins // 60:02d}:{mins % 60:02d}"


def today_name():
    return DAYS[datetime.now().weekday()]


def get_off_days(conn):
    rows = conn.execute("SELECT day FROM off_days WHERE user_id = ?", (g.user_id,)).fetchall()
    return {r["day"] for r in rows}


def get_free_slots_for_day(conn, day, day_start="07:00", day_end="23:00"):
    """
    Compute free time gaps for a given day based on the schedule table.
    Returns a list of {"start_time","end_time"} dicts.
    """
    events = conn.execute(
        "SELECT start_time, end_time FROM schedule WHERE user_id = ? AND day = ? ORDER BY start_time",
        (g.user_id, day)
    ).fetchall()

    busy = sorted([(parse_time(e["start_time"]), parse_time(e["end_time"])) for e in events])
    cursor = parse_time(day_start)
    end_of_day = parse_time(day_end)
    free = []

    for start, end in busy:
        if start is None or end is None:
            continue
        if start > cursor:
            free.append({"start_time": minutes_to_hhmm(cursor), "end_time": minutes_to_hhmm(start)})
        cursor = max(cursor, end)

    if cursor < end_of_day:
        free.append({"start_time": minutes_to_hhmm(cursor), "end_time": minutes_to_hhmm(end_of_day)})

    return free


def detect_conflicts(conn, day, start_time, end_time, exclude_id=None):
    """Return overlapping schedule rows for a given day/time range."""
    s, e = parse_time(start_time), parse_time(end_time)
    rows = conn.execute(
        "SELECT * FROM schedule WHERE user_id = ? AND day = ?", (g.user_id, day)
    ).fetchall()
    conflicts = []
    for r in rows:
        if exclude_id is not None and r["id"] == exclude_id:
            continue
        rs, re = parse_time(r["start_time"]), parse_time(r["end_time"])
        if rs is None or re is None:
            continue
        if s < re and rs < e:  # overlap test
            conflicts.append(dict(r))
    return conflicts


def push_notification(conn, title, body, category="general"):
    conn.execute(
        "INSERT INTO notifications (user_id, title, body, category) VALUES (?,?,?,?)",
        (g.user_id, title, body, category)
    )
    conn.commit()


def compute_alarms_for_day(conn, day):
    """
    For every Class on `day`, compute wake-up / leave-home / class-reminder
    alarm times based on the user's settings offsets.
    """
    settings = conn.execute("SELECT * FROM settings WHERE user_id = ?", (g.user_id,)).fetchone()
    wakeup_offset = settings["wakeup_offset"] if settings else 90
    leave_offset = settings["leave_offset"] if settings else 30

    classes = conn.execute(
        "SELECT * FROM schedule WHERE user_id = ? AND day = ? AND type = 'Class' ORDER BY start_time",
        (g.user_id, day)
    ).fetchall()

    if not classes:
        return []

    first_class_start = parse_time(classes[0]["start_time"])
    alarms = []
    if first_class_start is not None:
        alarms.append({"label": "Wake Up", "time": minutes_to_hhmm(first_class_start - wakeup_offset)})

    for c in classes:
        start = parse_time(c["start_time"])
        if start is None:
            continue
        alarms.append({"label": f"Leave Home — {c['title']}", "time": minutes_to_hhmm(start - leave_offset)})
        alarms.append({"label": f"Class Reminder — {c['title']}", "time": minutes_to_hhmm(start - 10)})

    return sorted(alarms, key=lambda a: a["time"])


# =========================================================
# PAGE ROUTE
# =========================================================
@app.route("/")
def index():
    if not auth.current_user():
        return redirect(url_for("login_page"))
    return render_template("index.html")


@app.route("/login")
def login_page():
    if auth.current_user():
        return redirect(url_for("index"))
    return render_template("login.html", google_configured=auth.google_oauth_configured())


@app.route("/register")
def register_page():
    if auth.current_user():
        return redirect(url_for("index"))
    return render_template("register.html", google_configured=auth.google_oauth_configured())


@app.route("/reset-password")
def reset_password_page():
    return render_template("reset_password.html", token=request.args.get("token", ""))


# =========================================================
# DASHBOARD
# =========================================================
@app.route("/api/dashboard", methods=["GET"])
@auth.login_required
@auth.data_access
def api_dashboard():
    conn = db.get_db()
    day = today_name()
    off_days = get_off_days(conn)
    is_off = day in off_days

    todays_classes = db.rows_to_list(conn.execute(
        "SELECT s.*, sub.color as subject_color FROM schedule s "
        "LEFT JOIN subjects sub ON s.subject_id = sub.id "
        "WHERE s.user_id = ? AND s.day = ? ORDER BY s.start_time",
        (g.user_id, day)
    ).fetchall())

    todays_tasks = db.rows_to_list(conn.execute(
        "SELECT * FROM tasks WHERE user_id = ? AND due_date = ? AND completed = 0 ORDER BY due_time",
        (g.user_id, datetime.now().strftime("%Y-%m-%d"))
    ).fetchall())

    upcoming_quizzes = db.rows_to_list(conn.execute(
        "SELECT * FROM tasks WHERE user_id = ? AND type IN ('Quiz','Exam') AND completed = 0 "
        "AND due_date >= ? ORDER BY due_date LIMIT 5",
        (g.user_id, datetime.now().strftime("%Y-%m-%d"))
    ).fetchall())

    attendance_warnings = db.rows_to_list(conn.execute(
        "SELECT * FROM subjects WHERE user_id = ? AND attendance_percentage < 80", (g.user_id,)
    ).fetchall())

    all_tasks = db.rows_to_list(conn.execute(
        "SELECT * FROM tasks WHERE user_id = ? AND completed = 0 ORDER BY due_date", (g.user_id,)
    ).fetchall())
    next_task = all_tasks[0] if all_tasks else None

    now_str = datetime.now().strftime("%H:%M")
    next_class = next((c for c in todays_classes if c["start_time"] >= now_str), None)

    notifications = db.rows_to_list(conn.execute(
        "SELECT * FROM notifications WHERE user_id = ? ORDER BY created_at DESC LIMIT 8", (g.user_id,)
    ).fetchall())

    today_log = conn.execute(
        "SELECT * FROM productivity_log WHERE user_id = ? AND log_date = ?",
        (g.user_id, datetime.now().strftime("%Y-%m-%d"))
    ).fetchone()

    completed_today = conn.execute(
        "SELECT COUNT(*) c FROM tasks WHERE user_id = ? AND completed = 1 AND due_date = ?",
        (g.user_id, datetime.now().strftime("%Y-%m-%d"))
    ).fetchone()["c"]

    message = ai.generate_encouraging_message({
        "tasks_due_today": len(todays_tasks),
        "classes_today": len(todays_classes),
        "off_day": is_off
    })

    conn.close()
    return jsonify({
        "day": day,
        "is_off_day": is_off,
        "date": datetime.now().strftime("%A, %B %d, %Y"),
        "time": datetime.now().strftime("%H:%M"),
        "todays_classes": todays_classes,
        "todays_tasks": todays_tasks,
        "upcoming_quizzes": upcoming_quizzes,
        "attendance_warnings": attendance_warnings,
        "next_class": next_class,
        "next_task": next_task,
        "notifications": notifications,
        "study_minutes_today": today_log["study_minutes"] if today_log else 0,
        "tasks_completed_today": completed_today,
        "motivational_message": message,
    })


# =========================================================
# TIMETABLE (schedule)
# =========================================================
@app.route("/api/timetable", methods=["GET"])
@auth.login_required
@auth.data_access
def get_timetable():
    conn = db.get_db()
    rows = db.rows_to_list(conn.execute(
        "SELECT s.*, sub.subject_name, sub.color as subject_color, sub.instructor, sub.room "
        "FROM schedule s LEFT JOIN subjects sub ON s.subject_id = sub.id "
        "WHERE s.user_id = ? ORDER BY s.day, s.start_time",
        (g.user_id,)
    ).fetchall())
    off_days = list(get_off_days(conn))
    conn.close()
    return jsonify({"schedule": rows, "off_days": off_days})


@app.route("/api/timetable", methods=["POST"])
@auth.login_required
@auth.data_access
@auth.csrf_protect
def create_timetable_entry():
    data = request.get_json(force=True)
    conn = db.get_db()

    conflicts = detect_conflicts(conn, data["day"], data["start_time"], data["end_time"])
    if conflicts and not data.get("force"):
        conn.close()
        return jsonify({"error": "conflict", "conflicts": conflicts}), 409

    cur = conn.execute(
        "INSERT INTO schedule (user_id, title, type, subject_id, day, start_time, end_time, recurring, off_day) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (g.user_id, data["title"], data.get("type", "Class"), data.get("subject_id"),
         data["day"], data["start_time"], data["end_time"], int(data.get("recurring", 1)),
         int(data.get("off_day", 0)))
    )
    conn.commit()
    new_id = cur.lastrowid
    push_notification(conn, "Timetable updated", f"{data['title']} added on {data['day']}", "schedule")
    conn.close()
    return jsonify({"id": new_id, "status": "created"}), 201


@app.route("/api/timetable/<int:entry_id>", methods=["PUT"])
@auth.login_required
@auth.data_access
@auth.csrf_protect
def update_timetable_entry(entry_id):
    data = request.get_json(force=True)
    conn = db.get_db()

    if "start_time" in data and "end_time" in data and "day" in data:
        conflicts = detect_conflicts(conn, data["day"], data["start_time"], data["end_time"], exclude_id=entry_id)
        if conflicts and not data.get("force"):
            conn.close()
            return jsonify({"error": "conflict", "conflicts": conflicts}), 409

    fields = ["title", "type", "subject_id", "day", "start_time", "end_time", "recurring", "off_day"]
    updates = {k: data[k] for k in fields if k in data}
    if updates:
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        conn.execute(
            f"UPDATE schedule SET {set_clause} WHERE id = ? AND user_id = ?",
            (*updates.values(), entry_id, g.user_id)
        )
        conn.commit()
    conn.close()
    return jsonify({"status": "updated"})


@app.route("/api/timetable/<int:entry_id>", methods=["DELETE"])
@auth.login_required
@auth.data_access
@auth.csrf_protect
def delete_timetable_entry(entry_id):
    conn = db.get_db()
    conn.execute("DELETE FROM schedule WHERE id = ? AND user_id = ?", (entry_id, g.user_id))
    conn.commit()
    conn.close()
    return jsonify({"status": "deleted"})


@app.route("/api/timetable/<int:entry_id>/duplicate", methods=["POST"])
@auth.login_required
@auth.data_access
@auth.csrf_protect
def duplicate_timetable_entry(entry_id):
    conn = db.get_db()
    row = conn.execute("SELECT * FROM schedule WHERE id = ? AND user_id = ?", (entry_id, g.user_id)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "not found"}), 404
    cur = conn.execute(
        "INSERT INTO schedule (user_id, title, type, subject_id, day, start_time, end_time, recurring, off_day) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (g.user_id, row["title"] + " (copy)", row["type"], row["subject_id"], row["day"],
         row["start_time"], row["end_time"], row["recurring"], row["off_day"])
    )
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return jsonify({"id": new_id, "status": "duplicated"}), 201


@app.route("/api/offdays", methods=["POST"])
@auth.login_required
@auth.data_access
@auth.csrf_protect
def toggle_offday():
    data = request.get_json(force=True)
    day = data["day"]
    conn = db.get_db()
    existing = conn.execute(
        "SELECT id FROM off_days WHERE user_id = ? AND day = ?", (g.user_id, day)
    ).fetchone()
    if existing:
        conn.execute("DELETE FROM off_days WHERE id = ?", (existing["id"],))
        result = "removed"
    else:
        conn.execute("INSERT INTO off_days (user_id, day) VALUES (?,?)", (g.user_id, day))
        result = "added"
    conn.commit()
    conn.close()
    return jsonify({"status": result})


# =========================================================
# SUBJECTS
# =========================================================
@app.route("/api/subjects", methods=["GET"])
@auth.login_required
@auth.data_access
def get_subjects():
    conn = db.get_db()
    rows = db.rows_to_list(conn.execute("SELECT * FROM subjects WHERE user_id = ?", (g.user_id,)).fetchall())
    conn.close()
    return jsonify(rows)


@app.route("/api/subjects", methods=["POST"])
@auth.login_required
@auth.data_access
@auth.csrf_protect
def create_subject():
    data = request.get_json(force=True)
    conn = db.get_db()
    cur = conn.execute(
        "INSERT INTO subjects (user_id, subject_name, instructor, room, color, credit_hours, target_grade_percent) "
        "VALUES (?,?,?,?,?,?,?)",
        (g.user_id, data["subject_name"], data.get("instructor", ""), data.get("room", ""),
         data.get("color", "#7C6FF0"), float(data.get("credit_hours", 3)),
         float(data.get("target_grade_percent", 90)))
    )
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return jsonify({"id": new_id, "status": "created"}), 201


@app.route("/api/subjects/<int:subject_id>", methods=["PUT"])
@auth.login_required
@auth.data_access
@auth.csrf_protect
def update_subject(subject_id):
    data = request.get_json(force=True)
    fields = ["subject_name", "instructor", "room", "color", "credit_hours", "target_grade_percent"]
    updates = {k: data[k] for k in fields if k in data}
    conn = db.get_db()
    if updates:
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        conn.execute(f"UPDATE subjects SET {set_clause} WHERE id = ? AND user_id = ?",
                     (*updates.values(), subject_id, g.user_id))
        conn.commit()
    conn.close()
    return jsonify({"status": "updated"})


@app.route("/api/subjects/<int:subject_id>", methods=["DELETE"])
@auth.login_required
@auth.data_access
@auth.csrf_protect
def delete_subject(subject_id):
    conn = db.get_db()
    conn.execute("DELETE FROM subjects WHERE id = ? AND user_id = ?", (subject_id, g.user_id))
    conn.commit()
    conn.close()
    return jsonify({"status": "deleted"})


# =========================================================
# TASKS (assignments, quizzes, exams...)
# =========================================================
@app.route("/api/tasks", methods=["GET"])
@auth.login_required
@auth.data_access
def get_tasks():
    conn = db.get_db()
    rows = db.rows_to_list(conn.execute(
        "SELECT * FROM tasks WHERE user_id = ? ORDER BY completed, due_date, due_time", (g.user_id,)
    ).fetchall())
    conn.close()
    return jsonify(rows)


@app.route("/api/tasks", methods=["POST"])
@auth.login_required
@auth.data_access
@auth.csrf_protect
def create_task():
    data = request.get_json(force=True)
    conn = db.get_db()
    cur = conn.execute(
        "INSERT INTO tasks (user_id, subject, title, type, due_date, due_time, estimated_hours, "
        "priority, progress, status, notes) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (g.user_id, data.get("subject", ""), data["title"], data.get("type", "Assignment"),
         data.get("due_date"), data.get("due_time"), data.get("estimated_hours", 1),
         data.get("priority", "Medium"), data.get("progress", 0),
         data.get("status", "Not Started"), data.get("notes", ""))
    )
    conn.commit()
    new_id = cur.lastrowid
    push_notification(conn, "New task added", f"{data['title']} due {data.get('due_date', 'soon')}", "tasks")
    conn.close()
    return jsonify({"id": new_id, "status": "created"}), 201


@app.route("/api/tasks/<int:task_id>", methods=["PUT"])
@auth.login_required
@auth.data_access
@auth.csrf_protect
def update_task(task_id):
    data = request.get_json(force=True)
    fields = ["subject", "title", "type", "due_date", "due_time", "estimated_hours",
              "priority", "progress", "status", "notes", "completed"]
    updates = {k: data[k] for k in fields if k in data}
    conn = db.get_db()
    if updates:
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        conn.execute(f"UPDATE tasks SET {set_clause} WHERE id = ? AND user_id = ?",
                     (*updates.values(), task_id, g.user_id))
        conn.commit()

        if updates.get("completed") == 1 or updates.get("status") == "Completed":
            log_date = datetime.now().strftime("%Y-%m-%d")
            existing = conn.execute(
                "SELECT * FROM productivity_log WHERE user_id = ? AND log_date = ?", (g.user_id, log_date)
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE productivity_log SET tasks_completed = tasks_completed + 1 WHERE user_id=? AND log_date=?",
                    (g.user_id, log_date)
                )
            else:
                conn.execute(
                    "INSERT INTO productivity_log (user_id, log_date, tasks_completed) VALUES (?,?,1)",
                    (g.user_id, log_date)
                )
            conn.commit()
    conn.close()
    return jsonify({"status": "updated"})


@app.route("/api/tasks/<int:task_id>", methods=["DELETE"])
@auth.login_required
@auth.data_access
@auth.csrf_protect
def delete_task(task_id):
    conn = db.get_db()
    conn.execute("DELETE FROM tasks WHERE id = ? AND user_id = ?", (task_id, g.user_id))
    conn.commit()
    conn.close()
    return jsonify({"status": "deleted"})


@app.route("/api/tasks/<int:task_id>/reschedule", methods=["POST"])
@auth.login_required
@auth.data_access
@auth.csrf_protect
def reschedule_task(task_id):
    """Auto-rescheduler: move a missed task's due_date to tomorrow."""
    conn = db.get_db()
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    conn.execute(
        "UPDATE tasks SET due_date = ?, status = 'In Progress' WHERE id = ? AND user_id = ?",
        (tomorrow, task_id, g.user_id)
    )
    conn.commit()
    push_notification(conn, "Task rescheduled", "Moved to tomorrow", "tasks")
    conn.close()
    return jsonify({"status": "rescheduled", "new_due_date": tomorrow})


@app.route("/api/tasks/<int:task_id>/steps", methods=["GET"])
@auth.login_required
@auth.data_access
def get_task_steps(task_id):
    conn = db.get_db()
    task = conn.execute("SELECT * FROM tasks WHERE id = ? AND user_id = ?", (task_id, g.user_id)).fetchone()
    conn.close()
    if not task:
        return jsonify({"error": "not found"}), 404
    steps = ai.break_task_into_steps(dict(task))
    return jsonify({"steps": steps})


@app.route("/api/tasks/<int:task_id>/auto-schedule", methods=["POST"])
@auth.login_required
@auth.data_access
@auth.csrf_protect
def auto_schedule_task(task_id):
    """Auto Task Scheduler: find free slots this week and suggest study sessions."""
    conn = db.get_db()
    task = conn.execute("SELECT * FROM tasks WHERE id = ? AND user_id = ?", (task_id, g.user_id)).fetchone()
    if not task:
        conn.close()
        return jsonify({"error": "not found"}), 404

    slots_by_day = {}
    for d in DAYS:
        free = get_free_slots_for_day(conn, d)
        if free:
            slots_by_day[d] = free

    flat_slots = []
    for d, slots in slots_by_day.items():
        for s in slots:
            flat_slots.append({"day": d, **s})

    recommendation = ai.recommend_study_schedule(dict(task), flat_slots)
    conn.close()
    return jsonify({"recommended_slots": recommendation})


# =========================================================
# ATTENDANCE
# =========================================================
@app.route("/api/attendance", methods=["GET"])
@auth.login_required
@auth.data_access
def get_attendance():
    conn = db.get_db()
    settings = conn.execute("SELECT * FROM settings WHERE user_id = ?", (g.user_id,)).fetchone()
    goal_pct = (settings["attendance_goal"] if settings else 85) / 100.0
    goal_pct = min(max(goal_pct, 0.01), 0.99)  # keep the math sane at the edges

    subjects = db.rows_to_list(conn.execute("SELECT * FROM subjects WHERE user_id = ?", (g.user_id,)).fetchall())
    for s in subjects:
        attended = s["classes_attended"]
        missed = s["classes_missed"]
        total = attended + missed
        pct = round((attended / total) * 100, 1) if total else 100.0
        s["attendance_percentage"] = pct
        s["attendance_goal"] = round(goal_pct * 100, 1)

        # Forecast #1: how many more classes can be missed in a row while
        # staying at/above the personal attendance goal.
        skippable = 0
        while total + skippable == 0 or (attended / (total + skippable)) >= goal_pct:
            skippable += 1
            if skippable > 90:
                break
        s["can_skip"] = max(skippable - 1, 0)

        # Forecast #2: if currently below goal, how many classes in a row
        # need to be attended (with no further misses) to climb back to goal.
        if total > 0 and pct < goal_pct * 100:
            need = 0
            a = attended
            while True:
                need += 1
                a += 1
                if a / (total + need) >= goal_pct or need > 200:
                    break
            s["classes_needed_to_reach_goal"] = need
        else:
            s["classes_needed_to_reach_goal"] = 0
    conn.close()
    return jsonify(subjects)


@app.route("/api/attendance/<int:subject_id>", methods=["POST"])
@auth.login_required
@auth.data_access
@auth.csrf_protect
def mark_attendance(subject_id):
    data = request.get_json(force=True)  # {"status": "attended" | "missed"}
    conn = db.get_db()
    if data.get("status") == "attended":
        conn.execute("UPDATE subjects SET classes_attended = classes_attended + 1 WHERE id = ? AND user_id = ?",
                     (subject_id, g.user_id))
    else:
        conn.execute("UPDATE subjects SET classes_missed = classes_missed + 1 WHERE id = ? AND user_id = ?",
                     (subject_id, g.user_id))
    conn.commit()

    row = conn.execute("SELECT * FROM subjects WHERE id = ?", (subject_id,)).fetchone()
    if row:
        total = row["classes_attended"] + row["classes_missed"]
        pct = round((row["classes_attended"] / total) * 100, 1) if total else 100.0
        conn.execute("UPDATE subjects SET attendance_percentage = ? WHERE id = ?", (pct, subject_id))
        conn.commit()
        settings = conn.execute("SELECT attendance_goal FROM settings WHERE user_id = ?", (g.user_id,)).fetchone()
        goal = settings["attendance_goal"] if settings else 80
        if pct < goal:
            push_notification(conn, "Attendance warning", f"{row['subject_name']} attendance is {pct}%", "attendance")
    conn.close()
    return jsonify({"status": "updated"})


# =========================================================
# GOALS — daily / weekly / monthly / semester / GPA / attendance
# =========================================================
def _goal_progress(conn, goal):
    """Compute {current_value, target_value, percent, unit} for a single goal
    by pulling from whatever table already tracks that metric."""
    gtype = goal["goal_type"]
    today = datetime.now().strftime("%Y-%m-%d")

    if gtype == "daily":
        log = conn.execute(
            "SELECT study_minutes FROM productivity_log WHERE user_id = ? AND log_date = ?",
            (g.user_id, today)
        ).fetchone()
        current = round((log["study_minutes"] if log else 0) / 60, 2)
        unit = "hours"

    elif gtype == "weekly":
        week_start = (datetime.now() - timedelta(days=datetime.now().weekday())).strftime("%Y-%m-%d")
        rows = conn.execute(
            "SELECT COALESCE(SUM(study_minutes),0) m FROM productivity_log WHERE user_id = ? AND log_date >= ?",
            (g.user_id, week_start)
        ).fetchone()
        current = round(rows["m"] / 60, 2)
        unit = "hours"

    elif gtype == "monthly":
        month_start = datetime.now().strftime("%Y-%m-01")
        rows = conn.execute(
            "SELECT COALESCE(SUM(study_minutes),0) m FROM productivity_log WHERE user_id = ? AND log_date >= ?",
            (g.user_id, month_start)
        ).fetchone()
        current = round(rows["m"] / 60, 2)
        unit = "hours"

    elif gtype == "semester":
        start = goal["period_start"] or "2000-01-01"
        rows = conn.execute(
            "SELECT COALESCE(SUM(study_minutes),0) m FROM productivity_log WHERE user_id = ? AND log_date >= ?",
            (g.user_id, start)
        ).fetchone()
        current = round(rows["m"] / 60, 2)
        unit = "hours"

    elif gtype == "attendance":
        subjects = conn.execute("SELECT attendance_percentage FROM subjects WHERE user_id = ?", (g.user_id,)).fetchall()
        current = round(sum(s["attendance_percentage"] for s in subjects) / len(subjects), 1) if subjects else 100.0
        unit = "%"

    elif gtype == "gpa":
        current = _compute_overall_gpa(conn)
        unit = "GPA"

    else:
        current = 0
        unit = ""

    target = goal["target_value"]
    pct = round(min(current / target, 1.0) * 100, 1) if target else 0
    return {"current_value": current, "target_value": target, "percent": pct, "unit": unit}


@app.route("/api/goals", methods=["GET"])
@auth.login_required
@auth.data_access
def get_goals():
    conn = db.get_db()
    goals = db.rows_to_list(conn.execute(
        "SELECT * FROM goals WHERE user_id = ? ORDER BY created_at DESC", (g.user_id,)
    ).fetchall())
    for goal in goals:
        goal.update(_goal_progress(conn, goal))
        if goal["status"] == "active" and goal["percent"] >= 100:
            conn.execute("UPDATE goals SET status = 'completed' WHERE id = ?", (goal["id"],))
            conn.commit()
            goal["status"] = "completed"
    conn.close()
    return jsonify(goals)


@app.route("/api/goals", methods=["POST"])
@auth.login_required
@auth.data_access
@auth.csrf_protect
def create_goal():
    data = request.get_json(force=True)
    valid_types = {"daily", "weekly", "monthly", "semester", "gpa", "attendance"}
    goal_type = data.get("goal_type")
    if goal_type not in valid_types:
        return jsonify({"error": "invalid_goal_type", "message": f"goal_type must be one of {sorted(valid_types)}"}), 400
    try:
        target_value = float(data["target_value"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "target_value_required"}), 400

    conn = db.get_db()
    cur = conn.execute(
        "INSERT INTO goals (user_id, goal_type, title, target_value, period_start, period_end) "
        "VALUES (?,?,?,?,?,?)",
        (g.user_id, goal_type, data.get("title") or goal_type.capitalize() + " goal", target_value,
         data.get("period_start"), data.get("period_end"))
    )
    conn.commit()
    new_id = cur.lastrowid
    push_notification(conn, "Goal created", f"New {goal_type} goal set.", "goals")
    conn.close()
    return jsonify({"id": new_id, "status": "created"}), 201


@app.route("/api/goals/<int:goal_id>", methods=["PUT"])
@auth.login_required
@auth.data_access
@auth.csrf_protect
def update_goal(goal_id):
    data = request.get_json(force=True)
    fields = ["title", "target_value", "period_start", "period_end", "status"]
    updates = {k: data[k] for k in fields if k in data}
    conn = db.get_db()
    if updates:
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        conn.execute(f"UPDATE goals SET {set_clause} WHERE id = ? AND user_id = ?",
                     (*updates.values(), goal_id, g.user_id))
        conn.commit()
    conn.close()
    return jsonify({"status": "updated"})


@app.route("/api/goals/<int:goal_id>", methods=["DELETE"])
@auth.login_required
@auth.data_access
@auth.csrf_protect
def delete_goal(goal_id):
    conn = db.get_db()
    conn.execute("DELETE FROM goals WHERE id = ? AND user_id = ?", (goal_id, g.user_id))
    conn.commit()
    conn.close()
    return jsonify({"status": "deleted"})


# =========================================================
# GRADES + GPA TRACKING
# =========================================================
def _subject_grade_percent(conn, subject_id, user_id):
    """Weighted-average percentage across all recorded grades for a subject."""
    rows = conn.execute(
        "SELECT marks_obtained, marks_total, weight FROM grades WHERE subject_id = ? AND user_id = ?",
        (subject_id, user_id)
    ).fetchall()
    if not rows:
        return None
    weighted_sum, weight_total = 0.0, 0.0
    for r in rows:
        if r["marks_total"]:
            weighted_sum += (r["marks_obtained"] / r["marks_total"]) * 100 * r["weight"]
            weight_total += r["weight"]
    if weight_total == 0:
        return None
    return round(weighted_sum / weight_total, 2)


def _compute_overall_gpa(conn):
    """Credit-hour-weighted GPA across every subject that has at least one grade."""
    subjects = conn.execute("SELECT id, credit_hours FROM subjects WHERE user_id = ?", (g.user_id,)).fetchall()
    total_points, total_credits = 0.0, 0.0
    for s in subjects:
        pct = _subject_grade_percent(conn, s["id"], g.user_id)
        if pct is None:
            continue
        _, points = db.percent_to_letter_and_points(pct)
        credits = s["credit_hours"] or 3
        total_points += points * credits
        total_credits += credits
    return round(total_points / total_credits, 2) if total_credits else 0.0


@app.route("/api/gpa", methods=["GET"])
@auth.login_required
@auth.data_access
def get_gpa():
    conn = db.get_db()
    subjects = db.rows_to_list(conn.execute("SELECT * FROM subjects WHERE user_id = ?", (g.user_id,)).fetchall())
    breakdown = []
    for s in subjects:
        pct = _subject_grade_percent(conn, s["id"], g.user_id)
        letter, points = db.percent_to_letter_and_points(pct) if pct is not None else (None, None)
        breakdown.append({
            "subject_id": s["id"], "subject_name": s["subject_name"],
            "credit_hours": s["credit_hours"], "grade_percent": pct,
            "letter_grade": letter, "grade_points": points,
            "target_grade_percent": s["target_grade_percent"],
        })
    overall_gpa = _compute_overall_gpa(conn)
    conn.close()
    return jsonify({"overall_gpa": overall_gpa, "subjects": breakdown})


@app.route("/api/grades", methods=["GET"])
@auth.login_required
@auth.data_access
def get_grades():
    conn = db.get_db()
    subject_id = request.args.get("subject_id", type=int)
    if subject_id:
        rows = db.rows_to_list(conn.execute(
            "SELECT * FROM grades WHERE user_id = ? AND subject_id = ? ORDER BY created_at DESC",
            (g.user_id, subject_id)
        ).fetchall())
    else:
        rows = db.rows_to_list(conn.execute(
            "SELECT gr.*, sub.subject_name FROM grades gr LEFT JOIN subjects sub ON gr.subject_id = sub.id "
            "WHERE gr.user_id = ? ORDER BY gr.created_at DESC", (g.user_id,)
        ).fetchall())
    conn.close()
    return jsonify(rows)


@app.route("/api/grades", methods=["POST"])
@auth.login_required
@auth.data_access
@auth.csrf_protect
def create_grade():
    data = request.get_json(force=True)
    try:
        subject_id = int(data["subject_id"])
        marks_obtained = float(data["marks_obtained"])
        marks_total = float(data["marks_total"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "subject_id_marks_required"}), 400
    if marks_total <= 0:
        return jsonify({"error": "marks_total_must_be_positive"}), 400

    conn = db.get_db()
    owns = conn.execute("SELECT id FROM subjects WHERE id = ? AND user_id = ?", (subject_id, g.user_id)).fetchone()
    if not owns:
        conn.close()
        return jsonify({"error": "subject_not_found"}), 404

    cur = conn.execute(
        "INSERT INTO grades (user_id, subject_id, title, marks_obtained, marks_total, weight) VALUES (?,?,?,?,?,?)",
        (g.user_id, subject_id, data.get("title", "Assessment"), marks_obtained, marks_total,
         float(data.get("weight", 1.0)))
    )
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return jsonify({"id": new_id, "status": "created"}), 201


@app.route("/api/grades/<int:grade_id>", methods=["DELETE"])
@auth.login_required
@auth.data_access
@auth.csrf_protect
def delete_grade(grade_id):
    conn = db.get_db()
    conn.execute("DELETE FROM grades WHERE id = ? AND user_id = ?", (grade_id, g.user_id))
    conn.commit()
    conn.close()
    return jsonify({"status": "deleted"})


@app.route("/api/gpa/calculator", methods=["POST"])
@auth.login_required
@auth.data_access
@auth.csrf_protect
def gpa_calculator():
    """
    'What do I need on my remaining work to hit my target grade/GPA?'
    Given a subject, its recorded weighted grades so far, and the weight of
    work still remaining, solve for the average percentage needed on that
    remaining weight to reach the subject's target_grade_percent.
    """
    data = request.get_json(force=True)
    try:
        subject_id = int(data["subject_id"])
        remaining_weight = float(data.get("remaining_weight", 1.0))
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "subject_id_required"}), 400

    conn = db.get_db()
    subject = conn.execute("SELECT * FROM subjects WHERE id = ? AND user_id = ?", (subject_id, g.user_id)).fetchone()
    if not subject:
        conn.close()
        return jsonify({"error": "subject_not_found"}), 404

    target_pct = float(data.get("target_grade_percent", subject["target_grade_percent"] or 90))
    rows = conn.execute(
        "SELECT marks_obtained, marks_total, weight FROM grades WHERE subject_id = ? AND user_id = ?",
        (subject_id, g.user_id)
    ).fetchall()

    earned_weighted, weight_so_far = 0.0, 0.0
    for r in rows:
        if r["marks_total"]:
            earned_weighted += (r["marks_obtained"] / r["marks_total"]) * 100 * r["weight"]
            weight_so_far += r["weight"]

    total_weight = weight_so_far + remaining_weight
    conn.close()

    if total_weight <= 0:
        return jsonify({"error": "no_weight_to_evaluate"}), 400

    # Solve: (earned_weighted + needed_pct * remaining_weight) / total_weight = target_pct
    needed_pct = ((target_pct * total_weight) - earned_weighted) / remaining_weight if remaining_weight else None
    current_pct = round(earned_weighted / weight_so_far, 2) if weight_so_far else None

    result = {
        "current_grade_percent": current_pct,
        "target_grade_percent": target_pct,
        "remaining_weight": remaining_weight,
        "required_average_percent": round(needed_pct, 2) if needed_pct is not None else None,
        "achievable": needed_pct is not None and needed_pct <= 100,
    }
    if needed_pct is not None and needed_pct > 100:
        result["message"] = "Target isn't reachable even with a perfect score on everything remaining."
    elif needed_pct is not None and needed_pct < 0:
        result["message"] = "Target is already secured based on grades so far."
    return jsonify(result)


# =========================================================
# COMMUNITY / LEADERBOARD (opt-in only)
# =========================================================
@app.route("/api/community/status", methods=["GET"])
@auth.login_required
def community_status():
    conn = db.get_db()
    row = conn.execute("SELECT * FROM leaderboard_optin WHERE user_id = ?", (g.current_user["id"],)).fetchone()
    conn.close()
    if not row:
        return jsonify({"opted_in": False, "display_name": g.current_user["name"]})
    return jsonify({"opted_in": bool(row["opted_in"]), "display_name": row["display_name"] or g.current_user["name"]})


@app.route("/api/community/opt-in", methods=["POST"])
@auth.login_required
@auth.csrf_protect
def community_opt_in():
    if g.role != "student":
        return jsonify({"error": "students_only", "message": "Only student accounts can join the leaderboard."}), 403
    data = request.get_json(force=True)
    opted_in = bool(data.get("opted_in"))
    display_name = (data.get("display_name") or g.current_user["name"] or "Student").strip()[:40]

    conn = db.get_db()
    conn.execute(
        "INSERT INTO leaderboard_optin (user_id, display_name, opted_in) VALUES (?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET display_name = excluded.display_name, opted_in = excluded.opted_in",
        (g.current_user["id"], display_name, int(opted_in))
    )
    conn.commit()
    conn.close()
    return jsonify({"status": "updated", "opted_in": opted_in, "display_name": display_name})


@app.route("/api/community/leaderboard", methods=["GET"])
@auth.login_required
def community_leaderboard():
    """Ranks opted-in students by current study streak and this week's study
    minutes. Nobody appears here unless they've explicitly opted in."""
    conn = db.get_db()
    metric = request.args.get("metric", "weekly_minutes")
    week_start = (datetime.now() - timedelta(days=datetime.now().weekday())).strftime("%Y-%m-%d")

    optin_rows = conn.execute(
        "SELECT user_id, display_name FROM leaderboard_optin WHERE opted_in = 1"
    ).fetchall()

    board = []
    for row in optin_rows:
        uid = row["user_id"]
        weekly = conn.execute(
            "SELECT COALESCE(SUM(study_minutes),0) m FROM productivity_log WHERE user_id = ? AND log_date >= ?",
            (uid, week_start)
        ).fetchone()["m"]
        tasks_done = conn.execute(
            "SELECT COUNT(*) c FROM tasks WHERE user_id = ? AND completed = 1", (uid,)
        ).fetchone()["c"]

        all_logs = conn.execute(
            "SELECT log_date, study_minutes FROM productivity_log WHERE user_id = ? ORDER BY log_date DESC", (uid,)
        ).fetchall()
        log_map = {l["log_date"]: l["study_minutes"] for l in all_logs}
        streak, cursor_date = 0, datetime.now().date()
        while True:
            key = cursor_date.strftime("%Y-%m-%d")
            if log_map.get(key, 0) > 0:
                streak += 1
                cursor_date -= timedelta(days=1)
            else:
                break

        board.append({
            "display_name": row["display_name"] or "Student",
            "weekly_minutes": weekly,
            "study_streak_days": streak,
            "tasks_completed": tasks_done,
            "is_you": uid == g.current_user["id"],
        })

    key_fn = {
        "weekly_minutes": lambda r: r["weekly_minutes"],
        "study_streak_days": lambda r: r["study_streak_days"],
        "tasks_completed": lambda r: r["tasks_completed"],
    }.get(metric, lambda r: r["weekly_minutes"])
    board.sort(key=key_fn, reverse=True)
    for i, row in enumerate(board):
        row["rank"] = i + 1

    conn.close()
    return jsonify({"metric": metric, "leaderboard": board})


# =========================================================
# PLANNER (AI)
# =========================================================
@app.route("/api/planner", methods=["POST"])
@auth.login_required
@auth.data_access
@auth.csrf_protect
def api_planner():
    data = request.get_json(force=True) or {}
    scope = data.get("scope", "daily")  # "daily" | "weekly"
    conn = db.get_db()

    tasks = db.rows_to_list(conn.execute(
        "SELECT * FROM tasks WHERE user_id = ? AND completed = 0 ORDER BY due_date", (g.user_id,)
    ).fetchall())
    for t in tasks:
        t["priority_rank"] = PRIORITY_RANK.get(t.get("priority", "Medium"), 2)

    if scope == "daily":
        day = today_name()
        schedule_today = db.rows_to_list(conn.execute(
            "SELECT title, start_time, end_time FROM schedule WHERE user_id = ? AND day = ? ORDER BY start_time",
            (g.user_id, day)
        ).fetchall())
        free_slots = get_free_slots_for_day(conn, day)
        plan = ai.generate_daily_study_plan(tasks, schedule_today, free_slots)
        conn.close()
        return jsonify({"scope": "daily", "day": day, "plan": plan})

    else:
        schedule_week = db.rows_to_list(conn.execute(
            "SELECT title, day, start_time, end_time FROM schedule WHERE user_id = ? ORDER BY day, start_time",
            (g.user_id,)
        ).fetchall())
        free_by_day = {d: get_free_slots_for_day(conn, d) for d in DAYS}
        plan = ai.generate_weekly_plan(tasks, schedule_week, free_by_day)
        conn.close()
        return jsonify({"scope": "weekly", "plan": plan})


# =========================================================
# GENERIC AI ENDPOINT
# =========================================================
@app.route("/api/ai", methods=["POST"])
@auth.login_required
@auth.data_access
@auth.csrf_protect
def api_ai():
    data = request.get_json(force=True) or {}
    action = data.get("action")
    conn = db.get_db()

    if action == "tomorrow_preview":
        tomorrow_day = DAYS[(datetime.now().weekday() + 1) % 7]
        tomorrow_date = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        schedule_tomorrow = db.rows_to_list(conn.execute(
            "SELECT title, start_time, end_time FROM schedule WHERE user_id = ? AND day = ? ORDER BY start_time",
            (g.user_id, tomorrow_day)
        ).fetchall())
        tasks_tomorrow = db.rows_to_list(conn.execute(
            "SELECT title FROM tasks WHERE user_id = ? AND due_date = ? AND completed = 0",
            (g.user_id, tomorrow_date)
        ).fetchall())
        settings = conn.execute("SELECT * FROM settings WHERE user_id = ?", (g.user_id,)).fetchone()
        alarms = compute_alarms_for_day(conn, tomorrow_day)
        wakeup = alarms[0]["time"] if alarms else "07:00"
        preview = ai.generate_tomorrow_preview(schedule_tomorrow, tasks_tomorrow, wakeup)
        conn.close()
        return jsonify({"preview": preview, "alarms": alarms})

    elif action == "productivity_tip":
        log_date = datetime.now().strftime("%Y-%m-%d")
        log = conn.execute("SELECT * FROM productivity_log WHERE user_id = ? AND log_date = ?",
                            (g.user_id, log_date)).fetchone()
        stats = dict(log) if log else {"study_minutes": 0, "tasks_completed": 0}
        tip = ai.generate_productivity_tip(stats)
        conn.close()
        return jsonify({"tip": tip})

    elif action == "daily_summary":
        log_date = datetime.now().strftime("%Y-%m-%d")
        log = conn.execute("SELECT * FROM productivity_log WHERE user_id = ? AND log_date = ?",
                            (g.user_id, log_date)).fetchone()
        completed = log["tasks_completed"] if log else 0
        minutes = log["study_minutes"] if log else 0
        warnings = conn.execute(
            "SELECT subject_name FROM subjects WHERE user_id = ? AND attendance_percentage < 80", (g.user_id,)
        ).fetchall()
        note = "attendance looks healthy" if not warnings else f"watch attendance in {', '.join(r['subject_name'] for r in warnings)}"
        summary = ai.summarize_daily_progress(completed, minutes, note)
        conn.close()
        return jsonify({"summary": summary})

    else:
        conn.close()
        return jsonify({"error": "unknown action"}), 400


# =========================================================
# SETTINGS
# =========================================================
@app.route("/api/settings", methods=["GET"])
@auth.login_required
@auth.data_access
def get_settings():
    conn = db.get_db()
    row = conn.execute("SELECT * FROM settings WHERE user_id = ?", (g.user_id,)).fetchone()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (g.user_id,)).fetchone()
    conn.close()
    result = db.row_to_dict(row) or {}
    result["name"] = user["name"] if user else "Student"
    result["email"] = user["email"] if user else ""
    return jsonify(result)


@app.route("/api/settings", methods=["POST"])
@auth.login_required
@auth.data_access
@auth.csrf_protect
def update_settings():
    data = request.get_json(force=True)
    conn = db.get_db()

    fields = ["wakeup_offset", "leave_offset", "reminder_offsets", "dark_mode",
              "daily_goal_hours", "weekly_goal_hours", "attendance_goal"]
    updates = {k: data[k] for k in fields if k in data}
    if updates:
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        conn.execute(f"UPDATE settings SET {set_clause} WHERE user_id = ?", (*updates.values(), g.user_id))

    if "name" in data:
        conn.execute("UPDATE users SET name = ? WHERE id = ?", (data["name"], g.user_id))

    conn.commit()
    conn.close()
    return jsonify({"status": "updated"})


# =========================================================
# NOTIFICATIONS
# =========================================================
@app.route("/api/notifications", methods=["GET"])
@auth.login_required
@auth.data_access
def get_notifications():
    conn = db.get_db()
    rows = db.rows_to_list(conn.execute(
        "SELECT * FROM notifications WHERE user_id = ? ORDER BY created_at DESC LIMIT 30", (g.user_id,)
    ).fetchall())
    conn.close()
    return jsonify(rows)


@app.route("/api/notifications/<int:notif_id>/read", methods=["POST"])
@auth.login_required
@auth.data_access
@auth.csrf_protect
def mark_notification_read(notif_id):
    conn = db.get_db()
    conn.execute("UPDATE notifications SET is_read = 1 WHERE id = ? AND user_id = ?", (notif_id, g.user_id))
    conn.commit()
    conn.close()
    return jsonify({"status": "read"})


# =========================================================
# NOTES
# =========================================================
@app.route("/api/notes", methods=["GET"])
@auth.login_required
@auth.data_access
def get_notes():
    conn = db.get_db()
    rows = db.rows_to_list(conn.execute("SELECT * FROM notes WHERE user_id = ? ORDER BY created_at DESC", (g.user_id,)).fetchall())
    conn.close()
    return jsonify(rows)


@app.route("/api/notes", methods=["POST"])
@auth.login_required
@auth.data_access
@auth.csrf_protect
def create_note():
    data = request.get_json(force=True)
    conn = db.get_db()
    cur = conn.execute(
        "INSERT INTO notes (user_id, title, body, link, subject_id) VALUES (?,?,?,?,?)",
        (g.user_id, data["title"], data.get("body", ""), data.get("link", ""), data.get("subject_id"))
    )
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return jsonify({"id": new_id, "status": "created"}), 201


@app.route("/api/notes/<int:note_id>", methods=["DELETE"])
@auth.login_required
@auth.data_access
@auth.csrf_protect
def delete_note(note_id):
    conn = db.get_db()
    conn.execute("DELETE FROM notes WHERE id = ? AND user_id = ?", (note_id, g.user_id))
    conn.commit()
    conn.close()
    return jsonify({"status": "deleted"})


# =========================================================
# ANALYTICS
# =========================================================
@app.route("/api/analytics", methods=["GET"])
@auth.login_required
@auth.data_access
def get_analytics():
    conn = db.get_db()

    subjects = db.rows_to_list(conn.execute("SELECT subject_name, attendance_percentage FROM subjects WHERE user_id = ?", (g.user_id,)).fetchall())

    logs = db.rows_to_list(conn.execute(
        "SELECT * FROM productivity_log WHERE user_id = ? ORDER BY log_date DESC LIMIT 14", (g.user_id,)
    ).fetchall())

    total_tasks = conn.execute("SELECT COUNT(*) c FROM tasks WHERE user_id = ?", (g.user_id,)).fetchone()["c"]
    completed_tasks = conn.execute("SELECT COUNT(*) c FROM tasks WHERE user_id = ? AND completed = 1", (g.user_id,)).fetchone()["c"]

    # study streak: consecutive days (including today) with study_minutes > 0
    all_logs = db.rows_to_list(conn.execute(
        "SELECT log_date, study_minutes FROM productivity_log WHERE user_id = ? ORDER BY log_date DESC", (g.user_id,)
    ).fetchall())
    streak = 0
    cursor_date = datetime.now().date()
    log_map = {l["log_date"]: l["study_minutes"] for l in all_logs}
    while True:
        key = cursor_date.strftime("%Y-%m-%d")
        if log_map.get(key, 0) > 0:
            streak += 1
            cursor_date -= timedelta(days=1)
        else:
            break

    avg_attendance = round(sum(s["attendance_percentage"] for s in subjects) / len(subjects), 1) if subjects else 100.0

    conn.close()
    return jsonify({
        "subjects_attendance": subjects,
        "study_hours_last_14_days": [{"date": l["log_date"], "minutes": l["study_minutes"]} for l in reversed(logs)],
        "task_completion": {"total": total_tasks, "completed": completed_tasks},
        "study_streak_days": streak,
        "average_attendance": avg_attendance,
    })


@app.route("/api/productivity/log", methods=["POST"])
@auth.login_required
@auth.data_access
@auth.csrf_protect
def log_study_time():
    """Record study minutes for today (used by 'Start Focus Session' style actions)."""
    data = request.get_json(force=True)
    minutes = int(data.get("minutes", 0))
    log_date = datetime.now().strftime("%Y-%m-%d")
    conn = db.get_db()
    existing = conn.execute("SELECT * FROM productivity_log WHERE user_id = ? AND log_date = ?", (g.user_id, log_date)).fetchone()
    if existing:
        conn.execute("UPDATE productivity_log SET study_minutes = study_minutes + ? WHERE user_id = ? AND log_date = ?",
                     (minutes, g.user_id, log_date))
    else:
        conn.execute("INSERT INTO productivity_log (user_id, log_date, study_minutes) VALUES (?,?,?)",
                     (g.user_id, log_date, minutes))
    conn.commit()
    conn.close()
    return jsonify({"status": "logged"})


# =========================================================
# SEARCH
# =========================================================
@app.route("/api/search", methods=["GET"])
@auth.login_required
@auth.data_access
def search():
    q = f"%{request.args.get('q', '')}%"
    conn = db.get_db()
    subjects = db.rows_to_list(conn.execute(
        "SELECT id, subject_name as label, 'subject' as kind FROM subjects WHERE user_id = ? AND subject_name LIKE ?",
        (g.user_id, q)
    ).fetchall())
    tasks = db.rows_to_list(conn.execute(
        "SELECT id, title as label, 'task' as kind FROM tasks WHERE user_id = ? AND title LIKE ?",
        (g.user_id, q)
    ).fetchall())
    classes = db.rows_to_list(conn.execute(
        "SELECT id, title as label, 'class' as kind FROM schedule WHERE user_id = ? AND title LIKE ?",
        (g.user_id, q)
    ).fetchall())
    notes = db.rows_to_list(conn.execute(
        "SELECT id, title as label, 'note' as kind FROM notes WHERE user_id = ? AND title LIKE ?",
        (g.user_id, q)
    ).fetchall())
    conn.close()
    return jsonify(subjects + tasks + classes + notes)


# =========================================================
# GOOGLE SYNC (stub — requires real OAuth credentials)
# =========================================================
@app.route("/api/google-sync", methods=["POST"])
@auth.login_required
@auth.data_access
@auth.csrf_protect
def google_sync():
    """
    Real Google Classroom sync requires:
      1. A Google Cloud project with the Classroom API enabled
      2. An OAuth 2.0 Client ID (Web application) with your redirect URI
      3. GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET set in .env
      4. The google-auth-oauthlib + google-api-python-client packages

    Wire the real flow in here once you have those. Until then this
    endpoint responds honestly instead of pretending to sync.
    """
    configured = bool(os.environ.get("GOOGLE_CLIENT_ID")) and bool(os.environ.get("GOOGLE_CLIENT_SECRET"))
    if not configured:
        return jsonify({
            "status": "not_configured",
            "message": "Google OAuth credentials are not set. Add GOOGLE_CLIENT_ID and "
                       "GOOGLE_CLIENT_SECRET to .env, then implement the OAuth redirect "
                       "flow here to enable real Classroom sync."
        }), 501

    # Placeholder for once credentials exist — real implementation would
    # redirect through OAuth consent, then call the Classroom API.
    return jsonify({"status": "ok", "imported": 0, "message": "Sync flow not yet implemented."})


# =========================================================
# AUTH — registration, login, logout, session, password reset
# =========================================================
@app.route("/api/csrf-token", methods=["GET"])
def get_csrf_token():
    """Frontend calls this once on load (and after login) to get a fresh
    token to send back in the X-CSRFToken header on every write."""
    return jsonify({"csrf_token": auth.get_csrf_token()})


@app.route("/api/auth/me", methods=["GET"])
def api_auth_me():
    user = auth.current_user()
    if not user:
        return jsonify({"user": None}), 200
    safe = {k: v for k, v in user.items() if k != "password_hash"}
    return jsonify({"user": safe})


@app.route("/api/auth/register", methods=["POST"])
@auth.csrf_protect
def api_register():
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    role = data.get("role") if data.get("role") in ("student", "parent") else "student"

    if not name:
        return jsonify({"error": "name_required"}), 400
    if not auth.validate_email(email):
        return jsonify({"error": "invalid_email"}), 400
    pw_error = auth.validate_password_strength(password)
    if pw_error:
        return jsonify({"error": "weak_password", "message": pw_error}), 400

    conn = db.get_db()
    existing = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
    if existing:
        conn.close()
        return jsonify({"error": "email_in_use"}), 409

    cur = conn.execute(
        "INSERT INTO users (name, email, password_hash, role) VALUES (?,?,?,?)",
        (name, email, auth.hash_password(password), role)
    )
    user_id = cur.lastrowid
    if role == "student":
        auth.generate_invite_code(conn, user_id)
    conn.commit()
    user_row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()

    if role == "student":
        db.create_default_settings(user_id)

    auth.start_session(user_row, remember=bool(data.get("remember")))
    safe = {k: v for k, v in dict(user_row).items() if k != "password_hash"}
    return jsonify({"status": "registered", "user": safe}), 201


@app.route("/api/auth/login", methods=["POST"])
@auth.csrf_protect
def api_login():
    data = request.get_json(force=True)
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    conn = db.get_db()
    user = conn.execute("SELECT * FROM users WHERE email = ? AND is_active = 1", (email,)).fetchone()
    conn.close()

    if not user or not auth.verify_password(password, user["password_hash"]):
        return jsonify({"error": "invalid_credentials"}), 401

    auth.start_session(user, remember=bool(data.get("remember")))
    safe = {k: v for k, v in dict(user).items() if k != "password_hash"}
    return jsonify({"status": "logged_in", "user": safe})


@app.route("/api/auth/logout", methods=["POST"])
@auth.csrf_protect
def api_logout():
    auth.end_session()
    return jsonify({"status": "logged_out"})


@app.route("/api/auth/forgot-password", methods=["POST"])
@auth.csrf_protect
def api_forgot_password():
    data = request.get_json(force=True)
    email = (data.get("email") or "").strip().lower()

    conn = db.get_db()
    user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    conn.close()

    # Always respond the same way regardless of whether the email exists,
    # so this endpoint can't be used to enumerate registered accounts.
    if user:
        token = auth.make_reset_token(user["id"])
        reset_link = f"{request.host_url.rstrip('/')}/reset-password?token={token}"
        # No SMTP/email service is configured yet, so the link is logged
        # server-side instead of silently vanishing. Wire a real mail
        # provider here (e.g. SendGrid, SES) and email `reset_link` instead.
        print(f"[password reset] {email} -> {reset_link}")

    return jsonify({"status": "if_account_exists_email_sent"})


@app.route("/api/auth/reset-password", methods=["POST"])
@auth.csrf_protect
def api_reset_password():
    data = request.get_json(force=True)
    token = data.get("token") or ""
    new_password = data.get("password") or ""

    user_id = auth.verify_reset_token(token)
    if not user_id:
        return jsonify({"error": "invalid_or_expired_token"}), 400

    pw_error = auth.validate_password_strength(new_password)
    if pw_error:
        return jsonify({"error": "weak_password", "message": pw_error}), 400

    conn = db.get_db()
    conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                 (auth.hash_password(new_password), user_id))
    conn.commit()
    conn.close()
    return jsonify({"status": "password_reset"})


# ---------------------------------------------------------
# Google OAuth login (separate from the Classroom data-sync stub above)
# ---------------------------------------------------------
@app.route("/auth/google/login")
def google_login():
    if not auth.google_oauth_configured():
        return jsonify({
            "error": "google_oauth_not_configured",
            "message": "Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in .env, and add "
                       f"{request.host_url.rstrip('/')}/auth/google/callback as an authorized "
                       "redirect URI in the Google Cloud Console."
        }), 501
    redirect_uri = url_for("google_callback", _external=True)
    return auth.oauth.google.authorize_redirect(redirect_uri)


@app.route("/auth/google/callback")
def google_callback():
    if not auth.google_oauth_configured():
        return redirect(url_for("login_page"))

    token = auth.oauth.google.authorize_access_token()
    userinfo = token.get("userinfo")
    if not userinfo:
        return redirect(url_for("login_page"))

    email = userinfo["email"].lower()
    google_id = userinfo["sub"]
    name = userinfo.get("name") or email.split("@")[0]

    conn = db.get_db()
    user = conn.execute("SELECT * FROM users WHERE google_id = ? OR email = ?", (google_id, email)).fetchone()
    if user:
        conn.execute("UPDATE users SET google_id = ? WHERE id = ?", (google_id, user["id"]))
        conn.commit()
        user_id = user["id"]
    else:
        cur = conn.execute(
            "INSERT INTO users (name, email, google_id, role) VALUES (?,?,?, 'student')",
            (name, email, google_id)
        )
        user_id = cur.lastrowid
        auth.generate_invite_code(conn, user_id)
        conn.commit()
        db.create_default_settings(user_id)

    user_row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()

    auth.start_session(user_row, remember=True)
    return redirect(url_for("index"))


# =========================================================
# PARENT DASHBOARD — linking + read-only student access
# =========================================================
@app.route("/api/parent/link", methods=["POST"])
@auth.login_required
@auth.csrf_protect
def api_parent_link():
    if g.role != "parent":
        return jsonify({"error": "parents_only"}), 403
    data = request.get_json(force=True)
    code = (data.get("invite_code") or "").strip().upper()
    if not code:
        return jsonify({"error": "invite_code_required"}), 400

    student, error = auth.link_parent_to_student(g.current_user["id"], code)
    if error:
        return jsonify({"error": error}), 404
    return jsonify({"status": "linked", "student": {"id": student["id"], "name": student["name"]}})


@app.route("/api/parent/students", methods=["GET"])
@auth.login_required
def api_parent_students():
    if g.role != "parent":
        return jsonify({"error": "parents_only"}), 403
    conn = db.get_db()
    rows = db.rows_to_list(conn.execute(
        "SELECT u.id, u.name, u.email FROM parent_links pl "
        "JOIN users u ON u.id = pl.student_id "
        "WHERE pl.parent_id = ? AND pl.status = 'approved'",
        (g.current_user["id"],)
    ).fetchall())
    conn.close()
    return jsonify(rows)


@app.route("/api/student/invite-code", methods=["GET"])
@auth.login_required
def api_student_invite_code():
    if g.role != "student":
        return jsonify({"error": "students_only"}), 403
    conn = db.get_db()
    row = conn.execute("SELECT invite_code FROM users WHERE id = ?", (g.current_user["id"],)).fetchone()
    if not row["invite_code"]:
        code = auth.generate_invite_code(conn, g.current_user["id"])
    else:
        code = row["invite_code"]
    conn.close()
    return jsonify({"invite_code": code})


@app.route("/api/student/invite-code/regenerate", methods=["POST"])
@auth.login_required
@auth.csrf_protect
def api_regenerate_invite_code():
    if g.role != "student":
        return jsonify({"error": "students_only"}), 403
    conn = db.get_db()
    code = auth.generate_invite_code(conn, g.current_user["id"])
    conn.close()
    return jsonify({"invite_code": code})


if __name__ == "__main__":
    app.run(debug=True, port=5000)
