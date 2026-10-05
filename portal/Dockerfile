# Pinned by digest: bump deliberately (docker buildx imagetools inspect python:3.13-slim).
FROM python:3.13-slim@sha256:bb2988715db2cf7ace7b53f38f3cffbef7c7046a656bee66245eb0ed386e2e81
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN useradd --uid 1000 --create-home app
# collector tools: ssh (NAS stats, restricted key), ping (reachability), msmtp (sends the portal's email outbox)
RUN apt-get update && apt-get install -y --no-install-recommends openssh-client iputils-ping msmtp && rm -rf /var/lib/apt/lists/*
# Ookla's official speedtest CLI (the old speedtest-cli picked servers in Brazil). Pinned version, checksum verified.
RUN python -c "import urllib.request as u,hashlib as h;d=u.urlopen('https://install.speedtest.net/app/cli/ookla-speedtest-1.2.0-linux-x86_64.tgz',timeout=60).read();assert h.sha256(d).hexdigest()=='5690596c54ff9bed63fa3732f818a05dbc2db19ad36ed68f21ca5f64d5cfeeb7';open('/tmp/o.tgz','wb').write(d)"  && tar -xzf /tmp/o.tgz -C /usr/local/bin speedtest && rm /tmp/o.tgz
WORKDIR /app
COPY requirements.txt .
# requirements.txt is compiled with hashes (see requirements.in): pip refuses any package whose hash differs
RUN pip install --no-cache-dir --require-hashes -r requirements.txt
COPY app ./app
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"
CMD ["uvicorn", "app.asgi:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
# Only Caddy may set X-Forwarded-* : compose sets FORWARDED_ALLOW_IPS to its fixed address (unset = loopback only)
