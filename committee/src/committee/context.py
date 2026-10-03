"""Application context: project root, validated config, resolved paths and secrets.

Built once per command and passed explicitly (no global state).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from committee.config.loader import default_root, load_config
from committee.config.schema import AppConfig
from committee.config.secrets import Secrets


@dataclass(frozen=True)
class AppContext:
    root: Path
    config: AppConfig
    secrets: Secrets

    @classmethod
    def load(cls, root: Path | None = None) -> AppContext:
        root = (root or default_root()).resolve()
        cfg = load_config(root / "config")
        return cls(root=root, config=cfg, secrets=Secrets(env_file=root / ".env"))

    @property
    def config_dir(self) -> Path:
        return self.root / "config"

    def path(self, rel: str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else self.root / p

    @property
    def journal_db(self) -> Path:
        return self.path(self.config.app.paths.journal_db)

    @property
    def state_db(self) -> Path:
        return self.path(self.config.app.paths.state_db)

    @property
    def data_dir(self) -> Path:
        return self.path(self.config.app.paths.data_dir)

    @property
    def raw_dir(self) -> Path:
        return self.path(self.config.app.paths.raw_dir)
