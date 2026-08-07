.PHONY: install dev test lint run css css-watch tailwind pipeline fetch triage extract resolve score notify verify migrate revision

VENV ?= .venv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip

# Tailwind v4 standalone binary — no Node, no package.json, no lockfile in a
# Python service. Pinned so a CSS rebuild on another machine cannot silently
# produce different output from the version that was reviewed.
TAILWIND_VERSION ?= v4.3.3
TAILWIND := tools/tailwindcss

install:
	python3 -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -e ".[dev]"
	$(MAKE) tailwind css

tailwind:
	mkdir -p tools
	curl -sL -o $(TAILWIND) https://github.com/tailwindlabs/tailwindcss/releases/download/$(TAILWIND_VERSION)/tailwindcss-linux-x64
	chmod +x $(TAILWIND)

# app/web/static/app.css is a BUILD ARTEFACT. Rebuild it after touching any
# template — Tailwind only emits utilities it can see used.
css:
	$(TAILWIND) -i app/web/styles/app.css -o app/web/static/app.css --minify

css-watch:
	$(TAILWIND) -i app/web/styles/app.css -o app/web/static/app.css --watch

test:
	$(PY) -m pytest tests/ -q

lint:
	$(VENV)/bin/ruff check app tests

run:
	$(PY) -m uvicorn app.web.main:app --reload --port 8000

migrate:
	$(VENV)/bin/alembic upgrade head

revision:
	$(VENV)/bin/alembic revision --autogenerate -m "$(m)"

pipeline:
	$(VENV)/bin/scout pipeline

fetch triage extract resolve score notify:
	$(VENV)/bin/scout $@

verify:
	$(VENV)/bin/scout verify-sources
