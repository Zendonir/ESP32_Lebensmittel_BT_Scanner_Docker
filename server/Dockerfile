# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Abhaengigkeiten zuerst: eine Codeaenderung invalidiert diesen Layer nicht.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY web ./web

# Datenverzeichnis fuer SQLite. Der Container laeuft nicht als root.
RUN mkdir -p /data && \
    useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin scanner && \
    chown -R scanner:scanner /data /app
USER scanner

ARG APP_VERSION=dev
ENV APP_VERSION=${APP_VERSION}

VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=4).status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--forwarded-allow-ips", "*"]
