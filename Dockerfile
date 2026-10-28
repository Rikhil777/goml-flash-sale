FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN groupadd --system app \
    && useradd --system --gid app --create-home --home-dir /app app

WORKDIR /app

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY --chown=app:app manage.py ./
COPY --chown=app:app config ./config
COPY --chown=app:app flashsale ./flashsale
RUN mkdir -p /app/staticfiles \
    && chown app:app /app/staticfiles \
    && python manage.py check

USER app
EXPOSE 8000

CMD ["sh", "-c", "exec gunicorn config.wsgi:application --bind 0.0.0.0:8000 --workers ${GUNICORN_WORKERS:-4} --timeout ${GUNICORN_TIMEOUT:-30} --access-logfile - --error-logfile -"]
