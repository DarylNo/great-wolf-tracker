FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    GWT_DB=/app/data/gwl.db \
    GWT_PORT=8080 \
    GWT_TZ=America/Toronto

WORKDIR /app

# Exact dependency versions from the lock file (its own layer, so code changes don't
# reinstall them), then the app itself without re-resolving anything. `pip check` fails
# the build if the lock no longer satisfies pyproject.toml.
COPY requirements.lock ./
RUN pip install -r requirements.lock
COPY pyproject.toml README.md LICENSE ./
COPY gwt ./gwt
RUN pip install --no-deps . \
    && pip check \
    && useradd --create-home --uid 1000 gwt \
    && mkdir -p /app/data \
    && chown gwt:gwt /app/data

USER gwt
VOLUME ["/app/data"]
EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('GWT_PORT', '8080'), timeout=4)" || exit 1

# Web dashboard + in-process sweep scheduler. One-off sweeps still work:
#   docker compose exec gwt python -m gwt sweep --days 38
ENTRYPOINT ["python", "-m", "gwt"]
CMD ["serve"]
