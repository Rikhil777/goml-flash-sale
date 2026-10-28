#!/bin/sh
set -eu

python manage.py migrate --noinput
python manage.py initialize_sale

product_id=$(python manage.py shell --no-imports -c "from flashsale.models import Product; print(Product.objects.only('id').get(name='Flash Sale Smartphone').pk)")
python manage.py sync_redis_inventory --product-id "$product_id"

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
