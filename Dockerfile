FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/data

# tzdata so that TZ=Europe/Prague etc. works for the times shown in the UI
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# non-root user that owns /data
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --home-dir /srv --shell /usr/sbin/nologin app \
    && mkdir -p /data \
    && chown app:app /data

COPY app ./app
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
# a checkout on Windows (autocrlf) turns the script into CRLF -> "exec ...: no such file or directory"; also ensure +x
RUN sed -i 's/\r$//' /usr/local/bin/docker-entrypoint.sh && chmod 0755 /usr/local/bin/docker-entrypoint.sh

VOLUME /data
EXPOSE 5001

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:5001/health', timeout=4).status == 200 else 1)"]

# starts as root only to fix ownership of /data, then runs the app as user "app"
ENTRYPOINT ["docker-entrypoint.sh"]
# exactly one worker process: the sync worker thread must exist once
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "5001"]
