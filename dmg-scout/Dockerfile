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

# Build the stylesheet IN THE IMAGE, from the templates in the image.
#
# app/web/static/app.css is a build artefact, and the previous arrangement had it
# existing only on whoever last ran `make css`. Two ways that fails in
# production and both are silent: the directory is untracked, so a clean checkout
# deploys a container with no stylesheet at all and every page renders as raw
# HTML; and if it were committed, a template edit without a rebuild would ship
# CSS that does not match its own markup — Tailwind only emits utilities it can
# see used, so the classes simply would not exist.
#
# Building here makes the stylesheet a function of the templates beside it. The
# version is pinned for the same reason the Makefile pins it: a rebuild months
# from now must not silently produce different output from the one reviewed.
ARG TAILWIND_VERSION=v4.3.3
RUN curl -sL -o /usr/local/bin/tailwindcss \
      "https://github.com/tailwindlabs/tailwindcss/releases/download/${TAILWIND_VERSION}/tailwindcss-linux-x64" \
    && chmod +x /usr/local/bin/tailwindcss \
    && tailwindcss -i app/web/styles/app.css -o app/web/static/app.css --minify \
    && rm /usr/local/bin/tailwindcss \
    && test -s app/web/static/app.css

RUN pip install --no-cache-dir .

EXPOSE 8000

# Web service default; cron overrides with `scout pipeline`
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.web.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
