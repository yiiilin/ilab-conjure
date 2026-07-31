FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app

WORKDIR /app

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin app

COPY requirements-webui.txt ./
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir -r requirements-webui.txt

COPY --chown=app:app codex_image/ ./codex_image/
COPY --chown=app:app pyproject.toml LICENSE README.md ./

RUN mkdir -p /app/output && chown -R app:app /app/output

USER app
EXPOSE 8787

CMD ["python", "-m", "uvicorn", "codex_image.webui.app:app", "--host", "0.0.0.0", "--port", "8787", "--proxy-headers", "--forwarded-allow-ips=*", "--no-access-log"]
