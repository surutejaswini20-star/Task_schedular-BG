# TaskScheduler: Render Free Web Service (combined processes)

This package runs Gunicorn (Flask), a Celery worker, and Celery Beat inside ONE Render web service using Honcho. It avoids creating paid Render Background Worker services, but it is a best-effort workaround, NOT reliable always-on hosting. Render Free web services can spin down after inactivity; during sleep/restarts, Celery tasks and reminders may be delayed. External pings do not guarantee uptime and should not be treated as a production reliability solution.

## 1. Prepare the project
1. Extract this ZIP.
2. Push the contents of `taskscheduler_render_free` to a GitHub repository (root should contain `app.py`, `tasks.py`, `requirements.txt`, `Procfile`, and `render.yaml`).
3. Ensure `.env` is NOT committed. This ZIP intentionally excludes `.env` and virtual environments.

## 2. Create one Render Web Service
1. Open https://dashboard.render.com/ and choose **New + → Web Service**.
2. Connect the GitHub repository and branch.
3. Set Runtime to Python.
4. Build command: `pip install -r requirements.txt`
5. Start command: `honcho start`
6. Select the **Free** instance plan. Do NOT create separate Background Worker services for this combined setup.

If using Render Blueprint, the included `render.yaml` describes one free web service. Review it before applying it; it may create a new service rather than modify your existing one.

## 3. Set environment variables in Render
In the web service → Environment, set:
- `SECRET_KEY`: strong random secret (do not reuse a public/example value).
- `DATABASE_URL`: Neon PostgreSQL connection URL; use SSL as required by Neon.
- `REDIS_URL`: Upstash **Redis TCP/TLS URL**, normally beginning `rediss://`; do NOT use the Upstash REST URL.
- `BREVO_API_KEY`: Brevo transactional API key.
- `EMAIL_FROM`: sender email verified/authorized in Brevo.
- `EMAIL_FROM_NAME`: `TaskScheduler and DeadlineManager` (optional; default supplied).
- `COOKIE_SECURE`: `true` for HTTPS deployment.
- `TRUST_PROXY`: `true`.

Do not put quotation marks around values in Render's environment variable fields. If a URL password contains reserved characters (`@`, `:`, `/`, `#`, `%`), URL-encode them.

## 4. Check deploy logs
After deployment, open the service Logs. Honcho should start three processes named `web`, `worker`, and `beat`. Confirm Gunicorn binds to `$PORT`, the worker reports `ready`, and Beat logs periodic scheduling. If any process exits, Honcho should stop the service so Render can restart it; inspect the first traceback.

## 5. Test reminder email end-to-end
1. Confirm a test email can be sent via Brevo and the sender is verified.
2. Create a test account/task with a future deadline and reminder configured.
3. Check worker logs for `tasks.dispatch_due_reminders` and `tasks.send_reminder`.
4. Check Brevo → Transactional → Logs for the recipient and delivery status.
5. Check spam/junk if Brevo reports delivered.

## Important behavior and limitations
- This package keeps one Celery Beat instance and uses `/tmp/celerybeat-schedule` to avoid trying to write Beat state into a read-only application directory. The schedule file is ephemeral; Beat will recreate it after restarts.
- Render Free web services may sleep after inactivity and can restart or be redeployed. While asleep, the worker and Beat process are not running. A pinging service is not a guarantee and may be subject to provider terms. Reminders can be late or missed.
- For reliable time-sensitive reminders, use an always-on VM or a paid background worker, and consider idempotent reminder dispatch and monitoring.
- Do not run this combined configuration at the same time as separate worker/Beat instances against the same queue; that can cause duplicate scheduling/processing.
- `psycopg[binary]` is pinned to 3.2.13 because the previous 3.2.9 pin failed to resolve in your Render build.
