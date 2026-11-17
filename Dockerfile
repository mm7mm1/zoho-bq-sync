FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first so code changes do not invalidate the dependency layer.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-deps . \
    && useradd --create-home --uid 10001 app

USER app

# Cloud Run Job runs `python -m zoho_bq_sync sync`; override args for discover/freshness.
ENTRYPOINT ["python", "-m", "zoho_bq_sync"]
CMD ["sync"]
