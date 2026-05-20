from __future__ import annotations

import json
import re
import secrets
import shutil
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Sequence

from .security import hash_password, verify_password

_SLUG_RE = re.compile(r"[^a-z0-9-]+")


@dataclass(frozen=True)
class Artifact:
    slug: str
    title: str
    description: str
    path: Path
    created_at: int
    updated_at: int
    password_hash: Optional[str] = None
    status: str = "active"
    archived_at: Optional[int] = None
    archive_reason: Optional[str] = None
    capabilities: tuple[str, ...] = ()
    pinned: bool = False
    expires_at: Optional[int] = None
    auth_mode: str = "public"
    requires_action: bool = False
    share_token_hash: Optional[str] = None

    @property
    def has_password(self) -> bool:
        return bool(self.password_hash)

    @property
    def uses_profile_auth(self) -> bool:
        return self.auth_mode == "profile"

    @property
    def is_archived(self) -> bool:
        return self.status == "archived"


@dataclass(frozen=True)
class ActionAudit:
    id: int
    created_at: int
    slug: str
    capability: str
    actor: str
    payload_hash: str
    status: str
    result_summary: str
    error: str


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

    def deploy(
        self,
        source: Path,
        *,
        slug: str,
        title: Optional[str] = None,
        description: Optional[str] = None,
        password: Optional[str] = None,
        capabilities: Optional[Sequence[str]] = None,
        pinned: bool = False,
        expires_at: Optional[int] = None,
        auth_mode: Optional[str] = None,
        requires_action: bool = False,
    ) -> Artifact:
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
        password_supplied = password is not None
        password_hash_value = hash_password(password) if password_supplied else (existing.password_hash if existing else None)
        next_auth_mode = _normalize_auth_mode(auth_mode, password_hash_value, existing.auth_mode if existing else None, password_supplied=password_supplied)
        artifact = Artifact(
            slug=safe_slug,
            title=title or (existing.title if existing else safe_slug),
            description=description if description is not None else (existing.description if existing else ""),
            path=artifact_dir,
            created_at=created_at,
            updated_at=now,
            password_hash=password_hash_value,
            status="active",
            archived_at=None,
            archive_reason=None,
            capabilities=tuple(capabilities) if capabilities is not None else (existing.capabilities if existing else ()),
            pinned=bool(pinned or (existing.pinned if existing else False)),
            expires_at=expires_at if expires_at is not None else (existing.expires_at if existing else None),
            auth_mode=next_auth_mode,
            requires_action=bool(requires_action or (existing.requires_action if existing else False)),
            share_token_hash=existing.share_token_hash if existing else None,
        )
        self._upsert(artifact)
        return artifact

    def register_thing(
        self,
        source: Path,
        *,
        slug: str,
        title: Optional[str] = None,
        description: Optional[str] = None,
        capabilities: Optional[Sequence[str]] = None,
        requires_action: bool = False,
        pinned: bool = False,
        public: bool = False,
        password: Optional[str] = None,
    ) -> Artifact:
        """Register an agent-generated Workspace Thing using profile auth by default."""

        auth_mode = "public" if public else ("custom" if password else "profile")
        return self.deploy(
            source,
            slug=slug,
            title=title,
            description=description,
            password=password,
            capabilities=capabilities,
            pinned=pinned,
            auth_mode=auth_mode,
            requires_action=requires_action,
        )

    def list(self, *, status: str = "active", include_archived: Optional[bool] = None) -> Iterable[Artifact]:
        where = _status_where(status, include_archived)
        with self._connect() as con:
            rows = con.execute(
                f"""
                SELECT slug, title, description, path, created_at, updated_at, password_hash, status, archived_at, archive_reason, capabilities, pinned, expires_at, auth_mode, requires_action, share_token_hash
                FROM artifacts
                {where}
                ORDER BY updated_at DESC, slug ASC
                """
            ).fetchall()
        return [self._row_to_artifact(row) for row in rows]

    def search(self, query: str, *, status: str = "active", include_archived: Optional[bool] = None) -> Iterable[Artifact]:
        needle = query.strip()
        if not needle:
            return self.list(status=status, include_archived=include_archived)
        like = f"%{needle.lower()}%"
        archived_clause = _status_and_clause(status, include_archived)
        with self._connect() as con:
            rows = con.execute(
                """
                SELECT slug, title, description, path, created_at, updated_at, password_hash, status, archived_at, archive_reason, capabilities, pinned, expires_at, auth_mode, requires_action, share_token_hash
                FROM artifacts
                WHERE (lower(slug) LIKE ? OR lower(title) LIKE ? OR lower(description) LIKE ?)
                """
                + archived_clause
                + """
                ORDER BY updated_at DESC, slug ASC
                """,
                (like, like, like),
            ).fetchall()
        return [self._row_to_artifact(row) for row in rows]

    def get(self, slug: str) -> Optional[Artifact]:
        safe_slug = sanitize_slug(slug)
        with self._connect() as con:
            row = con.execute(
                """
                SELECT slug, title, description, path, created_at, updated_at, password_hash, status, archived_at, archive_reason, capabilities, pinned, expires_at, auth_mode, requires_action, share_token_hash
                FROM artifacts
                WHERE slug = ?
                """,
                (safe_slug,),
            ).fetchone()
        return self._row_to_artifact(row) if row else None

    def protect(self, slug: str, password: str) -> Artifact:
        artifact = self._require(slug)
        updated = self._copy_artifact(artifact, updated_at=int(time.time()), password_hash=hash_password(password), auth_mode="custom")
        self._upsert(updated)
        return updated

    def unprotect(self, slug: str) -> Artifact:
        artifact = self._require(slug)
        updated = self._copy_artifact(artifact, updated_at=int(time.time()), password_hash=None, auth_mode="public")
        self._upsert(updated)
        return updated

    def update_metadata(
        self,
        slug: str,
        *,
        title: Optional[str] = None,
        description: Optional[str] = None,
        pinned: Optional[bool] = None,
        expires_at: Optional[int] = None,
        clear_expires_at: bool = False,
    ) -> Artifact:
        artifact = self._require(slug)
        next_expires_at = None if clear_expires_at else (expires_at if expires_at is not None else artifact.expires_at)
        updated = self._copy_artifact(
            artifact,
            title=title if title is not None else artifact.title,
            description=description if description is not None else artifact.description,
            pinned=pinned if pinned is not None else artifact.pinned,
            expires_at=next_expires_at,
            updated_at=int(time.time()),
        )
        self._upsert(updated)
        return updated

    def set_capabilities(self, slug: str, capabilities: Sequence[str]) -> Artifact:
        artifact = self._require(slug)
        updated = self._copy_artifact(artifact, updated_at=int(time.time()), capabilities=tuple(capabilities))
        self._upsert(updated)
        return updated

    def set_requires_action(self, slug: str, requires_action: bool) -> Artifact:
        artifact = self._require(slug)
        updated = self._copy_artifact(artifact, updated_at=int(time.time()), requires_action=bool(requires_action))
        self._upsert(updated)
        return updated

    def list_workspace_things(self, *, bucket: str = "active") -> list[Artifact]:
        bucket = (bucket or "active").lower()
        if bucket == "requires-action":
            return [artifact for artifact in self.list(status="active") if artifact.requires_action]
        if bucket == "pinned":
            return [artifact for artifact in self.list(status="active") if artifact.pinned]
        if bucket == "recent":
            return list(self.list(status="active"))
        if bucket == "archived":
            return list(self.list(status="archived"))
        if bucket in {"active", "all"}:
            return list(self.list(status=bucket))
        raise ValueError(f"invalid workspace bucket: {bucket}")

    def create_share_override(self, slug: str, *, token: Optional[str] = None) -> str:
        artifact = self._require(slug)
        raw_token = token or secrets.token_urlsafe(24)
        updated = self._copy_artifact(artifact, updated_at=int(time.time()), share_token_hash=hash_password(raw_token))
        self._upsert(updated)
        return raw_token

    def verify_share_token(self, slug: str, token: Optional[str]) -> bool:
        if not token:
            return False
        artifact = self.get(slug)
        if not artifact or not artifact.share_token_hash:
            return False
        return verify_password(token, artifact.share_token_hash)

    def get_setting(self, key: str) -> Optional[str]:
        with self._connect() as con:
            row = con.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_setting(self, key: str, value: str) -> None:
        with self._connect() as con:
            con.execute(
                "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (key, value, int(time.time())),
            )

    def set_workspace_password(self, password: str) -> None:
        self.set_setting("workspace_password_hash", hash_password(password))

    def verify_workspace_password(self, password: str) -> bool:
        encoded = self.get_setting("workspace_password_hash")
        return bool(encoded and verify_password(password, encoded))

    def workspace_password_configured(self) -> bool:
        return bool(self.get_setting("workspace_password_hash"))

    def archive(self, slug: str, *, reason: str = "", force: bool = False) -> Artifact:
        artifact = self._require(slug)
        if artifact.pinned and not force:
            return artifact
        now = int(time.time())

        updated = self._copy_artifact(
            artifact,
            status="archived",
            archived_at=now,
            archive_reason=reason,
            updated_at=now,
        )
        self._upsert(updated)
        return updated

    def restore(self, slug: str) -> Artifact:
        artifact = self._require(slug)
        updated = self._copy_artifact(
            artifact,
            status="active",
            archived_at=None,
            archive_reason=None,
            updated_at=int(time.time()),
        )
        self._upsert(updated)
        return updated

    def prune(self, *, now: int, dry_run: bool = True) -> list[dict[str, str]]:
        report: list[dict[str, str]] = []
        expired = sorted(
            [artifact for artifact in self.list(status="all") if artifact.expires_at is not None and artifact.expires_at <= now],
            key=lambda artifact: (not artifact.has_password, artifact.slug),
        )
        for artifact in expired:
            if artifact.pinned:
                report.append({"slug": artifact.slug, "action": "skip", "reason": "pinned"})
                continue
            if artifact.status == "archived":
                if artifact.has_password:
                    report.append({"slug": artifact.slug, "action": "skip", "reason": "protected"})
                    continue
                report.append({"slug": artifact.slug, "action": "delete", "reason": "expired archived"})
                if not dry_run:
                    self.delete(artifact.slug)
                continue
            report.append({"slug": artifact.slug, "action": "archive", "reason": "expired"})
            if not dry_run:
                self.archive(artifact.slug, reason="expired")
        return report

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

    def record_action_audit(
        self,
        *,
        slug: str,
        capability: str,
        actor: str,
        payload_hash: str,
        status: str,
        result_summary: str = "",
        error: str = "",
    ) -> None:
        with self._connect() as con:
            con.execute(
                """
                INSERT INTO action_audit (created_at, slug, capability, actor, payload_hash, status, result_summary, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (int(time.time()), slug, capability, actor, payload_hash, status, result_summary, error),
            )

    def list_action_audit(self, slug: Optional[str] = None) -> list[ActionAudit]:
        with self._connect() as con:
            if slug is None:
                rows = con.execute(
                    """
                    SELECT id, created_at, slug, capability, actor, payload_hash, status, result_summary, error
                    FROM action_audit
                    ORDER BY id
                    """
                ).fetchall()
            else:
                rows = con.execute(
                    """
                    SELECT id, created_at, slug, capability, actor, payload_hash, status, result_summary, error
                    FROM action_audit
                    WHERE slug = ?
                    ORDER BY id
                    """,
                    (sanitize_slug(slug),),
                ).fetchall()
        return [self._row_to_action_audit(row) for row in rows]

    def _status_where(self, *, include_archived: bool = False, status: Optional[str] = None) -> str:
        if status is None:
            return "" if include_archived else "WHERE status != 'archived'"
        status = status.lower()
        if status == "all":
            return ""
        if status not in {"active", "archived"}:
            raise ValueError(f"invalid artifact status: {status}")
        if status == "archived":
            return "WHERE status = 'archived'"
        return "WHERE status != 'archived'"

    def _status_clause(self, *, include_archived: bool = False, status: Optional[str] = None, prefix: str = "AND") -> str:
        where = self._status_where(include_archived=include_archived, status=status)
        if not where:
            return ""
        return f" {prefix} " + where.removeprefix("WHERE ")

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
                    description TEXT NOT NULL DEFAULT '',
                    path TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    password_hash TEXT,
                    status TEXT NOT NULL DEFAULT 'active',
                    archived_at INTEGER,
                    archive_reason TEXT,
                    capabilities TEXT NOT NULL DEFAULT '[]',
                    pinned INTEGER NOT NULL DEFAULT 0,
                    expires_at INTEGER,
                    auth_mode TEXT NOT NULL DEFAULT 'public',
                    requires_action INTEGER NOT NULL DEFAULT 0,
                    share_token_hash TEXT
                )
                """
            )
            columns = {row[1] for row in con.execute("PRAGMA table_info(artifacts)").fetchall()}
            if "description" not in columns:
                con.execute("ALTER TABLE artifacts ADD COLUMN description TEXT NOT NULL DEFAULT ''")
            if "status" not in columns:
                con.execute("ALTER TABLE artifacts ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")
            if "archived_at" not in columns:
                con.execute("ALTER TABLE artifacts ADD COLUMN archived_at INTEGER")
            if "archive_reason" not in columns:
                con.execute("ALTER TABLE artifacts ADD COLUMN archive_reason TEXT")
            if "capabilities" not in columns:
                con.execute("ALTER TABLE artifacts ADD COLUMN capabilities TEXT NOT NULL DEFAULT '[]'")
            if "pinned" not in columns:
                con.execute("ALTER TABLE artifacts ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")
            if "expires_at" not in columns:
                con.execute("ALTER TABLE artifacts ADD COLUMN expires_at INTEGER")
            if "auth_mode" not in columns:
                con.execute("ALTER TABLE artifacts ADD COLUMN auth_mode TEXT NOT NULL DEFAULT 'public'")
            if "requires_action" not in columns:
                con.execute("ALTER TABLE artifacts ADD COLUMN requires_action INTEGER NOT NULL DEFAULT 0")
            if "share_token_hash" not in columns:
                con.execute("ALTER TABLE artifacts ADD COLUMN share_token_hash TEXT")
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS action_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at INTEGER NOT NULL,
                    slug TEXT NOT NULL,
                    capability TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result_summary TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT ''
                )
                """
            )

    def _upsert(self, artifact: Artifact) -> None:
        with self._connect() as con:
            con.execute(
                """
                INSERT INTO artifacts (slug, title, description, path, created_at, updated_at, password_hash, status, archived_at, archive_reason, capabilities, pinned, expires_at, auth_mode, requires_action, share_token_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(slug) DO UPDATE SET
                    title = excluded.title,
                    description = excluded.description,
                    path = excluded.path,
                    updated_at = excluded.updated_at,
                    password_hash = excluded.password_hash,
                    status = excluded.status,
                    archived_at = excluded.archived_at,
                    archive_reason = excluded.archive_reason,
                    capabilities = excluded.capabilities,
                    pinned = excluded.pinned,
                    expires_at = excluded.expires_at,
                    auth_mode = excluded.auth_mode,
                    requires_action = excluded.requires_action,
                    share_token_hash = excluded.share_token_hash
                """,
                (
                    artifact.slug,
                    artifact.title,
                    artifact.description,
                    str(artifact.path),
                    artifact.created_at,
                    artifact.updated_at,
                    artifact.password_hash,
                    artifact.status,
                    artifact.archived_at,
                    artifact.archive_reason,
                    json.dumps(list(artifact.capabilities)),
                    1 if artifact.pinned else 0,
                    artifact.expires_at,
                    artifact.auth_mode,
                    1 if artifact.requires_action else 0,
                    artifact.share_token_hash,
                ),
            )

    def _row_to_artifact(self, row: sqlite3.Row) -> Artifact:
        return Artifact(
            slug=row["slug"],
            title=row["title"],
            description=row["description"] or "",
            path=Path(row["path"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            password_hash=row["password_hash"],
            status=row["status"] or "active",
            archived_at=row["archived_at"],
            archive_reason=row["archive_reason"],
            capabilities=_decode_capabilities(row["capabilities"]),
            pinned=bool(row["pinned"]),
            expires_at=row["expires_at"],
            auth_mode=row["auth_mode"] or "public",
            requires_action=bool(row["requires_action"]),
            share_token_hash=row["share_token_hash"],
        )

    def _row_to_action_audit(self, row: sqlite3.Row) -> ActionAudit:
        return ActionAudit(
            id=row["id"],
            created_at=row["created_at"],
            slug=row["slug"],
            capability=row["capability"],
            actor=row["actor"],
            payload_hash=row["payload_hash"],
            status=row["status"],
            result_summary=row["result_summary"],
            error=row["error"],
        )

    def _copy_artifact(self, artifact: Artifact, **changes) -> Artifact:
        values = {
            "slug": artifact.slug,
            "title": artifact.title,
            "description": artifact.description,
            "path": artifact.path,
            "created_at": artifact.created_at,
            "updated_at": artifact.updated_at,
            "password_hash": artifact.password_hash,
            "status": artifact.status,
            "archived_at": artifact.archived_at,
            "archive_reason": artifact.archive_reason,
            "capabilities": artifact.capabilities,
            "pinned": artifact.pinned,
            "expires_at": artifact.expires_at,
            "auth_mode": artifact.auth_mode,
            "requires_action": artifact.requires_action,
            "share_token_hash": artifact.share_token_hash,
        }
        values.update(changes)
        return Artifact(**values)


def _normalize_auth_mode(
    auth_mode: Optional[str],
    password_hash_value: Optional[str],
    existing_auth_mode: Optional[str],
    *,
    password_supplied: bool = False,
) -> str:
    if auth_mode is None:
        if password_supplied:
            return "custom"
        return existing_auth_mode or ("custom" if password_hash_value else "public")
    auth_mode = auth_mode.lower()
    if auth_mode not in {"public", "profile", "custom"}:
        raise ValueError(f"invalid auth mode: {auth_mode}")
    if auth_mode == "custom" and not password_hash_value:
        raise ValueError("custom auth mode requires a password")
    return auth_mode


def _status_where(status: str, include_archived: Optional[bool]) -> str:
    if include_archived is not None:
        return "" if include_archived else "WHERE status != 'archived'"
    status = (status or "active").lower()
    if status == "all":
        return ""
    if status == "archived":
        return "WHERE status = 'archived'"
    return "WHERE status != 'archived'"


def _status_and_clause(status: str, include_archived: Optional[bool]) -> str:
    where = _status_where(status, include_archived)
    if not where:
        return ""
    return " AND " + where.removeprefix("WHERE ")


def _decode_capabilities(raw: str) -> tuple[str, ...]:
    if not raw:
        return ()
    try:
        values = json.loads(raw)
    except json.JSONDecodeError:
        return ()
    if not isinstance(values, list):
        return ()
    return tuple(value for value in values if isinstance(value, str))
