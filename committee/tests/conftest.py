from __future__ import annotations

import shutil
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A throwaway project root with a copy of the real config."""
    shutil.copytree(PROJECT_ROOT / "config", tmp_path / "config")
    shutil.copy(PROJECT_ROOT / "pyproject.toml", tmp_path / "pyproject.toml")
    if (PROJECT_ROOT / "prompts").exists():
        shutil.copytree(PROJECT_ROOT / "prompts", tmp_path / "prompts")
    return tmp_path


@pytest.fixture
def ctx(project: Path):  # type: ignore[no-untyped-def]
    from committee.context import AppContext

    return AppContext.load(project)
