"""
auth.py
-------------------------------------------------
StudySync authentication & authorization layer.

Handles:
  - password hashing / verification
  - session-backed login (student + parent roles)
  - CSRF protection (double-submit token in session)
  - login_required decorator, with built-in support for parents
    viewing a linked student's data read-only via ?student_id=
  - parent <-> student linking via invite codes
  - password reset tokens (itsdangerous, time-limited)
  - Google OAuth (Authlib) — reuses GOOGLE_CLIENT_ID/SECRET from .env
-------------------------------------------------
"""

import os
import re
import secrets
from functools import wraps

from flask import session, request, jsonify, g
from werkzeug.security import generate_password_hash, check_password_hash
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from authlib.integrations.flask_client import OAuth

import database as db

# ---------------------------------------------------------------------------
# Secret key — REQUIRED to be set in .env for any real deployment. Sessions,
# CSRF tokens, and password-reset tokens are all signed with this.
# ---------------------------------------------------------------------------
SECRET_KEY = os.environ.get("SECRET_KEY")
if not SECRET_KEY:
    SECRET_KEY = secrets.token_hex(32)
    print(
        "WARNING: SECRET_KEY is not set in .env. Using a randomly generated key "
        "for this process only — all sessions will be invalidated on restart. "
        "Set SECRET_KEY in .env before deploying."
    )

_reset_serializer = URLSafeTimedSerializer(SECRET_KEY, salt="studysync-password-reset")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

oauth = OAuth()


def init_oauth(app):
    oauth.init_app(app)
    if os.environ.get("GOOGLE_CLIENT_ID") and os.environ.get("GOOGLE_CLIENT_SECRET"):
        oauth.register(
            name="google",
            client_id=os.environ["GOOGLE_CLIENT_ID"],
            client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
            server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
            client_kwargs={"scope": "openid email profile"},
        )


def google_oauth_configured():
    return bool(os.environ.get("GOOGLE_CLIENT_ID")) and bool(os.environ.get("GOOGLE_CLIENT_SECRET"))


# ---------------------------------------------------------------------------
# Password helpers
# ---------------------------------------------------------------------------
def hash_password(password):
    return generate_password_hash(password)


def verify_password(password, password_hash):
    if not password_hash:
        return False
    return check_password_hash(password_hash, password)


def validate_password_strength(password):
    if not password or len(password) < 8:
        return "Password must be at least 8 characters."
    if not re.search(r"[A-Za-z]", password) or not re.search(r"[0-9]", password):
        return "Password must contain both letters and numbers."
    return None


def validate_email(email):
    return bool(email) and bool(EMAIL_RE.match(email))


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------
def start_session(user_row, remember=False):
    """Log a user in: reset the session and store identity + a fresh CSRF token."""
    session.clear()
    session["user_id"] = user_row["id"]
    session["role"] = user_row["role"]
    session["csrf_token"] = secrets.token_hex(32)
    session.permanent = bool(remember)


def end_session():
    session.clear()


def current_user():
    """Return the logged-in user's DB row as a dict, or None if not logged in."""
    user_id = session.get("user_id")
    if not user_id:
        return None
    conn = db.get_db()
    row = conn.execute("SELECT * FROM users WHERE id = ? AND is_active = 1", (user_id,)).fetchone()
    conn.close()
    return db.row_to_dict(row)


def get_csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(32)
    return session["csrf_token"]


# ---------------------------------------------------------------------------
# Decorators
# ---------------------------------------------------------------------------
def login_required(view):
    """Require a logged-in user. Populates g.current_user / g.role / g.user_id
    (g.user_id defaults to the logged-in user's own id). Use this alone for
    routes that are not per-student data (auth, parent-linking, account
    management). Use @data_access on top of this for student-data routes,
    which adds parent-read-only-via-?student_id= scoping."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()
        if not user:
            return jsonify({"error": "authentication_required"}), 401
        g.current_user = user
        g.role = user["role"]
        g.user_id = user["id"]
        g.viewing_as_parent = False
        return view(*args, **kwargs)
    return wrapped


def data_access(view):
    """
    Stack on top of @login_required for routes that read/write a student's
    academic data. Students access their own data as normal. Parent accounts
    are read-only and must pass ?student_id=<id> for a linked student —
    g.user_id is swapped to that student's id so the underlying query code
    is unchanged.
    """
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.role == "parent":
            if request.method != "GET":
                return jsonify({"error": "read_only", "message": "Parent accounts are read-only."}), 403
            student_id = request.args.get("student_id", type=int)
            if student_id is None:
                return jsonify({
                    "error": "student_id_required",
                    "message": "Parents must pass ?student_id=<id>. See /api/parent/students."
                }), 400
            if not is_linked(g.current_user["id"], student_id):
                return jsonify({"error": "not_linked"}), 403
            g.user_id = student_id
            g.viewing_as_parent = True
        return view(*args, **kwargs)
    return wrapped


def csrf_protect(view):
    """Require a valid X-CSRFToken header matching the session token on writes."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if request.method in ("POST", "PUT", "DELETE", "PATCH"):
            token = request.headers.get("X-CSRFToken", "")
            if not token or not secrets.compare_digest(token, session.get("csrf_token", "")):
                return jsonify({"error": "csrf_invalid", "message": "Missing or invalid CSRF token."}), 403
        return view(*args, **kwargs)
    return wrapped


# ---------------------------------------------------------------------------
# Parent <-> student linking
# ---------------------------------------------------------------------------
def generate_invite_code(conn, user_id):
    """(Re)generate a short invite code a student can share with a parent."""
    code = secrets.token_hex(4).upper()
    conn.execute("UPDATE users SET invite_code = ? WHERE id = ?", (code, user_id))
    conn.commit()
    return code


def is_linked(parent_id, student_id):
    conn = db.get_db()
    row = conn.execute(
        "SELECT 1 FROM parent_links WHERE parent_id = ? AND student_id = ? AND status = 'approved'",
        (parent_id, student_id)
    ).fetchone()
    conn.close()
    return row is not None


def link_parent_to_student(parent_id, invite_code):
    """Link a parent to the student owning invite_code. Returns (student_row, error)."""
    conn = db.get_db()
    student = conn.execute(
        "SELECT * FROM users WHERE invite_code = ? AND role = 'student'", (invite_code,)
    ).fetchone()
    if not student:
        conn.close()
        return None, "invalid_code"

    existing = conn.execute(
        "SELECT 1 FROM parent_links WHERE parent_id = ? AND student_id = ?",
        (parent_id, student["id"])
    ).fetchone()
    if existing:
        conn.close()
        return db.row_to_dict(student), None

    conn.execute(
        "INSERT INTO parent_links (parent_id, student_id, status) VALUES (?, ?, 'approved')",
        (parent_id, student["id"])
    )
    conn.commit()
    conn.close()
    return db.row_to_dict(student), None


# ---------------------------------------------------------------------------
# Password reset tokens (time-limited, signed — no email transport configured
# yet, so the link is logged server-side instead of silently discarded).
# ---------------------------------------------------------------------------
def make_reset_token(user_id):
    return _reset_serializer.dumps({"uid": user_id})


def verify_reset_token(token, max_age_seconds=3600):
    try:
        data = _reset_serializer.loads(token, max_age=max_age_seconds)
        return data["uid"]
    except (BadSignature, SignatureExpired):
        return None
