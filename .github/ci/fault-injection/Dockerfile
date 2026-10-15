FROM python:3.12.14-slim@sha256:eeb8088e67610b37583880c7627e3931f087cba55a35810819e34a398f624a47
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app
WORKDIR /app
COPY requirements.txt /app/requirements-server.txt
RUN pip install --no-cache-dir -r /app/requirements-server.txt
COPY teledrive /app/teledrive
COPY scripts /app/scripts
COPY tests /app/tests
USER 10001:10001
