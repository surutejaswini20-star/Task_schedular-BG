"""Intelligent Task & Study Scheduler — production-oriented Flask application."""
import os
import secrets
import hashlib
import hmac
import re
from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import urlparse

from dotenv import load_dotenv
from flask import Flask, abort, flash, jsonify, redirect, render_template, request, session, url_for
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect
from sqlalchemy import inspect, text
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

from scheduler import build_schedule

load_dotenv()
db = SQLAlchemy()
csrf = CSRFProtect()


def _database_url():
    url = os.getenv("DATABASE_URL", "sqlite:///study_scheduler.db")
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://") and "+psycopg" not in url:
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


def create_app(test_config=None):
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.getenv("SECRET_KEY", "unsafe-development-key-change-me"),
        SQLALCHEMY_DATABASE_URI=_database_url(),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.getenv("COOKIE_SECURE", "false").lower() == "true",
        PERMANENT_SESSION_LIFETIME=timedelta(days=7),
        MAX_CONTENT_LENGTH=1 * 1024 * 1024,
        WTF_CSRF_TIME_LIMIT=3600,
    )
    if test_config:
        app.config.update(test_config)
    if os.getenv("TRUST_PROXY", "false").lower() == "true":
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    db.init_app(app)
    csrf.init_app(app)
    @app.context_processor
    def inject_globals():
        return {"current_user": get_current_user()}

    @app.before_request
    def require_login():
        allowed = {"login", "register", "verify_email", "resend_otp", "health", "static"}
        if request.endpoint and request.endpoint not in allowed and get_current_user() is None:
            if request.endpoint == "logout":
                return redirect(url_for("login"))
            return redirect(url_for("login", next=request.full_path if request.query_string else request.path))

    @app.get("/")
    def dashboard():
        user = get_current_user()
        tasks = StudyTask.query.filter_by(user_id=user.id).order_by(StudyTask.due_date.asc(), StudyTask.due_time.asc()).all()
        total = len(tasks)
        completed = sum(t.status == "Completed" for t in tasks)
        pending = sum(t.status != "Completed" for t in tasks)
        overdue = sum(t.is_overdue for t in tasks)
        minutes = sum(t.duration for t in tasks if t.status != "Completed")
        priority_order = {"High": 0, "Medium": 1, "Low": 2}
        sorted_tasks = sorted([t for t in tasks if t.status != "Completed"], key=lambda t: (t.due_date, priority_order.get(t.priority, 1)))
        return render_template("dashboard.html", tasks=tasks, total=total, completed=completed, pending=pending, overdue=overdue, minutes=minutes, sorted_tasks=sorted_tasks)

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if get_current_user():
            return redirect(url_for("dashboard"))
        next_url = safe_next_url(request.args.get("next", "") or request.form.get("next", ""))
        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "")
            user = User.query.filter_by(email=email).first()
            if not user or not user.password_hash or not check_password_hash(user.password_hash, password):
                flash("Invalid email or password.", "error")
                return render_template("login.html", next_url=next_url), 401
            if not user.is_verified:
                flash("Verify your email address before signing in.", "error")
                return redirect(url_for("verify_email", email=email))
            session.clear()
            session["user_id"] = user.id
            session.permanent = True
            flash("Signed in successfully.", "success")
            return redirect(next_url or url_for("dashboard"))
        return render_template("login.html", next_url=next_url)

    @app.route("/register", methods=["GET", "POST"])
    def register():
        if get_current_user():
            return redirect(url_for("dashboard"))
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            email = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "")
            if not name or len(name) > 120:
                flash("Enter a name between 1 and 120 characters.", "error")
                return render_template("register.html"), 400
            if not valid_email(email):
                flash("Enter a valid email address.", "error")
                return render_template("register.html"), 400
            if is_disposable_email(email):
                flash("Temporary/disposable email addresses are not allowed. Use a regular email account.", "error")
                return render_template("register.html"), 400
            if len(password) < 10 or len(password) > 128:
                flash("Password must be between 10 and 128 characters.", "error")
                return render_template("register.html"), 400

            user = User.query.filter_by(email=email).first()
            now = datetime.utcnow()
            if user and user.is_verified:
                flash("An account with this email already exists. Sign in instead.", "error")
                return redirect(url_for("login"))
            if user and user.otp_last_sent_at and (now - user.otp_last_sent_at).total_seconds() < 60:
                flash("An OTP was sent recently. Wait 60 seconds before requesting another.", "error")
                return render_template("verify_email.html", email=email), 429
            if user is None:
                user = User(email=email, name=name, password_hash=generate_password_hash(password),
                            is_verified=False)
                db.session.add(user)
            else:
                user.name = name
                user.password_hash = generate_password_hash(password)
            otp = f"{secrets.randbelow(1_000_000):06d}"
            user.otp_hash = _otp_digest(otp)
            user.otp_expires_at = now + timedelta(minutes=10)
            user.otp_attempts = 0
            user.otp_last_sent_at = now
            db.session.commit()
            try:
                from tasks import _send_email
                text_body = f"Your TaskScheduler and DeadlineManager verification code is {otp}. It expires in 10 minutes. If you did not request this, ignore this email."
                html = ("<div style='font-family:Arial,sans-serif;line-height:1.6'>"
                        "<h2>Verify your TaskScheduler and DeadlineManager account</h2><p>Your one-time verification code is:</p>"
                        f"<p style='font-size:28px;font-weight:bold;letter-spacing:6px'>{otp}</p>"
                        "<p>This code expires in 10 minutes. Do not share it with anyone.</p></div>")
                _send_email(email, "Your TaskScheduler and DeadlineManager verification code", html, text_body)
            except Exception:
                app.logger.exception("Unable to send registration OTP")
                db.session.rollback()
                flash("We could not send the verification email. Check the email service configuration and try again later.", "error")
                return render_template("register.html"), 503
            flash("Verification code sent. Check your inbox and spam folder.", "success")
            return redirect(url_for("verify_email", email=email))
        return render_template("register.html")

    @app.route("/verify-email", methods=["GET", "POST"])
    def verify_email():
        if get_current_user():
            return redirect(url_for("dashboard"))
        email = request.args.get("email", "").strip().lower() or request.form.get("email", "").strip().lower()
        if request.method == "POST":
            otp = request.form.get("otp", "").strip()
            user = User.query.filter_by(email=email).first()
            now = datetime.utcnow()
            if not user or user.is_verified or not user.otp_hash:
                flash("This verification request is invalid. Register again or sign in.", "error")
                return redirect(url_for("register"))
            if user.otp_expires_at is None or user.otp_expires_at < now:
                user.otp_hash = None
                db.session.commit()
                flash("That code has expired. Request a new one.", "error")
                return redirect(url_for("verify_email", email=email))
            if user.otp_attempts >= 5:
                flash("Too many incorrect attempts. Request a new code after the cooldown.", "error")
                return render_template("verify_email.html", email=email), 429
            if not re.fullmatch(r"\d{6}", otp) or not hmac.compare_digest(user.otp_hash, _otp_digest(otp)):
                user.otp_attempts += 1
                db.session.commit()
                flash("Incorrect verification code.", "error")
                return render_template("verify_email.html", email=email), 400
            user.is_verified = True
            user.otp_hash = None
            user.otp_expires_at = None
            user.otp_attempts = 0
            db.session.commit()
            session.clear()
            session["user_id"] = user.id
            session.permanent = True
            flash("Email verified. Your account is ready.", "success")
            return redirect(url_for("dashboard"))
        return render_template("verify_email.html", email=email)

    @app.post("/resend-otp")
    def resend_otp():
        email = request.form.get("email", "").strip().lower()
        user = User.query.filter_by(email=email).first()
        if not user or user.is_verified:
            flash("If this address has a pending verification, a new code can be requested.", "success")
            return redirect(url_for("login"))
        now = datetime.utcnow()
        if user.otp_last_sent_at and (now - user.otp_last_sent_at).total_seconds() < 60:
            flash("Wait 60 seconds before requesting another code.", "error")
            return redirect(url_for("verify_email", email=email))
        otp = f"{secrets.randbelow(1_000_000):06d}"
        user.otp_hash = _otp_digest(otp)
        user.otp_expires_at = now + timedelta(minutes=10)
        user.otp_attempts = 0
        user.otp_last_sent_at = now
        db.session.commit()
        try:
            from tasks import _send_email
            text_body = f"Your new TaskScheduler and DeadlineManager verification code is {otp}. It expires in 10 minutes."
            html = f"<div style='font-family:Arial,sans-serif'><h2>Your new verification code</h2><p style='font-size:28px;letter-spacing:6px;font-weight:bold'>{otp}</p><p>Expires in 10 minutes.</p></div>"
            _send_email(email, "Your TaskScheduler and DeadlineManager verification code", html, text_body)
        except Exception:
            app.logger.exception("Unable to resend registration OTP")
            flash("We could not send a new code. Try again later.", "error")
            return redirect(url_for("verify_email", email=email))
        flash("A new verification code was sent.", "success")
        return redirect(url_for("verify_email", email=email))

    @app.post("/logout")
    def logout():
        session.clear()
        flash("You have been signed out.", "success")
        return redirect(url_for("login"))

    @app.route("/tasks/add", methods=["GET", "POST"])
    def add_task():
        user = get_current_user()
        if request.method == "GET":
            return render_template("task_form.html", today=date.today().isoformat())
        title = request.form.get("title", "").strip()
        subject = request.form.get("subject", "General").strip() or "General"
        try:
            due = date.fromisoformat(request.form.get("due_date", ""))
            raw_due_time = request.form.get("due_time", "").strip()
            if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", raw_due_time):
                raise ValueError
            due_time = time.fromisoformat(raw_due_time)
            duration = int(request.form.get("duration", 60))
            priority = request.form.get("priority", "Medium")
            from zoneinfo import ZoneInfo
            user_tz = ZoneInfo(user.timezone or "Asia/Kolkata")
            deadline_local = datetime.combine(due, due_time).replace(tzinfo=user_tz)
            deadline = deadline_local.astimezone(timezone.utc).replace(tzinfo=None)
            now_local = datetime.now(user_tz)
            if not title or len(title) > 160 or duration < 5 or duration > 720 or priority not in {"Low", "Medium", "High"}:
                raise ValueError
            if deadline_local <= now_local:
                flash("Deadline must be a future date and time.", "error")
                return render_template("task_form.html", today=date.today().isoformat()), 400
        except (ValueError, TypeError):
            flash("Enter a valid title, future deadline, time in HH:MM format, duration, and priority.", "error")
            return render_template("task_form.html", today=date.today().isoformat()), 400

        task = StudyTask(user_id=user.id, title=title, subject=subject[:100], due_date=due,
                         due_time=due_time, duration=duration, priority=priority,
                         status="Pending", notes=request.form.get("notes", "").strip()[:3000])
        db.session.add(task)
        db.session.flush()
        # Every new task gets one automatic deadline email. Custom reminders can
        # be added separately on the task reminder page.
        automatic_reminder = Reminder(
            user_id=user.id, task_id=task.id, recipient=user.email,
            scheduled_at=deadline, status="scheduled"
        )
        db.session.add(automatic_reminder)
        db.session.commit()
        flash("Task added. Its deadline email is scheduled automatically.", "success")
        return redirect(url_for("dashboard"))

    @app.post("/tasks/<int:task_id>/toggle")
    def toggle_task(task_id):
        task = owned_task(task_id)
        task.status = "Completed" if task.status != "Completed" else "Pending"
        if task.status == "Completed":
            # Remove unsent reminders from the queue; the worker also checks
            # task status immediately before sending to close race conditions.
            Reminder.query.filter(
                Reminder.task_id == task.id,
                Reminder.status.in_(["scheduled", "queued"])
            ).update({"status": "cancelled"}, synchronize_session=False)
        else:
            # If reopened before its deadline, restore the automatic deadline
            # reminder when no active/sent reminder remains.
            from zoneinfo import ZoneInfo
            user = get_current_user()
            deadline_local = datetime.combine(task.due_date, task.due_time).replace(tzinfo=ZoneInfo(user.timezone or "Asia/Kolkata"))
            deadline = deadline_local.astimezone(timezone.utc).replace(tzinfo=None)
            if deadline > datetime.utcnow() and not Reminder.query.filter(
                Reminder.task_id == task.id,
                Reminder.status.in_(["scheduled", "queued", "sent"])
            ).first():
                db.session.add(Reminder(user_id=user.id, task_id=task.id,
                    recipient=user.email, scheduled_at=deadline, status="scheduled"))
        db.session.commit()
        flash("Task marked as " + task.status.lower() + ".", "success")
        return redirect(url_for("dashboard"))

    @app.post("/tasks/<int:task_id>/delete")
    def delete_task(task_id):
        task = owned_task(task_id)
        Reminder.query.filter_by(task_id=task.id).delete()
        db.session.delete(task)
        db.session.commit()
        flash("Task deleted.", "success")
        return redirect(url_for("dashboard"))

    @app.get("/schedule")
    def schedule():
        tasks = StudyTask.query.filter_by(user_id=get_current_user().id, status="Pending").all()
        plan = build_schedule(tasks, date.today())
        return render_template("schedule.html", plan=plan, tasks=tasks)

    @app.route("/tasks/<int:task_id>/reminder", methods=["GET", "POST"])
    def task_reminder(task_id):
        user = get_current_user()
        task = owned_task(task_id)
        if request.method == "GET":
            return render_template("task_reminder.html", task=task, today=date.today().isoformat())
        email = request.form.get("email", "").strip().lower() or user.email
        if not valid_email(email):
            flash("Enter a valid recipient email address.", "error")
            return redirect(url_for("task_reminder", task_id=task.id))
        try:
            reminder_date = date.fromisoformat(request.form.get("reminder_date", ""))
            raw_reminder_time = request.form.get("reminder_time", "").strip()
            if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", raw_reminder_time):
                raise ValueError
            reminder_time = time.fromisoformat(raw_reminder_time)
            from zoneinfo import ZoneInfo
            user_tz = ZoneInfo(user.timezone or "Asia/Kolkata")
            reminder_local = datetime.combine(reminder_date, reminder_time).replace(tzinfo=user_tz)
            deadline_local = datetime.combine(task.due_date, task.due_time).replace(tzinfo=user_tz)
            if task.status == "Completed":
                raise ValueError("Completed tasks cannot have active reminders.")
            if reminder_local <= datetime.now(user_tz):
                raise ValueError("Reminder time must be in the future.")
            if reminder_local >= deadline_local:
                raise ValueError("Reminder time must be before the task deadline.")
            reminder_at = reminder_local.astimezone(timezone.utc).replace(tzinfo=None)
        except (ValueError, TypeError):
            flash("Choose a valid future reminder time before the task deadline.", "error")
            return render_template("task_reminder.html", task=task, today=date.today().isoformat()), 400
        db.session.add(Reminder(user_id=user.id, task_id=task.id, recipient=email,
                                scheduled_at=reminder_at, status="scheduled"))
        db.session.commit()
        flash("Additional reminder scheduled.", "success")
        return redirect(url_for("dashboard"))

    @app.route("/daily-reminders", methods=["GET", "POST"])
    def daily_reminder_settings():
        user = get_current_user()
        if request.method == "GET":
            return render_template("daily_reminders.html")
        enabled = request.form.get("enabled") == "on"
        raw_time = request.form.get("daily_time", "08:00").strip()
        tz_name = request.form.get("timezone", "Asia/Kolkata").strip()
        try:
            if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", raw_time):
                raise ValueError
            daily_time = time.fromisoformat(raw_time)
            from zoneinfo import ZoneInfo
            ZoneInfo(tz_name)
        except Exception:
            flash("Enter a valid time in HH:MM format and timezone.", "error")
            return render_template("daily_reminders.html"), 400
        user.daily_reminder_enabled = enabled
        user.daily_reminder_time = daily_time
        user.timezone = tz_name[:80]
        db.session.commit()
        flash("Daily reminder settings updated.", "success")
        return redirect(url_for("dashboard"))

    @app.get("/health")
    def health():
        return jsonify({"status": "ok", "service": "TaskScheduler and DeadlineManager"})

    with app.app_context():
        db.create_all()
        _lightweight_migrations(db.engine)
    return app


def _lightweight_migrations(engine):
    """Best-effort additions for existing SQLite project databases; use migrations for larger changes."""
    inspector = inspect(engine)
    if "user" in inspector.get_table_names():
        cols = {c["name"] for c in inspector.get_columns("user")}
        dialect = engine.dialect.name
        additions = {
            "password_hash": "VARCHAR(255)",
            "is_verified": "BOOLEAN NOT NULL DEFAULT TRUE",
            "otp_hash": "VARCHAR(64)",
            "otp_expires_at": "TIMESTAMP",
            "otp_attempts": "INTEGER NOT NULL DEFAULT 0",
            "otp_last_sent_at": "TIMESTAMP",
        }
        with engine.begin() as conn:
            for col, sql_type in additions.items():
                if col not in cols:
                    conn.execute(text(f'ALTER TABLE "user" ADD COLUMN {col} {sql_type}'))
    if engine.dialect.name != "sqlite":
        return
    inspector = inspect(engine)
    if "study_task" in inspector.get_table_names():
        cols = {c["name"] for c in inspector.get_columns("study_task")}
        with engine.begin() as conn:
            if "due_time" not in cols:
                conn.execute(text("ALTER TABLE study_task ADD COLUMN due_time TIME NOT NULL DEFAULT '23:59:00'"))
            if "reminder_at" not in cols:
                conn.execute(text("ALTER TABLE study_task ADD COLUMN reminder_at DATETIME"))


def get_current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    return db.session.get(User, user_id)


def owned_task(task_id):
    task = StudyTask.query.filter_by(id=task_id, user_id=get_current_user().id).first()
    if task is None:
        abort(404)
    return task


def safe_next_url(target):
    if not target:
        return ""
    parsed = urlparse(target)
    if parsed.scheme or parsed.netloc or not target.startswith("/") or target.startswith("//"):
        return ""
    return target


# Curated blocklist is intentionally conservative; add domains via DISPOSABLE_EMAIL_DOMAINS.
_DISPOSABLE_DOMAINS = {
    "mailinator.com", "yopmail.com", "guerrillamail.com", "guerrillamail.net",
    "sharklasers.com", "grr.la", "10minutemail.com", "10minutemail.net",
    "temp-mail.org", "temp-mail.io", "tempmail.com", "tempail.com",
    "throwawaymail.com", "maildrop.cc", "getnada.com", "emailondeck.com",
    "fakeinbox.com", "dispostable.com", "mintemail.com", "mohmal.com",
    "trashmail.com", "trashmail.me", "trashmail.net", "mailnesia.com",
    "discard.email", "discardmail.com", "inboxkitten.com", "tempr.email",
    "burnermail.io", "emailfake.com", "crazymailing.com", "tmpmail.org",
    "tmpmail.net", "tmail.ws", "mailsac.com", "harakirimail.com",
}


def is_disposable_email(email):
    domain = email.rsplit("@", 1)[-1].lower().rstrip(".")
    extra = {item.strip().lower() for item in os.getenv("DISPOSABLE_EMAIL_DOMAINS", "").split(",") if item.strip()}
    return domain in (_DISPOSABLE_DOMAINS | extra)


def _otp_digest(otp):
    pepper = current_app_secret()
    return hashlib.sha256((pepper + ":" + otp).encode("utf-8")).hexdigest()


def current_app_secret():
    # Uses the configured application secret as an OTP hash pepper.
    from flask import current_app
    return str(current_app.config["SECRET_KEY"])


def valid_email(value):
    return len(value) <= 254 and "@" in value and not value.startswith("@") and "." in value.rsplit("@", 1)[-1] and " " not in value


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(254), unique=True, nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False)
    password_hash = db.Column(db.String(255), nullable=True)
    is_verified = db.Column(db.Boolean, nullable=False, default=False, index=True)
    otp_hash = db.Column(db.String(64), nullable=True)
    otp_expires_at = db.Column(db.DateTime, nullable=True)
    otp_attempts = db.Column(db.Integer, nullable=False, default=0)
    otp_last_sent_at = db.Column(db.DateTime, nullable=True)
    daily_reminder_enabled = db.Column(db.Boolean, nullable=False, default=False)
    daily_reminder_time = db.Column(db.Time, nullable=False, default=time(8, 0))
    timezone = db.Column(db.String(80), nullable=False, default="Asia/Kolkata")
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class StudyTask(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True)
    title = db.Column(db.String(160), nullable=False)
    subject = db.Column(db.String(100), nullable=False, default="General")
    due_date = db.Column(db.Date, nullable=False)
    due_time = db.Column(db.Time, nullable=False, default=time(23, 59))
    reminder_at = db.Column(db.DateTime, nullable=True)
    duration = db.Column(db.Integer, nullable=False, default=60)
    priority = db.Column(db.String(20), nullable=False, default="Medium")
    status = db.Column(db.String(20), nullable=False, default="Pending")
    notes = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    @property
    def is_overdue(self):
        deadline = datetime.combine(self.due_date, self.due_time)
        return self.status != "Completed" and deadline < datetime.now()

    @property
    def priority_score(self):
        return {"High": 3, "Medium": 2, "Low": 1}.get(self.priority, 2)


class Reminder(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True)
    task_id = db.Column(db.Integer, db.ForeignKey("study_task.id", ondelete="CASCADE"), nullable=False, index=True)
    recipient = db.Column(db.String(254), nullable=False)
    scheduled_at = db.Column(db.DateTime, nullable=False, index=True)
    status = db.Column(db.String(20), nullable=False, default="scheduled", index=True)
    sent_at = db.Column(db.DateTime, nullable=True)
    attempts = db.Column(db.Integer, nullable=False, default=0)
    last_error = db.Column(db.String(1000), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class DailyReminderLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True)
    local_date = db.Column(db.String(10), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    __table_args__ = (db.UniqueConstraint("user_id", "local_date", name="uq_daily_reminder_user_date"),)


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=os.getenv("FLASK_DEBUG", "false").lower() == "true")
