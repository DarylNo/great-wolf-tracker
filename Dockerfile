FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    GWT_DB=/app/data/gwl.db \
    GWT_PORT=8080 \
    GWT_TZ=America/Toronto

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY gwt ./gwt
RUN pip install . \
    && useradd --create-home --uid 1000 gwt \
    && mkdir -p /app/data \
    && chown gwt:gwt /app/data

USER gwt
VOLUME ["/app/data"]
EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('GWT_PORT', '8080'), timeout=4)" || exit 1

# Web dashboard + in-process sweep scheduler. One-off sweeps still work:
#   docker compose exec gwt python -m gwt sweep --days 30
ENTRYPOINT ["python", "-m", "gwt"]
CMD ["serve"]
