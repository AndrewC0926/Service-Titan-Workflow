.PHONY: install dev test lint run pipeline fetch triage extract resolve score notify verify migrate revision

VENV ?= .venv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip

install:
	python3 -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -e ".[dev]"

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
