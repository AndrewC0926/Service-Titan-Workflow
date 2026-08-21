FROM python:3.12-slim

WORKDIR /srv/dmg-scout

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 gcc libpq-dev postgresql-client curl unzip bash \
    && rm -rf /var/lib/apt/lists/* \
    && curl -sS "https://awscli.amazonaws.com/awscli-exe-linux-$(uname -m).zip" -o /tmp/awscli.zip \
    && unzip -q /tmp/awscli.zip -d /tmp && /tmp/aws/install && rm -rf /tmp/aws /tmp/awscli.zip

COPY pyproject.toml requirements.lock.txt ./
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

# Two steps on purpose. requirements.lock.txt exact-pins the full
# transitive closure -- install it FIRST so every dependency version is
# exactly what was tested, then install the local package with --no-deps
# so it registers the `dmg-scout` package and `scout` console script
# without re-resolving (and potentially drifting) anything the lock already
# pinned. `pip install .` alone re-resolves fresh from pyproject.toml's
# bare version floors on every build regardless of whether any dependency
# actually changed -- that silent redrift is what let anthropic 1.0.0 (a
# breaking major release that dropped Messages.create's temperature kwarg)
# land in production 2026-08-20 with no code of ours involved. See
# requirements.lock.txt's own header for how to regenerate it deliberately.
#
# --retries/--timeout: this exact step has now failed on three separate
# deploys (2026-08-19), always the same transient files.pythonhosted.org
# 502 during dependency resolution, always fixed by a bare retry via the
# Render API -- pip's own defaults (5 retries, 15s timeout) apparently
# aren't enough headroom against that edge's flakiness. This does not fix
# that root cause, only gives a flaky single request more room to succeed
# without a human having to notice the build failed and manually retry it.
RUN pip install --no-cache-dir --retries 10 --timeout 60 -r requirements.lock.txt \
    && pip install --no-cache-dir --no-deps .

# `pip install .` (no -e) COPIES app/ into site-packages -- a real,
# separate copy, permanently divorced from /srv/dmg-scout. app/config.py's
# DEFAULT_CONFIG is Path(__file__).resolve().parent.parent / "config.yaml",
# so any process that imports app.config via that installed copy resolves
# to site-packages/config.yaml, which was never put there and does not
# exist. The web service never shows this: `uvicorn app.web.main:app`
# resolves the `app` import via CWD-based sys.path (a real app/ source dir
# sits right here in WORKDIR), finding config.yaml correctly by accident.
# `scout` (the pip-installed console-script entry point, [project.scripts]
# in pyproject.toml) always imports via its installed package location
# instead, regardless of CWD -- confirmed by a real production traceback,
# FileNotFoundError against /usr/local/lib/python3.12/site-packages/
# config.yaml, from a Render Jobs API-triggered cron run (2026-08-13).
# SCOUT_CONFIG is load_config()'s own documented override (app/config.py's
# CONFIG_PATH_ENV) -- setting it here fixes every process in this image,
# not just the cron's, and doesn't depend on getting import-path behavior
# right for whichever entry point happens to run.
ENV SCOUT_CONFIG=/srv/dmg-scout/config.yaml

EXPOSE 8000

# Web service default. The cron (dmg-scout-pipeline, render.yaml) overrides
# this CMD entirely with `dockerCommand: scripts/run-pipeline.sh` -- a bare
# script path, not a multi-word shell string, on purpose. See that script's
# header comment for why.
#
# --forwarded-allow-ips='*': Render terminates TLS at its edge and forwards to
# this container over plain HTTP from a non-loopback address. Uvicorn only
# trusts X-Forwarded-Proto from proxies in forwarded_allow_ips, which defaults
# to 127.0.0.1 — nothing Render sends matches that, so request.url_for() (what
# base.html's stylesheet <link> is built from) stamped scheme=http on a page
# served over https. The browser silently blocks that as mixed content: no
# server error, no build error, CSS serves fine on its own URL, page renders
# naked. Render's edge is the only thing that can reach this container, so
# trusting it here is safe.
CMD ["sh", "-c", "scout check-migrations && alembic upgrade head && uvicorn app.web.main:app --host 0.0.0.0 --port ${PORT:-8000} --forwarded-allow-ips='*'"]
