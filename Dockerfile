FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY gwt ./gwt
RUN pip install . \
    && useradd --create-home --uid 1000 gwt \
    && mkdir -p /app/data \
    && chown gwt:gwt /app/data

USER gwt
VOLUME ["/app/data"]

ENTRYPOINT ["python", "-m", "gwt"]
CMD ["sweep", "--days", "365"]
