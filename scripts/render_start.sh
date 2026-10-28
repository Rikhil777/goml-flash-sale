#!/bin/sh
set -eu

python manage.py migrate --noinput
python manage.py collectstatic --noinput

celery -A config worker \
    --loglevel=INFO \
    --concurrency="${CELERY_WORKER_CONCURRENCY:-1}" &

exec gunicorn config.wsgi:application \
    --bind "0.0.0.0:${PORT:-10000}" \
    --workers "${GUNICORN_WORKERS:-1}" \
    --timeout "${GUNICORN_TIMEOUT:-30}" \
    --access-logfile - \
    --error-logfile -
