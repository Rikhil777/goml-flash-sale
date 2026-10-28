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
COPY --chown=app:app scripts/render_start.sh ./scripts/render_start.sh
RUN mkdir -p /app/staticfiles \
    && chown app:app /app/staticfiles \
    && python manage.py check

USER app
EXPOSE 8000

CMD ["sh", "/app/scripts/render_start.sh"]
