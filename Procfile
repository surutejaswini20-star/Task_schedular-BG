web: python -m gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --timeout 60
worker: python -m celery -A tasks.celery worker --loglevel=INFO --concurrency=1
beat: python -m celery -A tasks.celery beat --loglevel=INFO --schedule=/tmp/celerybeat-schedule
