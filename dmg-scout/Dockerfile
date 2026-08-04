FROM python:3.12-slim

WORKDIR /srv/dmg-scout

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 gcc libpq-dev postgresql-client curl unzip bash \
    && rm -rf /var/lib/apt/lists/* \
    && curl -sS "https://awscli.amazonaws.com/awscli-exe-linux-$(uname -m).zip" -o /tmp/awscli.zip \
    && unzip -q /tmp/awscli.zip -d /tmp && /tmp/aws/install && rm -rf /tmp/aws /tmp/awscli.zip

COPY pyproject.toml ./
COPY app ./app
COPY alembic ./alembic
COPY scripts ./scripts
COPY alembic.ini config.yaml ./

RUN pip install --no-cache-dir .

EXPOSE 8000

# Web service default; cron overrides with `scout pipeline`
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.web.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
