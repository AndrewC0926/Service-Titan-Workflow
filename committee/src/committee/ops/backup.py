"""Encrypted nightly backups of the lake, raw zone, journal and state DBs, plus a
restore test that proves a backup can be read back and the journal verifies.

Format: tar.gz of var/ (SQLite files copied with the online backup API so a
backup taken mid-write is consistent), encrypted with Fernet (AES-128-CBC +
HMAC) under a key derived from BACKUP_PASSPHRASE via scrypt. The salt is stored
in the file header.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import io
import os
import sqlite3
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

MAGIC = b"CMTBK1"
SALT_LEN = 16
SKIP_DIRS = frozenset({"backups", "flags"})


def _key(passphrase: str, salt: bytes) -> bytes:
    kdf = Scrypt(salt=salt, length=32, n=2**15, r=8, p=1)
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode()))


def _add_sqlite(tar: tarfile.TarFile, path: Path, arcname: str) -> None:
    with tempfile.TemporaryDirectory() as td:
        copy = Path(td) / path.name
        src = sqlite3.connect(path)
        dst = sqlite3.connect(copy)
        with dst:
            src.backup(dst)
        src.close()
        dst.close()
        tar.add(copy, arcname=arcname)


def create_backup(
    var_dir: Path, out_dir: Path, passphrase: str, now: dt.datetime | None = None
) -> Path:
    if not passphrase:
        raise ValueError("BACKUP_PASSPHRASE is required")
    now = now or dt.datetime.now(dt.UTC)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for p in sorted(var_dir.rglob("*")):
            rel = p.relative_to(var_dir)
            if (
                not p.is_file()
                or rel.parts[0] in SKIP_DIRS
                or p.suffix in (".sqlite-wal", ".sqlite-shm")
                or p.name.endswith(("-wal", "-shm"))
            ):
                continue
            if p.suffix == ".sqlite":
                _add_sqlite(tar, p, str(rel))
            else:
                tar.add(p, arcname=str(rel))
    salt = os.urandom(SALT_LEN)
    token = Fernet(_key(passphrase, salt)).encrypt(buf.getvalue())
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"committee-{now.strftime('%Y%m%dT%H%M%SZ')}.bak"
    out.write_bytes(MAGIC + salt + token)
    return out


def decrypt_backup(path: Path, passphrase: str) -> bytes:
    data = path.read_bytes()
    if not data.startswith(MAGIC):
        raise ValueError(f"{path.name} is not a Committee backup")
    salt = data[len(MAGIC) : len(MAGIC) + SALT_LEN]
    try:
        return Fernet(_key(passphrase, salt)).decrypt(data[len(MAGIC) + SALT_LEN :])
    except InvalidToken:
        raise ValueError("wrong passphrase or corrupted backup") from None


def restore_backup(path: Path, passphrase: str, dest: Path) -> list[str]:
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(decrypt_backup(path, passphrase)), mode="r:gz") as tar:
        names = tar.getnames()
        tar.extractall(dest, filter="data")
    return names


@dataclass(frozen=True)
class RestoreTest:
    backup: str
    ok: bool
    files: int
    journal_entries: int
    journal_ok: bool
    sha256: str
    detail: str


def restore_test(path: Path, passphrase: str, journal_name: str = "journal.sqlite") -> RestoreTest:
    """Restore into a temp dir and verify the journal chain. Used quarterly (DESIGN 13)."""
    from committee.journal.store import Journal

    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    with tempfile.TemporaryDirectory() as td:
        try:
            names = restore_backup(path, passphrase, Path(td))
        except ValueError as e:
            return RestoreTest(path.name, False, 0, 0, False, digest, str(e))
        jp = Path(td) / journal_name
        if not jp.exists():
            return RestoreTest(
                path.name, False, len(names), 0, False, digest, "journal missing from backup"
            )
        with Journal(jp) as j:
            rep = j.verify()
        return RestoreTest(
            path.name,
            rep.ok,
            len(names),
            rep.entries,
            rep.ok,
            digest,
            "ok" if rep.ok else rep.errors[0],
        )


def prune(out_dir: Path, keep: int = 30) -> list[Path]:
    files = sorted(out_dir.glob("committee-*.bak"))
    old = files[:-keep] if keep > 0 else files
    for f in old:
        f.unlink()
    return old
