#!/usr/bin/env python3
"""Generate a protected Palmer/Echo connectivity health artifact.

The artifact is intentionally static: it captures the last live smoke-test run,
shows exactly which app/account broke, and gives targeted repair commands so a
single bad email token does not turn into a full re-auth clown parade.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import pathlib
import re
import sqlite3
import subprocess

import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

PUBLIC_BASE_URL = "https://artifacts.skylarbpayne.com"
ARTIFACT_SLUG = "agent-health"
PALMER_PROFILE = pathlib.Path("/Users/skylarpayne/.hermes/profiles/palmer")
ECHO_PROFILE = pathlib.Path("/Users/skylarpayne/.hermes/profiles/echo")
ARTIFACT_HOME = pathlib.Path("/Users/skylarpayne/.hermes/artifacts")
HERMES_AGENT_REPO = pathlib.Path("/Users/skylarpayne/.hermes/hermes-agent")
SKYVAULT = pathlib.Path("/Users/skylarpayne/skyvault")
OPENCHRONICLE_ROOT = pathlib.Path("/Users/skylarpayne/.hermes/shared/macbook-openchronicle")
SKILL_CANVA = PALMER_PROFILE / "skills/productivity/third-party-oauth-integrations/scripts/canva_oauth.py"

STATUS_ORDER = {"ok": 0, "warn": 1, "fail": 2, "unknown": 3}


@dataclass
class Check:
    id: str
    agent: str
    app: str
    account: str
    operation: str
    status: str
    summary: str
    evidence: str = ""
    remediation: str = ""
    seconds: float = 0.0


@dataclass
class MorningItem:
    gate: str
    status: str
    title: str
    summary: str
    evidence: str = ""
    next_action: str = ""
    owner: str = "Palmer"


MORNING_GATES = ["system", "truth", "priority", "execution"]


def load_profile_env(profile: pathlib.Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "HERMES_HOME": str(profile),
            "HOME": str(profile / "home"),
            "GOG_CONFIG_DIR": str(profile / "home/Library/Application Support/gogcli"),
        }
    )
    env_path = profile / ".env"
    if env_path.exists():
        for raw in env_path.read_text(errors="ignore").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            env[key] = value.strip().strip('"').strip("'")
    return env


def run(cmd: list[str], env: dict[str, str] | None = None, timeout: int = 25) -> tuple[int, str, str, float]:
    start = time.time()
    try:
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout or "", proc.stderr or "", time.time() - start
    except subprocess.TimeoutExpired as exc:
        return 124, exc.stdout or "", exc.stderr or "timeout", time.time() - start
    except FileNotFoundError as exc:
        return 127, "", str(exc), time.time() - start


def redact(text: str, limit: int = 260) -> str:
    text = (text or "").replace("\x00", "")
    text = re.sub(r"gh[oprsu]_[A-Za-z0-9_]+", "gh*_REDACTED", text)
    text = re.sub(r"Bearer\s+[A-Za-z0-9._\-]+", "Bearer REDACTED", text)
    text = re.sub(r"(?i)(access_token|refresh_token|client_secret|password)\"?\s*[:=]\s*\"?[^\"\s,}]+", r"\1=REDACTED", text)
    text = re.sub(r"code=[^&\s]+", "code=REDACTED", text)
    text = " ".join(text.split())
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def parse_json(text: str) -> Any | None:
    try:
        return json.loads(text, strict=False)
    except Exception:
        return None


def _redact_process_args(args: list[str]) -> list[str]:
    """Keep process samples useful without leaking tokens or huge prompts."""
    scrubbed: list[str] = []
    skip_next = False
    secret_flags = {"--secret", "--secret-token", "--token", "--api-key", "--password", "--auth"}
    for part in args:
        if skip_next:
            skip_next = False
            continue
        if part in secret_flags:
            skip_next = True
            continue
        if any(part.startswith(flag + "=") for flag in secret_flags):
            continue
        if re.search(r"(?i)(token|secret|password|api[_-]?key)=", part):
            scrubbed.append("[REDACTED]")
            continue
        scrubbed.append(part)
    return scrubbed[:5]


def process_inventory(ps_output: str) -> dict[str, Any]:
    """Summarize local runtime processes for the ops console."""
    categories = {"artifactd": [], "cloudflared": [], "hermes": [], "codex": [], "other": []}
    for raw in ps_output.splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2 or not parts[0].isdigit():
            continue
        pid, comm = parts[0], pathlib.Path(parts[1]).name
        args = parts[2:]
        haystack = " ".join([comm, *args]).lower()
        if "artifactd" in haystack:
            category = "artifactd"
        elif "cloudflared" in haystack:
            category = "cloudflared"
        elif "hermes" in haystack:
            category = "hermes"
        elif re.search(r"(^|/)codex(\s|$)", haystack) or comm == "codex":
            category = "codex"
        else:
            category = "other"
        sample_args = _redact_process_args(args)
        if sample_args and pathlib.Path(sample_args[0]).name == comm:
            sample_args = sample_args[1:]
        categories[category].append(" ".join([pid, comm, *sample_args]).strip())
    counts = {key: len(value) for key, value in categories.items()}
    return {
        "counts": counts,
        "total_tracked": counts["artifactd"] + counts["cloudflared"] + counts["hermes"] + counts["codex"],
        "samples": {key: value[:6] for key, value in categories.items() if value},
    }


def _iso_age_hours(value: Any) -> float | None:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return round((datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds() / 3600, 1)
    except Exception:
        return None


def codex_auth_summary(home: pathlib.Path) -> dict[str, str]:
    """Report Codex auth freshness without exposing token contents."""
    auth_file = home / ".codex" / "auth.json"
    if not auth_file.exists():
        return {"status": "warn", "evidence": f"Codex auth file missing at {auth_file}"}
    if auth_file.stat().st_size <= 0:
        return {"status": "warn", "evidence": "Codex auth file exists but is empty"}

    data = parse_json(auth_file.read_text(errors="ignore"))
    if not isinstance(data, dict):
        return {"status": "warn", "evidence": "Codex auth file exists but JSON is unreadable"}

    last_refresh = data.get("last_refresh") or data.get("lastRefresh") or data.get("expires_at")
    age_hours = _iso_age_hours(last_refresh) if last_refresh else None
    auth_mode = data.get("auth_mode") or data.get("authMode") or "unknown"
    has_api_key = bool(data.get("OPENAI_API_KEY"))
    token_fields = sum(1 for key in data if re.search(r"(?i)(token|refresh|access|credential|session)", str(key)))
    evidence = f"auth_mode={auth_mode}; token_like_fields={token_fields}; api_key_present={has_api_key}; last_refresh={'present' if last_refresh else 'missing'}"
    if age_hours is not None:
        evidence += f"; last_refresh_age_hours={age_hours}"
    status = "ok" if last_refresh or has_api_key or token_fields else "warn"
    return {"status": status, "evidence": evidence}


def parse_cron_list_output(text: str) -> dict[str, Any]:
    jobs: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for raw in text.splitlines():
        line = raw.rstrip()
        job = re.match(r"\s*([a-z0-9]{4,})\s+\[([^\]]+)\]", line)
        if job:
            if current:
                jobs.append(current)
            current = {"id": job.group(1), "status": job.group(2), "name": job.group(1), "last_run": ""}
            continue
        if current and line.strip().startswith("Name:"):
            current["name"] = line.split("Name:", 1)[1].strip()
        elif current and line.strip().startswith("Last run:"):
            current["last_run"] = line.split("Last run:", 1)[1].strip()
    if current:
        jobs.append(current)
    failed_jobs = [j["name"] for j in jobs if j.get("last_run") and not j["last_run"].lower().endswith(" ok")]
    return {
        "total": len(jobs),
        "active": sum(1 for j in jobs if j.get("status") == "active"),
        "paused": sum(1 for j in jobs if j.get("status") == "paused"),
        "failed_last_runs": len(failed_jobs),
        "failed_jobs": failed_jobs[:8],
    }


def summarize_recent_log_errors(log_paths: list[pathlib.Path], max_lines: int = 200) -> dict[str, Any]:
    patterns = re.compile(r"(?i)(error|traceback|exception|failed|fatal)")
    samples: list[str] = []
    error_lines = 0
    files_checked = 0
    for path in log_paths:
        if not path.exists() or not path.is_file():
            continue
        files_checked += 1
        try:
            lines = path.read_text(errors="ignore").splitlines()[-max_lines:]
        except OSError:
            continue
        for line in lines:
            if patterns.search(line):
                error_lines += 1
                if len(samples) < 8:
                    samples.append(f"{path.name}: {redact(line, 220)}")
    return {"files_checked": files_checked, "error_lines": error_lines, "samples": samples}


def parse_df_output(text: str) -> dict[str, Any] | None:
    """Parse POSIX `df -Pk` output into a compact storage summary."""
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) < 2:
        return None
    parts = lines[-1].split()
    if len(parts) < 6:
        return None
    try:
        total_kb = int(parts[1])
        used_kb = int(parts[2])
        available_kb = int(parts[3])
    except ValueError:
        return None
    free_percent = round((available_kb / total_kb) * 100, 1) if total_kb else 0.0
    used_percent = round((used_kb / total_kb) * 100, 1) if total_kb else 0.0
    return {
        "filesystem": parts[0],
        "total_gb": round(total_kb / 1024 / 1024, 1),
        "used_gb": round(used_kb / 1024 / 1024, 1),
        "available_gb": round(available_kb / 1024 / 1024, 1),
        "used_percent": used_percent,
        "free_percent": free_percent,
        "mount": parts[-1],
    }


def directory_size_gb(path: pathlib.Path) -> float | None:
    try:
        total = 0
        for root, dirs, files in os.walk(path):
            # Avoid charging symlinked trees twice.
            dirs[:] = [d for d in dirs if not (pathlib.Path(root) / d).is_symlink()]
            for name in files:
                p = pathlib.Path(root) / name
                try:
                    if not p.is_symlink():
                        total += p.stat().st_size
                except OSError:
                    continue
        return round(total / 1024 / 1024 / 1024, 2)
    except OSError:
        return None


def hermes_release_gap(version_text: str, timeout: int = 20) -> dict[str, Any]:
    """Return last installed Hermes release and newer GitHub releases without leaking gh auth."""
    m = re.search(r"Hermes Agent\s+[^\n]*\((\d{4})\.(\d{1,2})\.(\d{1,2})\)", version_text)
    installed_tag = f"v{int(m.group(1))}.{int(m.group(2))}.{int(m.group(3))}" if m else ""
    env = os.environ.copy()
    env["HOME"] = "/Users/skylarpayne"
    rc, out, err, _ = run([
        "gh", "release", "list",
        "--repo", "NousResearch/hermes-agent",
        "--limit", "30",
        "--json", "tagName,publishedAt,isLatest",
    ], env, timeout)
    if rc != 0:
        return {"status": "warn", "evidence": f"installed_release={installed_tag or 'unknown'}; GitHub release check failed: {redact(err or out, 180)}"}
    releases = parse_json(out)
    if not isinstance(releases, list):
        return {"status": "warn", "evidence": f"installed_release={installed_tag or 'unknown'}; GitHub release output unreadable"}
    tags = [str(r.get("tagName", "")) for r in releases if isinstance(r, dict)]
    newest = releases[0] if releases and isinstance(releases[0], dict) else {}
    if installed_tag and installed_tag in tags:
        newer_count = tags.index(installed_tag)
        status = "ok" if newer_count == 0 else "warn"
        return {
            "status": status,
            "evidence": f"installed_release={installed_tag}; latest_release={newest.get('tagName','unknown')} published={newest.get('publishedAt','unknown')}; releases_since_installed={newer_count}",
        }
    return {
        "status": "warn",
        "evidence": f"installed_release={installed_tag or 'unknown'} not found in latest {len(tags)} GitHub releases; latest_release={newest.get('tagName','unknown')}",
    }


def git_remote_summary(repo: pathlib.Path, timeout: int = 20) -> dict[str, str]:
    if not (repo / ".git").exists():
        return {"status": "warn", "evidence": f"git checkout missing at {repo}"}
    env = os.environ.copy()
    rc, branch, err, _ = run(["git", "-C", str(repo), "branch", "--show-current"], env, timeout)
    if rc != 0:
        return {"status": "fail", "evidence": redact(err or branch)}
    branch = branch.strip() or "main"
    rc, head, err, _ = run(["git", "-C", str(repo), "rev-parse", "HEAD"], env, timeout)
    if rc != 0:
        return {"status": "fail", "evidence": redact(err or head)}
    rc, remote, err, _ = run(["git", "-C", str(repo), "ls-remote", "origin", f"refs/heads/{branch}"], env, timeout)
    if rc != 0:
        return {"status": "warn", "evidence": f"local {branch}@{head.strip()[:8]}; remote check failed: {redact(err or remote, 180)}"}
    remote_sha = (remote.split() or [""])[0]
    local_sha = head.strip()
    if remote_sha and remote_sha == local_sha:
        return {"status": "ok", "evidence": f"{branch}@{local_sha[:8]} matches origin/{branch}"}
    if remote_sha:
        return {"status": "warn", "evidence": f"{branch} local {local_sha[:8]} differs from origin/{branch} {remote_sha[:8]}"}
    return {"status": "warn", "evidence": f"{branch}@{local_sha[:8]}; origin branch not found"}


def parse_memory_config(config_text: str) -> dict[str, str]:
    provider = ""
    memory_enabled = ""
    user_enabled = ""
    context_engine = ""
    section = ""
    for raw in config_text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if raw and not raw.startswith(" ") and raw.rstrip().endswith(":"):
            section = raw.strip().rstrip(":")
            continue
        line = raw.strip()
        if section == "memory":
            if line.startswith("provider:"):
                provider = line.split(":", 1)[1].strip().strip("'\"")
            elif line.startswith("memory_enabled:"):
                memory_enabled = line.split(":", 1)[1].strip()
            elif line.startswith("user_profile_enabled:"):
                user_enabled = line.split(":", 1)[1].strip()
        elif section == "context" and line.startswith("engine:"):
            context_engine = line.split(":", 1)[1].strip().strip("'\"")
    return {"provider": provider, "memory_enabled": memory_enabled, "user_profile_enabled": user_enabled, "context_engine": context_engine}


def skill_usage_summary(path: pathlib.Path) -> dict[str, Any]:
    data = parse_json(path.read_text(errors="ignore")) if path.exists() else None
    if not isinstance(data, dict):
        return {"status": "warn", "evidence": f"skill usage file missing/unreadable at {path}"}
    active = [name for name, meta in data.items() if isinstance(meta, dict) and meta.get("state", "active") == "active"]
    patched = [name for name, meta in data.items() if isinstance(meta, dict) and (meta.get("patch_count") or 0) > 0]
    used = sorted(
        (
            (name, meta.get("use_count") or 0, meta.get("last_used_at") or "")
            for name, meta in data.items()
            if isinstance(meta, dict)
        ),
        key=lambda row: row[1],
        reverse=True,
    )[:5]
    top = ", ".join(f"{name}({count})" for name, count, _ in used) or "no usage recorded"
    return {
        "status": "ok",
        "evidence": f"active={len(active)}; patched_for_feedback={len(patched)}; top={top}",
    }


def latest_release_summary(root: pathlib.Path) -> dict[str, Any]:
    releases = root / "releases"
    if not releases.exists():
        return {"status": "warn", "evidence": f"OpenChronicle release root missing at {releases}"}
    candidates = sorted((p for p in releases.iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True)
    if not candidates:
        return {"status": "warn", "evidence": "No OpenChronicle releases found"}
    latest = candidates[0]
    manifest = latest / "manifest.json"
    data = parse_json(manifest.read_text(errors="ignore")) if manifest.exists() else None
    if not isinstance(data, dict):
        return {"status": "warn", "evidence": f"latest release {latest.name} has no readable manifest"}
    created = data.get("created_at", "unknown")
    age_hours = _iso_age_hours(created)
    status = "ok" if age_hours is not None and age_hours <= 48 else "warn"
    evidence = f"release={latest.name}; created_at={created}; captures={data.get('capture_files')}; memories={data.get('memory_files')}; sqlite_backup={'sqlite_backup' in (data.get('contains') or [])}"
    if age_hours is not None:
        evidence += f"; age_hours={age_hours}"
    return {"status": status, "evidence": evidence}


def openchronicle_index_summary(root: pathlib.Path) -> dict[str, Any]:
    current = root / "current"
    db_path = current / "index.db"
    if not db_path.exists():
        return {"status": "warn", "evidence": f"OpenChronicle index DB missing at {db_path}"}

    core_tables = ["captures", "sessions", "timeline_blocks", "entries", "extractor_records"]
    entity_tables = ["entities", "entity_mentions", "entity_edges"]
    counts: dict[str, int | str] = {}
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2) as conn:
            table_rows = conn.execute("select name from sqlite_master where type='table'").fetchall()
            tables = {str(row[0]) for row in table_rows}
            for table in core_tables + entity_tables:
                if table in tables:
                    counts[table] = int(conn.execute(f"select count(*) from {table}").fetchone()[0])
                else:
                    counts[table] = "missing"
    except sqlite3.Error as exc:
        return {"status": "warn", "evidence": f"OpenChronicle index unreadable: {redact(str(exc), 180)}"}

    core_ok = all(isinstance(counts.get(table), int) and int(counts[table]) > 0 for table in ["captures", "sessions", "timeline_blocks"])
    entities = counts.get("entities")
    entity_ok = isinstance(entities, int) and entities > 0
    status = "ok" if core_ok and entity_ok else "warn"
    evidence = "; ".join(f"{table}={counts[table]}" for table in core_tables + entity_tables)
    return {"status": status, "evidence": evidence}


def skyvault_entity_summary(root: pathlib.Path) -> dict[str, Any]:
    people_dir = root / "Palmer/people"
    if not people_dir.exists():
        return {"status": "warn", "evidence": f"People/entity note directory missing at {people_dir}"}
    notes = [p for p in people_dir.glob("*.md") if p.name not in {"INDEX.md", "QUICK-REFERENCE.md", "CRM-SUMMARY.md"}]
    summary = people_dir / "CRM-SUMMARY.md"
    status = "ok" if notes and summary.exists() else "warn"
    evidence = f"people_entity_notes={len(notes)}; crm_summary={'present' if summary.exists() else 'missing'}"
    return {"status": status, "evidence": evidence}


def status_rollup(statuses: list[str]) -> str:
    if "fail" in statuses:
        return "fail"
    if "warn" in statuses:
        return "warn"
    if statuses:
        return "ok"
    return "unknown"


def age_hours_from_timestamp(value: int | float | None, now_ts: float | None = None) -> float | None:
    if not value:
        return None
    now_ts = now_ts or time.time()
    return round(max(0.0, now_ts - float(value)) / 3600, 1)


def age_label(hours: float | None) -> str:
    if hours is None:
        return "unknown age"
    if hours < 1:
        return f"{round(hours * 60)}m old"
    if hours < 48:
        return f"{hours:.1f}h old"
    return f"{hours / 24:.1f}d old"


def note_updated_age_hours(path: pathlib.Path, now: datetime | None = None) -> float | None:
    now = now or datetime.now(ZoneInfo("America/Los_Angeles"))
    try:
        text = path.read_text(errors="ignore")[:1200]
    except OSError:
        return None
    match = re.search(r"(?m)^updated:\s*(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})", text)
    if match:
        try:
            updated = datetime.strptime(" ".join(match.groups()), "%Y-%m-%d %H:%M").replace(tzinfo=ZoneInfo("America/Los_Angeles"))
            return round((now - updated).total_seconds() / 3600, 1)
        except ValueError:
            pass
    try:
        return round((now.timestamp() - path.stat().st_mtime) / 3600, 1)
    except OSError:
        return None


def note_freshness_item(path: pathlib.Path, title: str, gate: str, warn_hours: int = 36, fail_hours: int = 96) -> MorningItem:
    if not path.exists():
        return MorningItem(gate=gate, status="fail", title=title, summary="Canonical note is missing", evidence=str(path), next_action="Restore or recreate this note before relying on morning priorities.")
    hours = note_updated_age_hours(path)
    if hours is None:
        status = "warn"
        summary = "Freshness could not be determined"
    elif hours > fail_hours:
        status = "fail"
        summary = f"Canonical note is stale ({age_label(hours)})"
    elif hours > warn_hours:
        status = "warn"
        summary = f"Canonical note should be refreshed ({age_label(hours)})"
    else:
        status = "ok"
        summary = f"Canonical note is fresh enough ({age_label(hours)})"
    return MorningItem(gate=gate, status=status, title=title, summary=summary, evidence=str(path), next_action="Refresh the note from Kanban, calendar, and recent receipts." if status != "ok" else "Use this note as morning context.")


def extract_markdown_items(path: pathlib.Path, heading: str, limit: int = 4) -> list[str]:
    try:
        lines = path.read_text(errors="ignore").splitlines()
    except OSError:
        return []
    items: list[str] = []
    in_section = False
    wanted = heading.strip().lower()
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("## "):
            in_section = stripped.lstrip("# ").strip().lower() == wanted
            continue
        if in_section and stripped.startswith("#"):
            break
        if in_section and stripped.startswith("- "):
            item = re.sub(r"\[\[([^\]]+)\]\]", r"\1", stripped[2:])
            items.append(redact(item, 220))
            if len(items) >= limit:
                break
    return items


def is_task_context_sufficient(row: sqlite3.Row | dict[str, Any]) -> bool:
    body = (row["body"] or "") if isinstance(row, sqlite3.Row) else (row.get("body") or "")
    title = row["title"] if isinstance(row, sqlite3.Row) else row.get("title", "")
    text = f"{title}\n{body}".lower()
    if len(body.strip()) < 180:
        return False
    vague_markers = ["tbd", "todo: fill", "needs context", "missing context", "unclear", "figure out what this means"]
    return not any(marker in text for marker in vague_markers)


def kanban_readiness_summary(db_path: pathlib.Path = pathlib.Path("/Users/skylarpayne/.hermes/kanban.db"), now_ts: float | None = None) -> dict[str, Any]:
    now_ts = now_ts or time.time()
    if not db_path.exists():
        return {"status": "fail", "evidence": f"Kanban DB missing at {db_path}", "counts": {}, "executable": [], "blocked_context": [], "approval_needed": [], "stale": []}
    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                select id, title, body, assignee, status, priority, created_at, started_at, last_heartbeat_at
                from tasks
                where status not in ('done', 'archived')
                order by priority desc, created_at desc
                """
            ).fetchall()
    except sqlite3.Error as exc:
        return {"status": "fail", "evidence": f"Kanban DB query failed: {redact(str(exc))}", "counts": {}, "executable": [], "blocked_context": [], "approval_needed": [], "stale": []}

    counts: dict[str, int] = {}
    executable: list[dict[str, Any]] = []
    blocked_context: list[dict[str, Any]] = []
    approval_needed: list[dict[str, Any]] = []
    stale: list[dict[str, Any]] = []

    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
        body = row["body"] or ""
        title = row["title"] or ""
        text = f"{title}\n{body}".lower()
        age = age_hours_from_timestamp(row["last_heartbeat_at"] or row["started_at"] or row["created_at"], now_ts)
        task_ref = {"id": row["id"], "title": title, "assignee": row["assignee"], "status": row["status"], "age_hours": age}

        if row["status"] == "running" and age is not None and age > 4:
            stale.append({**task_ref, "reason": "running without a fresh heartbeat"})
        elif row["status"] in {"ready", "todo", "triage", "blocked"} and age is not None and age > 72:
            stale.append({**task_ref, "reason": "active task has not moved in 72h"})

        if row["assignee"] == "palmer" and row["status"] in {"ready", "running"}:
            if is_task_context_sufficient(row):
                executable.append(task_ref)
            else:
                blocked_context.append({**task_ref, "reason": "body is too thin or vague for autonomous execution"})
        elif row["assignee"] == "palmer" and row["status"] in {"todo", "triage"} and not is_task_context_sufficient(row):
            blocked_context.append({**task_ref, "reason": "not ready and context is insufficient"})

        if row["status"] == "blocked" or any(marker in text for marker in ["approve", "approval", "decide", "send", "purchase", "payment", "calendar", "credential", "auth", "review-required"]):
            if row["assignee"] in {"skylar", "palmer", None}:
                approval_needed.append(task_ref)

    active_total = sum(counts.values())
    status = "fail" if not active_total else "warn" if stale or blocked_context else "ok"
    evidence = "statuses: " + ", ".join(f"{key}={value}" for key, value in sorted(counts.items()))
    return {
        "status": status,
        "evidence": evidence,
        "counts": counts,
        "executable": executable[:8],
        "blocked_context": blocked_context[:8],
        "approval_needed": approval_needed[:8],
        "stale": stale[:10],
    }


def _task_line(task: dict[str, Any]) -> str:
    age = age_label(task.get("age_hours")) if task.get("age_hours") is not None else "unknown age"
    return f"{task.get('id')} — {task.get('title')} ({task.get('status')}, {age})"


def collect_morning_rounds(checks: list[Check], generated_at: str) -> dict[str, Any]:
    health = rollup(checks)
    failing = [c for c in checks if c.status in {"fail", "warn"}]
    current_priorities = SKYVAULT / "Palmer/projects/current-priorities.md"
    active_ledger = SKYVAULT / "Palmer/projects/active-work-ledger.md"
    kanban = kanban_readiness_summary()

    items: list[MorningItem] = [
        MorningItem(
            gate="system",
            status=health["overall"],
            title="Base agent functionality/auth",
            summary=f"{health['label']}: {health['counts'].get('ok', 0)} ok / {health['counts'].get('warn', 0)} warn / {health['counts'].get('fail', 0)} fail",
            evidence="; ".join(f"{c.agent}/{c.app}:{c.status}" for c in failing[:6]) or "No failing health checks in this snapshot.",
            next_action="Repair the first failing check before assigning work that depends on it." if failing else "Safe to use agent-health as substrate for morning rounds.",
        ),
        note_freshness_item(current_priorities, "Current priorities note", "priority"),
        note_freshness_item(active_ledger, "Active work ledger", "truth"),
        MorningItem(
            gate="truth",
            status=kanban["status"],
            title="Kanban execution truth",
            summary=f"Kanban reachable with {sum(kanban.get('counts', {}).values())} active rows; {len(kanban['stale'])} stale, {len(kanban['blocked_context'])} context-thin.",
            evidence=kanban["evidence"],
            next_action="Refresh stale/ambiguous task rows before treating the queue as reliable." if kanban["status"] != "ok" else "Use Kanban as execution queue.",
        ),
        MorningItem(
            gate="execution",
            status="ok" if kanban["executable"] else "warn",
            title="Executable-now Palmer queue",
            summary=f"{len(kanban['executable'])} Palmer task(s) look executable from their current context.",
            evidence="; ".join(_task_line(t) for t in kanban["executable"][:4]) or "No ready/running Palmer task has enough context in the current snapshot.",
            next_action="Start the top executable Palmer task." if kanban["executable"] else "Write missing repo/path/acceptance criteria into the highest-value Palmer task before dispatch.",
        ),
        MorningItem(
            gate="execution",
            status="warn" if kanban["approval_needed"] else "ok",
            title="Approval-needed / Skylar-only queue",
            summary=f"{len(kanban['approval_needed'])} active task(s) appear to need Skylar approval, decision, auth, send, purchase, payment, or scheduling action.",
            evidence="; ".join(_task_line(t) for t in kanban["approval_needed"][:4]) or "No obvious approval queue from active Kanban rows.",
            next_action="Batch these into one morning decision ask; do not leak them into agent busywork." if kanban["approval_needed"] else "No approval batch needed from this snapshot.",
        ),
    ]

    now_items = extract_markdown_items(current_priorities, "Now", 3)
    palmer_safe = extract_markdown_items(current_priorities, "Palmer-owned / Palmer-safe", 3)
    priority_status = "ok" if now_items else "warn"
    items.append(
        MorningItem(
            gate="priority",
            status=priority_status,
            title="Priority synthesis source",
            summary="Top current priorities are extractable from Skyvault." if now_items else "No parseable 'Now' priorities found.",
            evidence=" | ".join(now_items) if now_items else str(current_priorities),
            next_action="Use the first current-priorities lane as the morning operating frame." if now_items else "Refresh current-priorities.md before making a morning call.",
        )
    )

    gate_rollups = {
        gate: status_rollup([item.status for item in items if item.gate == gate])
        for gate in MORNING_GATES
    }
    if health["overall"] == "fail":
        recommendation = "Repair base agent/auth failures first; morning execution is unreliable until the substrate is back."
    elif gate_rollups["truth"] in {"fail", "warn"}:
        recommendation = "Do a truth-refresh pass first: active ledger + stale Kanban rows, then pick work."
    elif kanban["executable"]:
        recommendation = f"Start with {_task_line(kanban['executable'][0])}. Palmer can move this without Skylar if no external approval appears."
    elif kanban["approval_needed"]:
        recommendation = "Batch the approval-needed queue into one decision ask for Skylar; do not start fake setup work."
    else:
        recommendation = "Priority context is present but no executable Palmer task surfaced; convert the top priority into a concrete Kanban task."

    first_90 = palmer_safe[0] if palmer_safe else recommendation
    return {
        "generated_at": generated_at,
        "gate_rollups": gate_rollups,
        "recommendation": recommendation,
        "first_90_minutes": first_90,
        "items": [asdict(item) for item in items],
        "kanban": kanban,
        "priority_now": now_items,
        "palmer_safe": palmer_safe,
    }


def add_check(checks: list[Check], **kwargs: Any) -> None:
    checks.append(Check(**kwargs))


def gog_smoke(checks: list[Check], agent: str, env: dict[str, str], account: str) -> None:
    # Gmail: broad recent search; empty results are okay if the API succeeds.
    rc, out, err, secs = run(
        ["gog", "gmail", "search", "newer_than:7d", "--account", account, "--json", "--max=1", "--no-input"],
        env,
        30,
    )
    data = parse_json(out)
    if rc == 0 and isinstance(data, dict):
        threads = data.get("threads") or []
        subject = threads[0].get("subject", "API returned no recent threads") if threads else "API returned no recent threads"
        status = "ok"
        summary = "Gmail read smoke passed"
        evidence = f"latest subject: {subject}"
        remediation = ""
    else:
        status = "fail"
        summary = "Gmail read smoke failed"
        evidence = redact(err or out, 360)
        remediation = targeted_gog_reauth(agent, account)
    add_check(
        checks,
        id=f"{agent.lower()}-gmail-{account}",
        agent=agent,
        app="Gmail",
        account=account,
        operation="search newer_than:7d",
        status=status,
        summary=summary,
        evidence=evidence,
        remediation=remediation,
        seconds=secs,
    )

    # Calendar: query primary next 7 days; empty events are still healthy.
    rc, out, err, secs = run(
        ["gog", "calendar", "events", "primary", "--account", account, "--days=7", "--json", "--max=1", "--no-input"],
        env,
        30,
    )
    data = parse_json(out)
    if rc == 0 and isinstance(data, dict) and "events" in data:
        events = data.get("events") or []
        event = events[0].get("summary", "calendar reachable; no upcoming events returned") if events else "calendar reachable; no upcoming events returned"
        status = "ok"
        summary = "Calendar read smoke passed"
        evidence = f"sample: {event}"
        remediation = ""
    else:
        status = "fail"
        summary = "Calendar read smoke failed"
        evidence = redact(err or out, 360)
        remediation = targeted_gog_reauth(agent, account)
    add_check(
        checks,
        id=f"{agent.lower()}-calendar-{account}",
        agent=agent,
        app="Google Calendar",
        account=account,
        operation="events primary next 7d",
        status=status,
        summary=summary,
        evidence=evidence,
        remediation=remediation,
        seconds=secs,
    )


def gog_drive_smoke(checks: list[Check], agent: str, env: dict[str, str], account: str) -> None:
    rc, out, err, secs = run(
        ["gog", "drive", "ls", "--account", account, "--json", "--max", "1", "--no-input"],
        env,
        30,
    )
    data = parse_json(out)
    if rc == 0 and data is not None:
        status = "ok"
        summary = "Drive read smoke passed"
        evidence = "Drive API reachable"
        remediation = ""
    else:
        status = "fail"
        summary = "Drive read smoke failed"
        evidence = redact(err or out, 360)
        remediation = targeted_gog_reauth(agent, account)
    add_check(
        checks,
        id=f"{agent.lower()}-drive-{account}",
        agent=agent,
        app="Google Drive",
        account=account,
        operation="drive ls max 1",
        status=status,
        summary=summary,
        evidence=evidence,
        remediation=remediation,
        seconds=secs,
    )


def targeted_gog_reauth(agent: str, account: str) -> str:
    profile = "palmer" if agent == "Palmer" else "echo"
    base = f"/Users/skylarpayne/.hermes/profiles/{profile}"
    return (
        "Targeted repair only — do not wipe every account. Run in a local shell: "
        f"export HERMES_HOME={base}; export HOME={base}/home; "
        "export GOG_CONFIG_DIR=\"$HOME/Library/Application Support/gogcli\"; "
        f"gog auth remove {account} --force --no-input || true; "
        f"gog auth add {account} --manual --services gmail,calendar,drive,docs,sheets --drive-scope readonly --force-consent; "
        f"gog gmail search 'newer_than:7d' --account {account} --json --max=1 --no-input"
    )


def check_outlook(checks: list[Check]) -> None:
    env = os.environ.copy()
    env["MSGCLI_CONFIG_DIR"] = "/Users/skylarpayne/.msgcli"
    rc, out, err, secs = run(["msgcli", "auth", "status", "--no-input", "-o", "json"], env, 20)
    data = parse_json(out)
    if rc == 0 and isinstance(data, dict) and data.get("accounts"):
        valid = [a for a in data.get("accounts", []) if a.get("valid")]
        status = "ok" if valid else "fail"
        evidence = ", ".join(a.get("email", "unknown") for a in valid) or "no valid account"
        remediation = "" if valid else "Run msgcli device-code auth for jacqueline@jacquelinepayne.com with MSGCLI_CONFIG_DIR=/Users/skylarpayne/.msgcli, then rerun this dashboard."
        summary = "Outlook Graph auth valid" if valid else "Outlook Graph auth has no valid accounts"
    else:
        status = "fail"
        summary = "Outlook auth status failed"
        evidence = redact(err or out, 360)
        remediation = "Check MSGCLI_CONFIG_DIR=/Users/skylarpayne/.msgcli, then run msgcli auth/device-code repair."
    add_check(checks, id="echo-outlook-auth", agent="Echo", app="Outlook", account="jacqueline@jacquelinepayne.com", operation="msgcli auth status", status=status, summary=summary, evidence=evidence, remediation=remediation, seconds=secs)

    rc, out, err, secs = run(["msgcli", "mail", "list", "-a", "jacqueline@jacquelinepayne.com", "--limit", "1", "--no-input", "-o", "json"], env, 20)
    data = parse_json(out)
    if rc == 0 and isinstance(data, list):
        status = "ok"
        summary = "Outlook mail read smoke passed"
        subject = data[0].get("subject", "mailbox reachable; no message returned") if data else "mailbox reachable; no message returned"
        evidence = f"latest subject: {subject}"
        remediation = ""
    else:
        status = "fail"
        summary = "Outlook mail read smoke failed"
        evidence = redact(err or out, 360)
        remediation = "Repair msgcli auth before trying Himalaya/app-password paths."
    add_check(checks, id="echo-outlook-mail", agent="Echo", app="Outlook", account="jacqueline@jacquelinepayne.com", operation="mail list limit 1", status=status, summary=summary, evidence=evidence, remediation=remediation, seconds=secs)


def check_asana(checks: list[Check], env: dict[str, str]) -> None:
    token = env.get("ASANA_API_KEY")
    if not token:
        add_check(checks, id="echo-asana", agent="Echo", app="Asana", account="Payne HQ", operation="/users/me", status="fail", summary="ASANA_API_KEY missing", evidence="Echo profile .env has no ASANA_API_KEY", remediation="Add Echo's Asana PAT to /Users/skylarpayne/.hermes/profiles/echo/.env, then smoke-test /users/me.")
        return
    rc, out, err, secs = run(["curl", "-fsS", "-H", f"Authorization: Bearer {token}", "https://app.asana.com/api/1.0/users/me"], env, 20)
    data = parse_json(out)
    if rc == 0 and isinstance(data, dict) and data.get("data"):
        me = data["data"]
        workspaces = ", ".join(w.get("name", "unknown") for w in me.get("workspaces", []))
        status = "ok"
        summary = "Asana API token works"
        evidence = f"identity: {me.get('email', 'unknown')}; workspaces: {workspaces or 'none returned'}"
        remediation = ""
    else:
        status = "fail"
        summary = "Asana API token smoke failed"
        evidence = redact(err or out, 360)
        remediation = "Replace ASANA_API_KEY in Echo profile .env and verify /users/me. Do not print the token."
    add_check(checks, id="echo-asana", agent="Echo", app="Asana", account="Payne HQ", operation="GET /users/me", status=status, summary=summary, evidence=evidence, remediation=remediation, seconds=secs)


def check_canva(checks: list[Check], env: dict[str, str]) -> None:
    if not SKILL_CANVA.exists():
        add_check(checks, id="echo-canva", agent="Echo", app="Canva", account="Echo profile", operation="Canva smoke helper", status="unknown", summary="Canva helper script missing", evidence=str(SKILL_CANVA), remediation="Restore third-party-oauth-integrations skill or verify Canva manually.")
        return
    rc, out, err, secs = run(["python3", str(SKILL_CANVA), "smoke"], env, 30)
    if rc == 0:
        status = "ok"
        summary = "Canva Connect smoke passed"
        evidence = redact(out, 320)
        remediation = ""
    else:
        missing_secret = "Missing CANVA_CLIENT_ID" in (err + out)
        status = "warn" if missing_secret else "fail"
        summary = "Canva not wired in Echo profile" if missing_secret else "Canva smoke failed"
        evidence = redact(err or out, 360)
        remediation = "If Echo needs Canva API access, add CANVA_CLIENT_ID/CANVA_CLIENT_SECRET to Echo .env and run the Canva PKCE flow; until then use browser/manual Canva, not agent API calls."
    add_check(checks, id="echo-canva", agent="Echo", app="Canva", account="Echo profile", operation="canva_oauth.py smoke", status=status, summary=summary, evidence=evidence, remediation=remediation, seconds=secs)


def check_github(checks: list[Check]) -> None:
    env = os.environ.copy()
    env["HOME"] = "/Users/skylarpayne"
    rc, out, err, secs = run(["gh", "auth", "status"], env, 20)
    if rc == 0:
        status = "ok"
        summary = "GitHub CLI auth works"
        evidence = redact(out, 300)
        remediation = ""
    else:
        status = "fail"
        summary = "GitHub CLI auth failed"
        evidence = redact(err or out, 360)
        remediation = "Use HOME=/Users/skylarpayne or GH_CONFIG_DIR=/Users/skylarpayne/.config/gh; if still failing, run gh auth login in Skylar's real home."
    add_check(checks, id="github-gh", agent="Palmer + Echo", app="GitHub", account="skylarbpayne", operation="gh auth status", status=status, summary=summary, evidence=evidence, remediation=remediation, seconds=secs)


def check_kanban_artifactd(checks: list[Check]) -> None:
    rc, out, err, secs = run(["hermes", "-p", "palmer", "kanban", "stats", "--json"], os.environ.copy(), 20)
    data = parse_json(out)
    if rc == 0 and isinstance(data, dict) and data.get("by_status"):
        status = "ok"
        summary = "Hermes Kanban reachable"
        evidence = "statuses: " + ", ".join(f"{k}={v}" for k, v in sorted(data["by_status"].items()))
        remediation = ""
    else:
        status = "fail"
        summary = "Hermes Kanban stats failed"
        evidence = redact(err or out, 360)
        remediation = "Check shared DB /Users/skylarpayne/.hermes/kanban.db and hermes CLI path before blaming workers."
    add_check(checks, id="palmer-kanban", agent="Palmer", app="Hermes Kanban", account="shared root board", operation="kanban stats", status=status, summary=summary, evidence=evidence, remediation=remediation, seconds=secs)


def check_runtime_ops(checks: list[Check]) -> None:
    rc, out, err, secs = run(["ps", "-axo", "pid=,comm=,args="], os.environ.copy(), 20)
    if rc == 0:
        inventory = process_inventory(out)
        counts = inventory["counts"]
        status = "ok" if inventory["total_tracked"] else "warn"
        summary = "Agent runtime processes visible" if inventory["total_tracked"] else "No tracked agent runtime processes found"
        evidence = "; ".join(f"{k}={v}" for k, v in counts.items())
        if inventory.get("samples"):
            evidence += " | samples: " + redact(json.dumps(inventory["samples"]), 520)
        remediation = "" if inventory["total_tracked"] else "Check LaunchDaemons/gateway processes before assuming agents are running."
    else:
        status = "fail"
        summary = "Process inventory failed"
        evidence = redact(err or out, 360)
        remediation = "Run ps manually on the Mac mini and check launchd/service ownership."
    add_check(checks, id="runtime-processes", agent="Palmer host", app="Runtime processes", account="local machine", operation="ps inventory", status=status, summary=summary, evidence=evidence, remediation=remediation, seconds=secs)


def check_codex_auth(checks: list[Check]) -> None:
    start = time.time()
    summary_data = codex_auth_summary(pathlib.Path("/Users/skylarpayne"))
    remediation = "" if summary_data["status"] == "ok" else "Run Codex login/auth repair from Skylar's real HOME before assigning Codex-backed coding work."
    add_check(
        checks,
        id="codex-auth",
        agent="Palmer host",
        app="Codex",
        account="Skylar real HOME",
        operation="auth file presence",
        status=summary_data["status"],
        summary="Codex auth available" if summary_data["status"] == "ok" else "Codex auth needs attention",
        evidence=summary_data["evidence"],
        remediation=remediation,
        seconds=time.time() - start,
    )


def check_cron_health(checks: list[Check]) -> None:
    rc, out, err, secs = run(["hermes", "-p", "palmer", "cron", "list"], os.environ.copy(), 30)
    if rc == 0:
        summary = parse_cron_list_output(out)
        failed = summary["failed_last_runs"]
        status = "fail" if failed else "ok"
        evidence = f"total={summary['total']}; active={summary['active']}; paused={summary['paused']}; failed_last_runs={failed}"
        if summary["failed_jobs"]:
            evidence += "; failed jobs: " + ", ".join(summary["failed_jobs"])
        remediation = "Inspect the failed job output before adding more cron noise." if failed else ""
        title = "Cron jobs healthy" if not failed else "Cron jobs have failed recent runs"
    else:
        status = "fail"
        title = "Cron list failed"
        evidence = redact(err or out, 360)
        remediation = "Run `hermes -p palmer cron status` and inspect scheduler logs."
    add_check(checks, id="cron-health", agent="Palmer", app="Hermes cron", account="palmer profile", operation="cron list", status=status, summary=title, evidence=evidence, remediation=remediation, seconds=secs)


def check_recent_logs(checks: list[Check]) -> None:
    start = time.time()
    base = PALMER_PROFILE / "logs"
    log_paths = [base / name for name in ["gateway.error.log", "errors.log", "agent.log", "gateway.log", "palmer-artifacts.err.log"]]
    summary = summarize_recent_log_errors(log_paths)
    error_lines = summary["error_lines"]
    status = "warn" if error_lines else "ok"
    evidence = f"files_checked={summary['files_checked']}; recent_error_lines={error_lines}"
    if summary["samples"]:
        evidence += " | samples: " + " || ".join(summary["samples"])
    remediation = "Open the named log file(s) and fix the newest repeated error; samples are redacted/truncated." if error_lines else ""
    add_check(checks, id="recent-log-health", agent="Palmer", app="Recent logs", account="palmer profile", operation="scan last log lines", status=status, summary="Recent logs contain errors" if error_lines else "No recent log errors in scanned files", evidence=evidence, remediation=remediation, seconds=time.time() - start)


def check_hermes_update_health(checks: list[Check]) -> None:
    rc, out, err, secs = run(["hermes", "--version"], os.environ.copy(), 25)
    if rc == 0:
        repo = git_remote_summary(HERMES_AGENT_REPO)
        releases = hermes_release_gap(out)
        status = "fail" if "fail" in {repo["status"], releases["status"]} else "warn" if "warn" in {repo["status"], releases["status"]} else "ok"
        evidence = redact(out, 220) + " | " + repo["evidence"] + " | " + releases["evidence"]
        summary = "Hermes CLI/version reachable and release-current" if status == "ok" else "Hermes update state needs review"
        remediation = "" if status == "ok" else "Review installed release vs GitHub releases before running `hermes update`; updates may restart agents."
    else:
        status = "fail"
        summary = "Hermes CLI version check failed"
        evidence = redact(err or out, 360)
        remediation = "Check the active Hermes venv/path before attempting update or gateway work."
    add_check(checks, id="hermes-updates", agent="Palmer host", app="Hermes updates/releases", account="hermes-agent checkout", operation="hermes --version + origin compare", status=status, summary=summary, evidence=evidence, remediation=remediation, seconds=secs)


def check_storage_health(checks: list[Check]) -> None:
    rc, out, err, secs = run(["df", "-Pk", "/Users/skylarpayne"], os.environ.copy(), 20)
    parsed = parse_df_output(out) if rc == 0 else None
    if parsed:
        free = parsed["free_percent"]
        status = "fail" if free < 5 else "warn" if free < 15 else "ok"
        summary = "Storage has safe free space" if status == "ok" else "Mac storage is getting tight"
        hermes_gb = directory_size_gb(pathlib.Path("/Users/skylarpayne/.hermes"))
        artifactd_gb = directory_size_gb(pathlib.Path("/Users/skylarpayne/artifactd"))
        evidence = f"free={parsed['available_gb']}GB ({free}%); used={parsed['used_gb']}GB/{parsed['total_gb']}GB; ~/.hermes={hermes_gb}GB; artifactd={artifactd_gb}GB"
        remediation = "" if status == "ok" else "Run the macOS storage audit skill and prioritize logs/caches/artifact bloat; do not delete project data blind."
    else:
        status = "fail"
        summary = "Storage check failed"
        evidence = redact(err or out, 360)
        remediation = "Run `df -h /Users/skylarpayne` on the host and inspect disk pressure."
    add_check(checks, id="storage-risk", agent="Palmer host", app="Storage risk", account="Mac mini disk", operation="df + managed dir sizes", status=status, summary=summary, evidence=evidence, remediation=remediation, seconds=secs)


def check_compaction_and_memory(checks: list[Check]) -> None:
    start = time.time()
    config_path = PALMER_PROFILE / "config.yaml"
    config_text = config_path.read_text(errors="ignore") if config_path.exists() else ""
    summary = parse_memory_config(config_text)
    compact_ok = summary["context_engine"] == "compressor"
    status = "ok" if compact_ok else "warn"
    evidence = f"context.engine={summary['context_engine'] or 'missing'}; memory.provider={summary['provider'] or 'missing'}; memory_enabled={summary['memory_enabled']}; user_profile_enabled={summary['user_profile_enabled']}"
    remediation = "" if compact_ok else "Set context.engine to compressor after confirming current Hermes config expectations."
    add_check(checks, id="compaction-config", agent="Palmer", app="Compaction", account="palmer profile", operation="config context engine", status=status, summary="Conversation compaction configured" if compact_ok else "Conversation compaction config missing", evidence=evidence, remediation=remediation, seconds=time.time() - start)

    start = time.time()
    provider_ok = summary["provider"] == "hindsight" and summary["memory_enabled"].lower() == "true"
    hindsight_logs = summarize_recent_log_errors([PALMER_PROFILE / "logs/hindsight-embed.log"], max_lines=120)
    status = "ok" if provider_ok and hindsight_logs["error_lines"] == 0 else "warn"
    evidence = f"provider={summary['provider'] or 'missing'}; memory_enabled={summary['memory_enabled']}; hindsight_log_errors={hindsight_logs['error_lines']}"
    if hindsight_logs["samples"]:
        evidence += " | samples: " + " || ".join(hindsight_logs["samples"])
    remediation = "" if status == "ok" else "Check Hindsight provider config and newest hindsight-embed.log errors before trusting long-term recall."
    add_check(checks, id="memory-hindsight", agent="Palmer", app="Memory / Hindsight", account="palmer profile", operation="config + embed log", status=status, summary="Hindsight memory configured" if provider_ok else "Memory provider needs review", evidence=evidence, remediation=remediation, seconds=time.time() - start)


def check_skill_feedback_health(checks: list[Check]) -> None:
    start = time.time()
    summary = skill_usage_summary(PALMER_PROFILE / "skills/.usage.json")
    add_check(checks, id="skill-feedback", agent="Palmer", app="Skills / corrective feedback", account="palmer skill library", operation="skills/.usage.json", status=summary["status"], summary="Skill usage and patch history visible" if summary["status"] == "ok" else "Skill usage tracking needs review", evidence=summary["evidence"], remediation="" if summary["status"] == "ok" else "Verify the skill curator/usage tracker is writing profile-local usage metadata.", seconds=time.time() - start)


def check_openchronicle_entity_health(checks: list[Check]) -> None:
    start = time.time()
    summary = latest_release_summary(OPENCHRONICLE_ROOT)
    add_check(checks, id="openchronicle-release", agent="Palmer", app="OpenChronicle", account="MacBook context bridge", operation="latest release manifest", status=summary["status"], summary="OpenChronicle export is fresh" if summary["status"] == "ok" else "OpenChronicle export may be stale", evidence=summary["evidence"], remediation="" if summary["status"] == "ok" else "Inspect macbook-context-bridge export/import pipeline before relying on activity/entity context.", seconds=time.time() - start)

    start = time.time()
    index = openchronicle_index_summary(OPENCHRONICLE_ROOT)
    add_check(checks, id="openchronicle-index-entities", agent="Palmer", app="OpenChronicle entity extraction", account="current/index.db", operation="SQLite table counts", status=index["status"], summary="OpenChronicle entity tables populated" if index["status"] == "ok" else "OpenChronicle core data present, entity tables missing/unpopulated", evidence=index["evidence"], remediation="" if index["status"] == "ok" else "Wire the context bridge/entity extractor to emit entities, entity_mentions, and entity_edges into the export, or document that Skyvault note extraction is the current source of truth.", seconds=time.time() - start)

    start = time.time()
    entity = skyvault_entity_summary(SKYVAULT)
    add_check(checks, id="entity-graph-skyvault", agent="Palmer", app="Entity graph", account="Skyvault Palmer people", operation="entity note inventory", status=entity["status"], summary="Entity note surface present" if entity["status"] == "ok" else "Entity note surface needs review", evidence=entity["evidence"], remediation="" if entity["status"] == "ok" else "Repair Skyvault Palmer/people index before treating entity graph health as complete.", seconds=time.time() - start)


def http_status_code(url: str) -> tuple[str, str, float]:
    rc, out, err, secs = run(["curl", "-o", "/dev/null", "-sS", "-w", "%{http_code}", url], os.environ.copy(), 20)
    code = (out or "").strip()[-3:] or "000"
    if rc != 0 and code == "000":
        return code, redact(err or out, 260), secs
    return code, "", secs


def artifactd_status_from_codes(root_code: str, probe_code: str, healthy_probe_codes: set[str]) -> dict[str, str]:
    root_healthy_codes = {"200", "401"}
    if root_code == "000" or probe_code == "000" or root_code not in root_healthy_codes:
        return {"status": "fail", "reason": f"root={root_code}; probe={probe_code}"}
    if probe_code in healthy_probe_codes:
        return {"status": "ok", "reason": f"root={root_code}; probe={probe_code}"}
    return {"status": "warn", "reason": f"root={root_code}; probe={probe_code}"}


def check_artifactd_instances(checks: list[Check]) -> None:
    targets = [
        ("Palmer", "artifactd", "artifacts.skylarbpayne.com", "https://artifacts.skylarbpayne.com/", "https://artifacts.skylarbpayne.com/smoke-live", {"200"}, "smoke-live"),
        ("Echo", "artifactd", "artifacts.agoracomms.com", "https://artifacts.agoracomms.com/", "https://artifacts.agoracomms.com/google-auth-repair-center", {"401", "200"}, "google-auth-repair-center"),
    ]
    for agent, app, account, root_url, probe_url, healthy_probe_codes, probe_label in targets:
        root_code, root_error, root_secs = http_status_code(root_url)
        probe_code, probe_error, probe_secs = http_status_code(probe_url)
        result = artifactd_status_from_codes(root_code, probe_code, healthy_probe_codes)
        evidence = f"GET root={root_code}; GET {probe_label}={probe_code}"
        errors = "; ".join(part for part in [root_error, probe_error] if part)
        if errors:
            evidence += f"; curl={redact(errors, 220)}"
        if result["status"] == "ok":
            summary = f"{agent} artifactd protected/public routes healthy"
            remediation = ""
        elif result["status"] == "warn":
            summary = f"{agent} artifactd reachable but expected probe route needs review"
            remediation = f"Service and tunnel appear reachable; verify the expected {probe_label} artifact/route before changing LaunchDaemons."
        else:
            summary = f"{agent} artifactd public route failed"
            remediation = f"Check local {agent} artifactd service/LaunchDaemon and Cloudflare tunnel before changing dashboard code."
        add_check(checks, id=f"{agent.lower()}-artifactd-public", agent=agent, app=app, account=account, operation=f"HTTPS root + {probe_label}", status=result["status"], summary=summary, evidence=evidence, remediation=remediation, seconds=root_secs + probe_secs)


def collect_checks() -> list[Check]:
    checks: list[Check] = []
    palmer_env = load_profile_env(PALMER_PROFILE)
    echo_env = load_profile_env(ECHO_PROFILE)

    palmer_google = [
        "skylar.b.payne@gmail.com",
        "me@skylarbpayne.com",
        "jacquelineandskylar@gmail.com",
        "palmer@skylarbpayne.com",
    ]
    echo_google = [
        "jacquelineaguilar030@gmail.com",
        "jaguilar@y2lef.org",
    ]
    for account in palmer_google:
        gog_smoke(checks, "Palmer", palmer_env, account)
    # Drive checks are slower/noisier; smoke the two accounts most likely used for file work.
    for account in ["me@skylarbpayne.com", "palmer@skylarbpayne.com"]:
        gog_drive_smoke(checks, "Palmer", palmer_env, account)
    for account in echo_google:
        gog_smoke(checks, "Echo", echo_env, account)
    gog_drive_smoke(checks, "Echo", echo_env, "jacquelineaguilar030@gmail.com")

    check_outlook(checks)
    check_asana(checks, echo_env)
    check_canva(checks, echo_env)
    check_github(checks)
    check_kanban_artifactd(checks)
    check_artifactd_instances(checks)
    check_runtime_ops(checks)
    check_codex_auth(checks)
    check_hermes_update_health(checks)
    check_storage_health(checks)
    check_cron_health(checks)
    check_recent_logs(checks)
    check_compaction_and_memory(checks)
    check_skill_feedback_health(checks)
    check_openchronicle_entity_health(checks)
    return checks


def rollup(checks: list[Check]) -> dict[str, Any]:
    counts = {"ok": 0, "warn": 0, "fail": 0, "unknown": 0}
    for check in checks:
        counts[check.status] = counts.get(check.status, 0) + 1
    if counts["fail"]:
        overall = "fail"
        label = "Broken checks need attention"
    elif counts["warn"]:
        overall = "warn"
        label = "Mostly healthy; one non-critical gap"
    else:
        overall = "ok"
        label = "Healthy"
    return {"overall": overall, "label": label, "counts": counts}


def repair_queue(checks: list[Check], morning: dict[str, Any] | None = None) -> list[dict[str, str | int]]:
    """Convert raw warnings into a small operator queue.

    Health dashboards are useful only if the next move is obvious. This layer is
    intentionally opinionated: separate optional capability gaps from real repair
    work, name the likely finding, and specify what should be automated so the
    same warning gets quieter over time.
    """
    templates: dict[str, dict[str, str | int]] = {
        "recent-log-health": {
            "priority": 10,
            "investigation": "Log warning is dominated by Hindsight daemon churn / port 9712 conflict signatures. This may be stale log noise unless the live Hindsight smoke fails.",
            "recommended_action": "Run a live Hindsight recall/retain smoke plus a port-owner check. If smoke passes, downgrade stale log samples; if it fails, restart/repair the Hindsight daemon owner before trusting memory.",
            "feedback_loop": "Automate a Hindsight live-smoke check and error aging so old gateway log lines stop creating fake daily repair work.",
        },
        "memory-hindsight": {
            "priority": 11,
            "investigation": "Hindsight config is present but recent embed/writer errors can make recall look flaky.",
            "recommended_action": "Run recall + retain smoke before assigning memory-heavy work; repair daemon startup only if the live smoke fails.",
            "feedback_loop": "Automate live recall/retain smoke and only page when the actual memory operation fails, not merely when a historical log contains errors.",
        },
        "hermes-updates": {
            "priority": 20,
            "investigation": "Hermes checkout is behind origin, but the installed public release is not necessarily behind. This is review work, not an update-now alarm.",
            "recommended_action": "Write a compact Hermes update change memo from the new commits/release notes; update only if it contains fixes that matter for current Palmer/Echo reliability.",
            "feedback_loop": "Automate a weekly Hermes update memo with commit buckets and a go/no-go recommendation instead of showing raw commit lag every morning.",
        },
        "openchronicle-index-entities": {
            "priority": 30,
            "investigation": "OpenChronicle index is readable and core capture/session/timeline data exists; the warning is specifically that entity tables are missing or empty.",
            "recommended_action": "Decide whether OpenChronicle entity extraction is a required health gate. If yes, implement/populate entities/entity_mentions/entity_edges; if no, mark this as a capability gap and use Skyvault entity notes as the current truth.",
            "feedback_loop": "Automate stage-level OpenChronicle checks: export freshness, core table counts, entity-table counts, and extractor last-run evidence separately.",
        },
        "echo-canva": {
            "priority": 80,
            "investigation": "Echo Canva API credentials are absent. This is optional unless Jacqueline/Echo has an active workflow requiring Canva API access.",
            "recommended_action": "Do not spend repair time here today unless Echo needs Canva. If needed, collect Canva app credentials and run the PKCE OAuth setup behind an approval gate.",
            "feedback_loop": "Move optional integrations into an 'optional capability gap' bucket so they do not pollute the daily repair queue.",
        },
    }

    queue: list[dict[str, str | int]] = []
    for check in checks:
        if check.status not in {"warn", "fail", "unknown"}:
            continue
        template = templates.get(
            check.id,
            {
                "priority": 50 if check.status == "warn" else 5,
                "investigation": f"{check.summary}. Evidence: {check.evidence}",
                "recommended_action": check.remediation or "Run the smallest live smoke that proves whether this is a real current failure.",
                "feedback_loop": "If this recurs, add a targeted smoke/check so future runs classify root cause instead of repeating the same generic warning.",
            },
        )
        queue.append(
            {
                "id": check.id,
                "status": check.status,
                "title": f"{check.agent} / {check.app}",
                "summary": check.summary,
                "priority": int(template["priority"]),
                "investigation": str(template["investigation"]),
                "recommended_action": str(template["recommended_action"]),
                "feedback_loop": str(template["feedback_loop"]),
            }
        )

    morning = morning or {}
    for item in morning.get("items", []):
        if item.get("status") not in {"warn", "fail", "unknown"}:
            continue
        title = str(item.get("title", "Morning readiness item"))
        if title == "Base agent functionality/auth":
            continue
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "morning-item"
        queue.append(
            {
                "id": f"truth-{slug}",
                "status": str(item.get("status", "warn")),
                "title": title,
                "summary": str(item.get("summary", "")),
                "priority": 25,
                "investigation": str(item.get("evidence", "Truth/context source needs refresh.")),
                "recommended_action": str(item.get("next_action", "Refresh the source of truth and rerun the dashboard.")),
                "feedback_loop": "Automate freshness/writeback checks so stale truth becomes a Palmer repair action, not ambient anxiety for Skylar.",
            }
        )

    return sorted(queue, key=lambda item: (int(item["priority"]), STATUS_ORDER.get(str(item["status"]), 9), str(item["id"])))


def esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def render_html(checks: list[Check], generated_at: str, morning: dict[str, Any] | None = None) -> str:
    summary = rollup(checks)
    morning = morning or {
        "gate_rollups": {},
        "recommendation": "Morning rounds readiness was not collected for this render.",
        "first_90_minutes": "Collect morning-rounds inputs, then rerender.",
        "items": [],
        "kanban": {"executable": [], "blocked_context": [], "approval_needed": [], "stale": []},
        "priority_now": [],
        "palmer_safe": [],
    }
    by_agent: dict[str, list[Check]] = {}
    for check in sorted(checks, key=lambda c: (c.agent, STATUS_ORDER.get(c.status, 9), c.app, c.account)):
        by_agent.setdefault(check.agent, []).append(check)
    repairs = repair_queue(checks, morning)

    def badge(status: str) -> str:
        return f'<span class="badge {esc(status)}">{esc(status.upper())}</span>'

    def status_dot(status: str) -> str:
        return f'<span class="dot {esc(status)}"></span>'

    def check_card(c: Check) -> str:
        remediation = f'<details><summary>Repair path</summary><pre>{esc(c.remediation)}</pre></details>' if c.remediation else ""
        return f"""
        <article class="check {esc(c.status)}" id="{esc(c.id)}">
          <div class="check-head">
            <div>{status_dot(c.status)}<strong>{esc(c.app)}</strong> <span class="muted">· {esc(c.account)}</span></div>
            {badge(c.status)}
          </div>
          <div class="summary">{esc(c.summary)}</div>
          <div class="meta"><span>{esc(c.operation)}</span><span>{c.seconds:.2f}s</span></div>
          <p class="evidence">{esc(c.evidence)}</p>
          {remediation}
        </article>
        """

    grouped = "\n".join(
        f"""
        <section class="agent">
          <h2>{esc(agent)}</h2>
          <div class="checks">{''.join(check_card(c) for c in items)}</div>
        </section>
        """
        for agent, items in by_agent.items()
    )

    failing = [c for c in checks if c.status in {"fail", "warn"}]
    incident_rows = "".join(
        f"<li>{badge(c.status)} <strong>{esc(c.agent)} / {esc(c.app)}</strong> — {esc(c.account)}: {esc(c.summary)}</li>"
        for c in failing
    ) or "<li>Nothing currently broken. Weirdly peaceful.</li>"

    def morning_card(item: dict[str, Any]) -> str:
        return f"""
        <article class="morning-item {esc(item.get('status', 'unknown'))}">
          <div class="check-head"><strong>{esc(item.get('title', 'Untitled'))}</strong>{badge(item.get('status', 'unknown'))}</div>
          <div class="summary">{esc(item.get('summary', ''))}</div>
          <p class="evidence">{esc(item.get('evidence', ''))}</p>
          <p><strong>Next:</strong> {esc(item.get('next_action', ''))}</p>
        </article>
        """

    gate_cards = "".join(
        f"<div class=\"gate {esc(morning.get('gate_rollups', {}).get(gate, 'unknown'))}\"><span>{esc(gate)}</span>{badge(morning.get('gate_rollups', {}).get(gate, 'unknown'))}</div>"
        for gate in MORNING_GATES
    )
    morning_items = "".join(morning_card(item) for item in morning.get("items", [])) or "<p class=\"muted\">Morning readiness details were not collected.</p>"

    def task_queue(title: str, tasks: list[dict[str, Any]], empty: str) -> str:
        rows = "".join(f"<li>{esc(_task_line(task))}</li>" for task in tasks) or f"<li>{esc(empty)}</li>"
        return f"<article class=\"panel queue\"><h3>{esc(title)}</h3><ul>{rows}</ul></article>"

    kanban_queues = "".join(
        [
            task_queue("Executable now", morning.get("kanban", {}).get("executable", []), "No executable Palmer task surfaced."),
            task_queue("Blocked by context", morning.get("kanban", {}).get("blocked_context", []), "No context-thin Palmer task surfaced."),
            task_queue("Approval needed", morning.get("kanban", {}).get("approval_needed", []), "No approval batch surfaced."),
            task_queue("Stale truth", morning.get("kanban", {}).get("stale", []), "No stale active task surfaced."),
        ]
    )

    def repair_card(item: dict[str, Any]) -> str:
        return f"""
        <article class="repair {esc(item.get('status', 'unknown'))}" id="repair-{esc(item.get('id', 'unknown'))}">
          <div class="check-head"><strong>{esc(item.get('title', 'Untitled'))}</strong>{badge(str(item.get('status', 'unknown')))}</div>
          <div class="summary">{esc(item.get('summary', ''))}</div>
          <p><strong>Investigated finding:</strong> {esc(item.get('investigation', ''))}</p>
          <p><strong>Recommended action:</strong> {esc(item.get('recommended_action', ''))}</p>
          <p><strong>Automate next:</strong> {esc(item.get('feedback_loop', ''))}</p>
        </article>
        """

    repair_cards = "".join(repair_card(item) for item in repairs) or "<p class=\"muted\">No repairs to clear. Keep the loop boring.</p>"

    json_blob = esc(json.dumps({"generated_at": generated_at, "summary": summary, "morning_rounds": morning, "repair_queue": repairs, "checks": [asdict(c) for c in checks]}, indent=2))

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Morning Rounds / Palmer + Echo Ops Console</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #080b12; --panel: #111827; --panel2: #0f172a; --text: #e5e7eb; --muted: #94a3b8;
      --ok: #22c55e; --warn: #f59e0b; --fail: #ef4444; --unknown: #94a3b8; --line: rgba(148,163,184,.22);
      --accent: #38bdf8;
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: radial-gradient(circle at top left, rgba(56,189,248,.16), transparent 34rem), var(--bg); color: var(--text); }}
    header {{ padding: 36px 28px 20px; max-width: 1180px; margin: 0 auto; }}
    h1 {{ margin: 0; font-size: clamp(32px, 5vw, 58px); letter-spacing: -.05em; line-height: .95; }}
    h2 {{ margin: 0 0 14px; font-size: 22px; letter-spacing: -.02em; }}
    h3 {{ margin: 0 0 10px; }}
    p {{ color: var(--muted); }}
    code, pre {{ font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace; }}
    .topline {{ display: flex; gap: 12px; align-items: center; flex-wrap: wrap; margin-bottom: 18px; }}
    .hero {{ display: grid; grid-template-columns: 1.2fr .8fr; gap: 18px; margin-top: 24px; }}
    .panel {{ background: linear-gradient(180deg, rgba(17,24,39,.96), rgba(15,23,42,.94)); border: 1px solid var(--line); border-radius: 22px; padding: 20px; box-shadow: 0 24px 80px rgba(0,0,0,.35); }}
    .status-big {{ display: flex; align-items: center; gap: 14px; font-size: 25px; font-weight: 750; }}
    .counts {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin-top: 18px; }}
    .count {{ background: rgba(15,23,42,.85); border: 1px solid var(--line); border-radius: 16px; padding: 14px; }}
    .count b {{ display: block; font-size: 28px; }}
    .count span {{ color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .08em; }}
    main {{ max-width: 1180px; margin: 0 auto; padding: 0 28px 48px; }}
    .agent {{ margin-top: 22px; }}
    .checks {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }}
    .check {{ background: rgba(15,23,42,.88); border: 1px solid var(--line); border-left: 5px solid var(--unknown); border-radius: 18px; padding: 16px; min-height: 172px; }}
    .check.ok {{ border-left-color: var(--ok); }} .check.warn {{ border-left-color: var(--warn); }} .check.fail {{ border-left-color: var(--fail); }}
    .check-head {{ display: flex; justify-content: space-between; gap: 12px; align-items: center; }}
    .summary {{ margin-top: 10px; font-weight: 650; }}
    .meta {{ display: flex; justify-content: space-between; gap: 8px; color: var(--muted); font-size: 12px; margin-top: 8px; }}
    .evidence {{ margin: 12px 0 0; font-size: 13px; line-height: 1.45; }}
    .muted {{ color: var(--muted); }}
    .badge {{ border: 1px solid currentColor; border-radius: 999px; padding: 4px 9px; font-size: 11px; font-weight: 800; letter-spacing: .08em; }}
    .badge.ok {{ color: var(--ok); }} .badge.warn {{ color: var(--warn); }} .badge.fail {{ color: var(--fail); }} .badge.unknown {{ color: var(--unknown); }}
    .dot {{ display: inline-block; width: 10px; height: 10px; border-radius: 50%; background: var(--unknown); margin-right: 9px; box-shadow: 0 0 18px currentColor; }}
    .dot.ok {{ background: var(--ok); color: var(--ok); }} .dot.warn {{ background: var(--warn); color: var(--warn); }} .dot.fail {{ background: var(--fail); color: var(--fail); }}
    details {{ margin-top: 12px; }}
    summary {{ cursor: pointer; color: #bae6fd; font-weight: 700; }}
    pre {{ white-space: pre-wrap; overflow-wrap: anywhere; background: rgba(2,6,23,.85); border: 1px solid var(--line); border-radius: 14px; padding: 12px; color: #d1fae5; font-size: 12px; line-height: 1.45; }}
    .playbook {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; margin-top: 22px; }}
    .morning {{ margin-top: 22px; }}
    .gates {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; margin: 16px 0; }}
    .gate {{ display: flex; justify-content: space-between; align-items: center; gap: 8px; background: rgba(15,23,42,.85); border: 1px solid var(--line); border-top: 4px solid var(--unknown); border-radius: 16px; padding: 12px; text-transform: uppercase; letter-spacing: .08em; font-size: 12px; color: var(--muted); }}
    .gate.ok {{ border-top-color: var(--ok); }} .gate.warn {{ border-top-color: var(--warn); }} .gate.fail {{ border-top-color: var(--fail); }}
    .recommendation {{ border-color: rgba(56,189,248,.5); background: linear-gradient(180deg, rgba(14,116,144,.22), rgba(15,23,42,.94)); }}
    .morning-grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; margin-top: 14px; }}
    .morning-item {{ background: rgba(15,23,42,.88); border: 1px solid var(--line); border-left: 5px solid var(--unknown); border-radius: 18px; padding: 16px; }}
    .morning-item.ok {{ border-left-color: var(--ok); }} .morning-item.warn {{ border-left-color: var(--warn); }} .morning-item.fail {{ border-left-color: var(--fail); }}
    .repair-grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; margin-top: 14px; }}
    .repair {{ background: rgba(15,23,42,.9); border: 1px solid var(--line); border-left: 5px solid var(--unknown); border-radius: 18px; padding: 16px; }}
    .repair.ok {{ border-left-color: var(--ok); }} .repair.warn {{ border-left-color: var(--warn); }} .repair.fail {{ border-left-color: var(--fail); }}
    .repair p {{ margin: 10px 0 0; font-size: 13px; line-height: 1.45; }}
    .repair strong {{ color: #e0f2fe; }}
    .queues {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 14px; margin-top: 14px; }}
    .queue ul {{ padding-left: 18px; }}
    .queue li {{ margin: 8px 0; color: var(--muted); font-size: 13px; }}
    .playbook li {{ margin: 8px 0; color: var(--muted); }}
    .incident li {{ margin: 9px 0; color: var(--muted); }}
    .footer {{ margin-top: 22px; color: var(--muted); font-size: 13px; }}
    @media (max-width: 850px) {{ .hero, .checks, .playbook, .morning-grid, .repair-grid, .queues {{ grid-template-columns: 1fr; }} .counts, .gates {{ grid-template-columns: repeat(2,1fr); }} }}
  </style>
</head>
<body>
  <header>
    <div class="topline">{badge(summary['overall'])}<span class="muted">Generated {esc(generated_at)}</span><span class="muted">Protected artifact: /{ARTIFACT_SLUG}</span></div>
    <h1>Morning rounds<br/>readiness cockpit</h1>
    <div class="hero">
      <section class="panel">
        <div class="status-big">{status_dot(summary['overall'])}{esc(summary['label'])}</div>
        <p>This is agent-health growing up into morning rounds: base auth/functionality, project/task truth freshness, priority context, and task-level execution context. The job is not pretty lights; it is deciding what Palmer/Echo can actually do before Skylar starts the day.</p>
        <div class="counts">
          <div class="count"><b>{summary['counts'].get('ok', 0)}</b><span>ok</span></div>
          <div class="count"><b>{summary['counts'].get('warn', 0)}</b><span>warn</span></div>
          <div class="count"><b>{summary['counts'].get('fail', 0)}</b><span>fail</span></div>
          <div class="count"><b>{summary['counts'].get('unknown', 0)}</b><span>unknown</span></div>
        </div>
      </section>
      <section class="panel incident">
        <h3>What needs attention</h3>
        <ul>{incident_rows}</ul>
      </section>
    </div>
  </header>
  <main>
    <section class="morning">
      <article class="panel recommendation">
        <h2>Morning recommendation</h2>
        <p class="summary">{esc(morning.get('recommendation', ''))}</p>
        <p><strong>First 90 minutes:</strong> {esc(morning.get('first_90_minutes', ''))}</p>
        <div class="gates">{gate_cards}</div>
      </article>
      <div class="morning-grid">{morning_items}</div>
      <div class="queues">{kanban_queues}</div>
    </section>

    <section class="morning">
      <article class="panel recommendation">
        <h2>Issue clearing board</h2>
        <p class="summary">Warnings are not chores for Skylar. Palmer should first investigate whether each warning is a real current failure, an optional capability gap, stale telemetry, or a truth-refresh item — then either repair it or make the next run smarter.</p>
      </article>
      <div class="repair-grid">{repair_cards}</div>
    </section>

    {grouped}

    <section class="playbook">
      <article class="panel">
        <h3>V0 coverage</h3>
        <ul>
          <li>Hermes updates/releases: CLI version plus local checkout vs origin.</li>
          <li>Host risk: storage pressure, runtime process inventory, cron, recent logs, Codex auth.</li>
          <li>Agent substrate: gog/Google, Outlook, Asana, Canva, GitHub, Kanban, Palmer/Echo artifactd.</li>
          <li>Knowledge layer: compaction/memory/Hindsight config, skill feedback/patch usage, OpenChronicle/entity graph export, Skyvault entity notes.</li>
        </ul>
      </article>
      <article class="panel">
        <h3>Email auth repair rule</h3>
        <ol>
          <li>Identify the failing profile and account first. Palmer and Echo credentials are separate.</li>
          <li>Smoke-test Gmail and Calendar for that exact account. Empty results are fine; auth errors are not.</li>
          <li>Remove and re-auth only the broken account token. Do not revoke every Google account unless the whole keyring is busted.</li>
          <li>After the browser/manual auth flow, verify with live Gmail + Calendar commands before declaring victory.</li>
        </ol>
      </article>
      <article class="panel">
        <h3>No-SSH repair center — next layer</h3>
        <ol>
          <li>This page already tells you exactly which account is broken.</li>
          <li>The next version should start the targeted `gog auth add --manual` flow from the browser.</li>
          <li>It should show the Google consent URL, accept the final localhost redirect URL, exchange it server-side, and rerun smoke tests.</li>
          <li>Credential mutation needs an explicit approval gate; OAuth codes/tokens must stay transient and out of notes/logs.</li>
        </ol>
      </article>
      <article class="panel">
        <h3>Refresh this artifact</h3>
        <pre>cd /Users/skylarpayne/artifactd
. .venv/bin/activate
python scripts/agent_health_dashboard.py --out dist/agent-health
python -c 'from pathlib import Path; from artifactd.store import ArtifactStore; ArtifactStore("/Users/skylarpayne/.hermes/artifacts").deploy(Path("dist/agent-health"), slug="agent-health", title="Morning Rounds readiness cockpit", description="Protected morning readiness cockpit layered on agent-health: system/auth checks, truth freshness, priority synthesis, execution-context sufficiency, and recommended first move.", tags=["ops", "health", "morning-rounds", "palmer", "echo"], pinned=True, auth_mode="profile")'</pre>
      </article>
    </section>

    <section class="panel" style="margin-top:22px">
      <h3>Raw redacted snapshot</h3>
      <details><summary>Open JSON</summary><pre>{json_blob}</pre></details>
    </section>
    <p class="footer">Truth note: this artifact is a snapshot, not a daemon. If it gets reused, promote it to a scheduled refresh/checker and only ping Skylar on failures or re-auth-needed states.</p>
  </main>
</body>
</html>
"""


def write_outputs(checks: list[Check], out_dir: pathlib.Path, generated_at: str) -> None:
    morning = collect_morning_rounds(checks, generated_at)
    repairs = repair_queue(checks, morning)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "index.html").write_text(render_html(checks, generated_at, morning), encoding="utf-8")
    (out_dir / "health.json").write_text(
        json.dumps({"generated_at": generated_at, "summary": rollup(checks), "morning_rounds": morning, "repair_queue": repairs, "checks": [asdict(c) for c in checks]}, indent=2),
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="dist/agent-health", help="Output directory containing index.html + health.json")
    parser.add_argument("--json", action="store_true", help="Print health JSON summary to stdout")
    args = parser.parse_args()

    generated_at = datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d %H:%M:%S %Z")
    checks = collect_checks()
    write_outputs(checks, pathlib.Path(args.out), generated_at)
    result = {"generated_at": generated_at, "summary": rollup(checks), "out": str(pathlib.Path(args.out).resolve())}
    print(json.dumps(result, indent=2) if args.json else f"wrote {args.out} ({result['summary']['label']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
