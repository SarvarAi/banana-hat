# One image, three jobs: the web app, the background worker, and one-off management
# commands. docker-compose.yml picks the job with the command argument.

FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DJANGO_SETTINGS_MODULE=bananahat.settings \
    DATA_DIR=/data

WORKDIR /app

RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --home-dir /app app \
    && mkdir -p /data \
    && chown app:app /data

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY src/ ./src/
COPY fixtures.json ./fixtures.json
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh

# Static files are collected at build time so the running container never writes to
# its own filesystem outside /data.
RUN SECRET_KEY=collectstatic-only python src/manage.py collectstatic --noinput --verbosity 0

USER app
EXPOSE 8000
VOLUME ["/data"]

HEALTHCHECK --interval=5s --timeout=3s --start-period=20s --retries=12 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status == 200 else 1)"

ENTRYPOINT ["sh", "/usr/local/bin/entrypoint.sh"]
CMD ["web"]
