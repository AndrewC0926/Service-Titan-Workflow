FROM python:3.12-slim

WORKDIR /srv/dmg-scout

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 gcc libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini config.yaml ./

RUN pip install --no-cache-dir .

EXPOSE 8000

# Web service default; cron overrides with `scout pipeline`
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.web.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
