"""Celery worker and durable reminder dispatch. Email uses Brevo HTTPS API, not SMTP."""
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests
from celery import Celery
from dotenv import load_dotenv

load_dotenv()

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

celery = Celery(
    "study_scheduler",
    broker=REDIS_URL,
    backend=REDIS_URL,
)

celery.conf.update(
    timezone="UTC",
    enable_utc=True,

    beat_schedule={
        "dispatch-due-reminders-every-minute": {
            "task": "tasks.dispatch_due_reminders",
            "schedule": 60.0,
        },
    },

    task_acks_late=True,
    worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
    result_expires=3600,
)

# Configure TLS certificate verification for Upstash Redis.
if REDIS_URL.startswith("rediss://"):
    celery.conf.update(
        broker_use_ssl={
            "ssl_cert_reqs": "required",
        },
        redis_backend_use_ssl={
            "ssl_cert_reqs": "required",
        },
    )

def _app_and_models():
    from app import create_app, db, User, StudyTask, Reminder, DailyReminderLog
    return create_app(), db, User, StudyTask, Reminder, DailyReminderLog


def _send_email(recipient, subject, html, text_body):
    api_key = os.getenv("BREVO_API_KEY", "").strip()
    sender_email = os.getenv("EMAIL_FROM", "").strip()
    sender_name = os.getenv("EMAIL_FROM_NAME", "TaskScheduler and DeadlineManager").strip()
    if not api_key or not sender_email:
        raise RuntimeError("Set BREVO_API_KEY and EMAIL_FROM. Verify the sender in Brevo before production sending.")
    response = requests.post(
        "https://api.brevo.com/v3/smtp/email",
        headers={"accept": "application/json", "api-key": api_key, "content-type": "application/json"},
        json={"sender": {"name": sender_name, "email": sender_email},
              "to": [{"email": recipient}], "subject": subject,
              "htmlContent": html, "textContent": text_body},
        timeout=20,
    )
    if response.status_code not in (200, 201, 202):
        raise RuntimeError(f"Brevo API returned HTTP {response.status_code}: {response.text[:500]}")
    return response.json() if response.content else {}


@celery.task(name="tasks.send_reminder", bind=True, autoretry_for=(Exception,), retry_backoff=True, retry_jitter=True, max_retries=4)
def send_reminder(self, reminder_id):
    app, db, User, StudyTask, Reminder, DailyReminderLog = _app_and_models()
    with app.app_context():
        reminder = db.session.get(Reminder, reminder_id)
        if not reminder or reminder.status == "sent":
            return "Reminder no longer needs sending."
        task = db.session.get(StudyTask, reminder.task_id)
        if not task:
            reminder.status = "cancelled"
            db.session.commit()
            return "Task deleted; reminder cancelled."
        if task.status == "Completed":
            reminder.status = "cancelled"
            reminder.last_error = None
            db.session.commit()
            return "Task completed; reminder cancelled without sending."
        if reminder.status in {"sent", "cancelled"}:
            return "Reminder no longer needs sending."
        due = f"{task.due_date:%d %b %Y} at {task.due_time:%H:%M}"
        subject = f"Study reminder: {task.title}"
        text_body = (f"TaskScheduler and DeadlineManager reminder\n\nTask: {task.title}\nSubject: {task.subject}\n"
                     f"Deadline: {due}\nPriority: {task.priority}\n\nOpen your scheduler to review your plan.")
        html = ("<div style='font-family:Arial,sans-serif;line-height:1.6;color:#24233a'>"
                "<h2>Your TaskScheduler and DeadlineManager reminder</h2><p>This is a reminder about your task.</p>"
                f"<p><b>Task:</b> { _escape(task.title) }<br><b>Subject:</b> {_escape(task.subject)}<br>"
                f"<b>Deadline:</b> {_escape(due)}<br><b>Priority:</b> {_escape(task.priority)}</p>"
                "<p>Plan a focused session and take a short break afterward.</p></div>")
        try:
            _send_email(reminder.recipient, subject, html, text_body)
            reminder.status = "sent"
            reminder.sent_at = datetime.utcnow()
            reminder.attempts += 1
            reminder.last_error = None
            db.session.commit()
            return f"Reminder {reminder_id} sent."
        except Exception as exc:
            reminder.status = "queued" if self.request.retries < self.max_retries else "failed"
            reminder.attempts += 1
            reminder.last_error = str(exc)[:1000]
            db.session.commit()
            raise

@celery.task(
    name="tasks.send_daily_digest",
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    max_retries=3,
)
def send_daily_digest(self, user_id, local_date):
    app, db, User, StudyTask, Reminder, DailyReminderLog = _app_and_models()

    with app.app_context():
        user = db.session.get(User, user_id)

        if not user or not user.daily_reminder_enabled:
            return "Daily reminders disabled."

        pending_tasks = (
            StudyTask.query
            .filter_by(user_id=user.id, status="Pending")
            .order_by(StudyTask.due_date, StudyTask.due_time)
            .all()
        )

        if not pending_tasks:
            return "No pending tasks; no email sent."

        lines = [
            f"- {t.title} ({t.subject}) — due "
            f"{t.due_date:%d %b %Y} {t.due_time:%H:%M}, "
            f"{t.priority} priority"
            for t in pending_tasks[:30]
        ]

        text_body = (
            f"Hello, {user.name}!\n\n"
            f"Here are your pending study tasks for {local_date}:\n\n"
            + "\n".join(lines)
        )

        html_items = "".join(
            f"<li><b>{_escape(t.title)}</b> "
            f"({_escape(t.subject)}) — due "
            f"{t.due_date:%d %b %Y} {t.due_time:%H:%M}, "
            f"{_escape(t.priority)} priority</li>"
            for t in pending_tasks[:30]
        )

        html = (
            "<div style='font-family:Arial,sans-serif;line-height:1.6'>"
            f"<h2>Hello, {_escape(user.name)}!</h2>"
            f"<p>Your pending study tasks for {_escape(local_date)}:</p>"
            f"<ul>{html_items}</ul></div>"
        )

        _send_email(
            user.email,
            "Your daily TaskScheduler and DeadlineManager study plan",
            html,
            text_body,
        )

        return f"Daily digest sent to {user.email}."


@celery.task(name="tasks.dispatch_due_reminders")
def dispatch_due_reminders():
    """Create missing deadline reminders, cancel completed tasks' reminders,
    and dispatch reminders whose scheduled times have arrived.
    """
    app, db, User, StudyTask, Reminder, DailyReminderLog = _app_and_models()
    dispatched = 0
    auto_created = 0

    with app.app_context():
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)

        # Create missing automatic deadline reminders.
        pending_tasks = StudyTask.query.filter_by(status="Pending").all()

        for study_task in pending_tasks:
            user = db.session.get(User, study_task.user_id)

            if not user or not user.email:
                continue

            try:
                user_tz = ZoneInfo(user.timezone or "Asia/Kolkata")
            except Exception:
                user_tz = ZoneInfo("UTC")

            deadline_local = datetime.combine(
                study_task.due_date,
                study_task.due_time,
            ).replace(tzinfo=user_tz)

            deadline_utc = deadline_local.astimezone(timezone.utc).replace(
                tzinfo=None
            )

            if deadline_utc <= now_utc:
                continue

            existing = Reminder.query.filter_by(
                task_id=study_task.id,
                scheduled_at=deadline_utc,
            ).first()

            if existing:
                continue

            db.session.add(
                Reminder(
                    user_id=user.id,
                    task_id=study_task.id,
                    recipient=user.email,
                    scheduled_at=deadline_utc,
                    status="scheduled",
                )
            )
            auto_created += 1

        if auto_created:
            db.session.commit()

        # Cancel unsent reminders for completed tasks.
        completed_ids = [
            row.id
            for row in StudyTask.query.filter_by(status="Completed").all()
        ]

        if completed_ids:
            (
                Reminder.query
                .filter(
                    Reminder.task_id.in_(completed_ids),
                    Reminder.status.in_(["scheduled", "queued"]),
                )
                .update(
                    {"status": "cancelled"},
                    synchronize_session=False,
                )
            )
            db.session.commit()

        # Dispatch reminders whose scheduled time has arrived.
        due_reminders = (
            Reminder.query
            .join(StudyTask, Reminder.task_id == StudyTask.id)
            .filter(
                Reminder.status == "scheduled",
                Reminder.scheduled_at <= now_utc,
                StudyTask.status != "Completed",
            )
            .order_by(Reminder.scheduled_at)
            .limit(200)
            .all()
        )

        for reminder in due_reminders:
            reminder.status = "queued"
            db.session.commit()

            try:
                send_reminder.delay(reminder.id)
                dispatched += 1
            except Exception:
                reminder.status = "scheduled"
                db.session.commit()
                raise

        # Queue each user's daily digest once per local date.
        users = User.query.filter_by(daily_reminder_enabled=True).all()
        utc_now = datetime.now(timezone.utc)

        for user in users:
            try:
                user_tz = ZoneInfo(user.timezone or "Asia/Kolkata")
            except Exception:
                user_tz = ZoneInfo("UTC")

            local_now = utc_now.astimezone(user_tz)
            daily_time = user.daily_reminder_time

            if not daily_time:
                continue

            if (
                local_now.hour,
                local_now.minute,
            ) != (
                daily_time.hour,
                daily_time.minute,
            ):
                continue

            day = local_now.date().isoformat()

            if DailyReminderLog.query.filter_by(
                user_id=user.id,
                local_date=day,
            ).first():
                continue

            db.session.add(
                DailyReminderLog(user_id=user.id, local_date=day)
            )

            try:
                db.session.commit()
                send_daily_digest.delay(user.id, day)
                dispatched += 1
            except Exception:
                db.session.rollback()
                DailyReminderLog.query.filter_by(
                    user_id=user.id,
                    local_date=day,
                ).delete(synchronize_session=False)
                db.session.commit()
                raise

    return {
        "auto_created": auto_created,
        "dispatched": dispatched,
    }

def _escape(value):
    from html import escape
    return escape(str(value), quote=True)
