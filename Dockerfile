FROM python:3.12.14-slim@sha256:eeb8088e67610b37583880c7627e3931f087cba55a35810819e34a398f624a47

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TELEDRIVE_CONFIG=/config/config.yaml \
    TELEDRIVE_AUTH_DIR=/data/auth

RUN groupadd --gid 10001 teledrive && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin teledrive

WORKDIR /app

# Apply base-image security updates available from Debian before installing Python deps.
RUN apt-get update \
 && apt-get upgrade -y --no-install-recommends \
 && rm -rf /var/lib/apt/lists/*
COPY requirements-web.txt /app/requirements-web.txt
ARG TELEDRIVE_DB_EXTRA_PACKAGES=""
RUN pip install --no-cache-dir -r /app/requirements-web.txt \
 && if [ -n "$TELEDRIVE_DB_EXTRA_PACKAGES" ]; then pip install --no-cache-dir $TELEDRIVE_DB_EXTRA_PACKAGES; fi

COPY db.py uploader.py /app/
COPY web /app/web
RUN mkdir -p /config /data/sessions /data/auth /data/tmp /uploads \
 && chown -R 10001:10001 /config /data /uploads /app

USER 10001:10001

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=3)" || exit 1

CMD ["uvicorn","web.app:app","--host","0.0.0.0","--port","8080","--workers","1","--no-server-header"]
