from __future__ import annotations

import re
import shutil
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from .security import hash_password

_SLUG_RE = re.compile(r"[^a-z0-9-]+")


@dataclass(frozen=True)
class Artifact:
    slug: str
    title: str
    path: Path
    created_at: int
    updated_at: int
    password_hash: Optional[str] = None

    @property
    def has_password(self) -> bool:
        return bool(self.password_hash)


def sanitize_slug(value: str) -> str:
    slug = value.strip().lower()
    slug = slug.replace("/", "-").replace("\\", "-")
    slug = _SLUG_RE.sub("-", slug)
    slug = re.sub(r"-+", "-", slug).strip("-")
    slug = slug.replace("..", "")
    if not slug:
        raise ValueError("slug must contain at least one letter or number")
    return slug


class ArtifactStore:
    def __init__(self, home: Path):
        self.home = Path(home).expanduser()
        self.sites_dir = self.home / "sites"
        self.db_path = self.home / "artifacts.db"
        self.sites_dir.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def deploy(self, source: Path, *, slug: str, title: Optional[str] = None, password: Optional[str] = None) -> Artifact:
        source = Path(source).expanduser().resolve()
        if not source.exists():
            raise FileNotFoundError(source)
        safe_slug = sanitize_slug(slug)
        artifact_dir = self.sites_dir / safe_slug
        tmp_dir = self.sites_dir / f".{safe_slug}.tmp"
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir)
        tmp_dir.mkdir(parents=True)

        if source.is_dir():
            if not (source / "index.html").exists():
                raise ValueError("directory artifacts must contain index.html at the root")
            for child in source.iterdir():
                destination = tmp_dir / child.name
                if child.is_dir():
                    shutil.copytree(child, destination)
                else:
                    shutil.copy2(child, destination)
        else:
            if source.suffix.lower() not in {".html", ".htm"}:
                raise ValueError("single-file artifacts must be .html or .htm")
            shutil.copy2(source, tmp_dir / "index.html")

        if artifact_dir.exists():
            shutil.rmtree(artifact_dir)
        tmp_dir.rename(artifact_dir)

        now = int(time.time())
        existing = self.get(safe_slug)
        created_at = existing.created_at if existing else now
        password_hash_value = hash_password(password) if password else (existing.password_hash if existing else None)
        artifact = Artifact(
            slug=safe_slug,
            title=title or safe_slug,
            path=artifact_dir,
            created_at=created_at,
            updated_at=now,
            password_hash=password_hash_value,
        )
        self._upsert(artifact)
        return artifact

    def list(self) -> Iterable[Artifact]:
        with self._connect() as con:
            rows = con.execute("SELECT slug, title, path, created_at, updated_at, password_hash FROM artifacts ORDER BY updated_at DESC").fetchall()
        return [self._row_to_artifact(row) for row in rows]

    def get(self, slug: str) -> Optional[Artifact]:
        safe_slug = sanitize_slug(slug)
        with self._connect() as con:
            row = con.execute("SELECT slug, title, path, created_at, updated_at, password_hash FROM artifacts WHERE slug = ?", (safe_slug,)).fetchone()
        return self._row_to_artifact(row) if row else None

    def protect(self, slug: str, password: str) -> Artifact:
        artifact = self._require(slug)
        updated = Artifact(
            slug=artifact.slug,
            title=artifact.title,
            path=artifact.path,
            created_at=artifact.created_at,
            updated_at=int(time.time()),
            password_hash=hash_password(password),
        )
        self._upsert(updated)
        return updated

    def unprotect(self, slug: str) -> Artifact:
        artifact = self._require(slug)
        updated = Artifact(
            slug=artifact.slug,
            title=artifact.title,
            path=artifact.path,
            created_at=artifact.created_at,
            updated_at=int(time.time()),
            password_hash=None,
        )
        self._upsert(updated)
        return updated

    def delete(self, slug: str) -> None:
        artifact = self._require(slug)
        if artifact.path.exists():
            shutil.rmtree(artifact.path)
        with self._connect() as con:
            con.execute("DELETE FROM artifacts WHERE slug = ?", (artifact.slug,))

    def resolve_file(self, artifact: Artifact, relative_path: str = "") -> Path:
        requested = (artifact.path / (relative_path or "index.html")).resolve()
        root = artifact.path.resolve()
        if root != requested and root not in requested.parents:
            raise ValueError("path escapes artifact root")
        if requested.is_dir():
            requested = requested / "index.html"
        return requested

    def _require(self, slug: str) -> Artifact:
        artifact = self.get(slug)
        if not artifact:
            raise KeyError(slug)
        return artifact

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        return con

    def _init_db(self) -> None:
        with self._connect() as con:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS artifacts (
                    slug TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    path TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    password_hash TEXT
                )
                """
            )

    def _upsert(self, artifact: Artifact) -> None:
        with self._connect() as con:
            con.execute(
                """
                INSERT INTO artifacts (slug, title, path, created_at, updated_at, password_hash)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(slug) DO UPDATE SET
                    title = excluded.title,
                    path = excluded.path,
                    updated_at = excluded.updated_at,
                    password_hash = excluded.password_hash
                """,
                (artifact.slug, artifact.title, str(artifact.path), artifact.created_at, artifact.updated_at, artifact.password_hash),
            )

    def _row_to_artifact(self, row: sqlite3.Row) -> Artifact:
        return Artifact(
            slug=row["slug"],
            title=row["title"],
            path=Path(row["path"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            password_hash=row["password_hash"],
        )
