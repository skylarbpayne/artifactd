#!/usr/bin/env python3
"""Generate artifactd-ready project cockpits from Skyvault + Kanban + artifact metadata.

Truth stays in Skyvault/Kanban. This script builds a static HTML command surface
with source links and local-only UI affordances.
"""
from __future__ import annotations

import argparse
import dataclasses
import html
import json
import re
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


DEFAULT_VAULT = Path("/Users/skylarpayne/skyvault")
DEFAULT_KANBAN_DB = Path("/Users/skylarpayne/.hermes/kanban.db")
DEFAULT_ARTIFACT_DB = Path("/Users/skylarpayne/.hermes/artifacts/artifacts.db")
DEFAULT_PUBLIC_BASE_URL = "https://artifacts.skylarbpayne.com"
DEFAULT_DEPLOY_SLUG = "project-dashboards"


@dataclass(frozen=True)
class ProjectSpec:
    key: str
    name: str
    slug: str
    aliases: tuple[str, ...]
    note_paths: tuple[str, ...]
    preferred_sections: tuple[str, ...]
    accent: str
    owner_hint: str
    objective: str


PROJECTS: dict[str, ProjectSpec] = {
    "htv": ProjectSpec(
        key="htv",
        name="Hack the Valley",
        slug="project-dashboard-htv",
        aliases=("hack the valley", "htv"),
        note_paths=(
            "1_Projects/Hack the Valley.md",
            "1_Projects/Hack the Valley This Week Lock List - 2026-05-14.md",
            "1_Projects/Hack the Valley Acceptance Email + Waiver Requirements - 2026-05-14.md",
            "1_Projects/Hack the Valley Replit Beginner Workshop Draft - 2026-05-14.md",
        ),
        preferred_sections=("TL;DR", "Status", "Current truth", "Done When", "Team", "Recent Emails"),
        accent="#7dd3fc",
        owner_hint="Skylar + HTV organizers",
        objective="Lock the execution details for the May 30 hackathon without losing the source trail.",
    ),
    "wedding": ProjectSpec(
        key="wedding",
        name="Our Wedding",
        slug="project-dashboard-wedding",
        aliases=("our wedding", "wedding", "jacqueline", "matinae", "fairy godmother", "the social vibe", "gabe"),
        note_paths=(
            "1_Projects/Our Wedding.md",
            "1_Projects/Our Wedding - Lay of Land Audit 2026-05-04.md",
            "1_Projects/J + S Wedding 💕.md",
            "1_Projects/Wedding Planning 2027.md",
        ),
        preferred_sections=("TL;DR", "Live work", "Current truth", "Key Deadlines", "Vendor follow-up", "Key People"),
        accent="#f9a8d4",
        owner_hint="Skylar + Jacqueline",
        objective="Keep real wedding blockers visible while Skyvault remains the planning record.",
    ),
}


@dataclass
class MarkdownNote:
    path: Path
    rel_path: str
    frontmatter: dict[str, Any]
    body: str
    sections: dict[str, str]
    wikilinks: list[str]
    checkboxes: list[dict[str, str]]


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 4)
    if end == -1:
        return {}, text
    raw = text[4:end].strip("\n")
    body = text[end + len("\n---") :].lstrip("\n")
    data: dict[str, Any] = {}
    current_key: str | None = None
    for line in raw.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith("  - ") and current_key:
            data.setdefault(current_key, [])
            if isinstance(data[current_key], list):
                data[current_key].append(line.split("- ", 1)[1].strip())
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        current_key = key
        if value.startswith("[") and value.endswith("]"):
            inner = value[1:-1].strip()
            data[key] = [item.strip().strip('"\'') for item in inner.split(",") if item.strip()]
        elif value == "":
            data[key] = []
        else:
            data[key] = value.strip('"\'')
    return data, body


def extract_sections(markdown: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current = "Overview"
    sections[current] = []
    for line in markdown.splitlines():
        heading = re.match(r"^(#{2,4})\s+(.+?)\s*$", line)
        if heading:
            current = re.sub(r"\s+[#]+$", "", heading.group(2)).strip()
            sections.setdefault(current, [])
            continue
        sections.setdefault(current, []).append(line)
    return {k: "\n".join(v).strip() for k, v in sections.items() if "\n".join(v).strip()}


def extract_wikilinks(text: str) -> list[str]:
    seen: set[str] = set()
    links: list[str] = []
    for raw in re.findall(r"\[\[([^\]]+)\]\]", text):
        name = raw.split("|", 1)[0].strip()
        if name and name not in seen:
            seen.add(name)
            links.append(name)
    return links


def extract_checkboxes(text: str) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for line in text.splitlines():
        match = re.match(r"\s*- \[( |x|X)\]\s+(.+)", line)
        if match:
            out.append({"done": str(match.group(1).lower() == "x"), "text": match.group(2).strip()})
    return out


def read_note(vault: Path, rel_path: str) -> MarkdownNote | None:
    path = vault / rel_path
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    frontmatter, body = parse_frontmatter(text)
    sections = extract_sections(body)
    return MarkdownNote(
        path=path,
        rel_path=rel_path,
        frontmatter=frontmatter,
        body=body,
        sections=sections,
        wikilinks=extract_wikilinks(text),
        checkboxes=extract_checkboxes(text),
    )


def first_section_line(note: MarkdownNote, names: Iterable[str], limit: int = 280) -> str:
    for name in names:
        content = note.sections.get(name, "")
        for line in content.splitlines():
            line = clean_markdown_line(line)
            if line:
                return truncate(line, limit)
    return ""


def clean_markdown_line(line: str) -> str:
    line = line.strip().strip("|").strip()
    line = re.sub(r"^[-*]\s+", "", line)
    line = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", line)
    line = re.sub(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]", r"\1", line)
    line = line.replace("**", "").replace("`", "")
    if not line or set(line) <= {"-", "|", ":", " "}:
        return ""
    return line


def truncate(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def table_from_db(db_path: Path, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    if not db_path.exists():
        return []
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in con.execute(query, params).fetchall()]
    finally:
        con.close()


def load_kanban_tasks(db_path: Path, aliases: tuple[str, ...]) -> list[dict[str, Any]]:
    clauses = []
    params: list[str] = []
    for alias in aliases:
        clauses.append("lower(title || ' ' || coalesce(body, '')) LIKE ?")
        params.append(f"%{alias.lower()}%")
    where = " OR ".join(clauses) or "1=0"
    rows = table_from_db(
        db_path,
        f"""
        SELECT id, title, status, priority, assignee, created_at, completed_at,
               substr(coalesce(body,''), 1, 900) AS body
        FROM tasks
        WHERE {where}
        ORDER BY CASE status
            WHEN 'running' THEN 0 WHEN 'blocked' THEN 1 WHEN 'ready' THEN 2
            WHEN 'todo' THEN 3 WHEN 'done' THEN 4 ELSE 5 END,
            priority DESC, coalesce(completed_at, created_at) DESC
        LIMIT 90
        """,
        tuple(params),
    )
    active = [r for r in rows if r["status"] != "done"]
    done = [r for r in rows if r["status"] == "done"][:12]
    return active + done


def load_artifacts(db_path: Path, aliases: tuple[str, ...]) -> list[dict[str, Any]]:
    clauses = []
    params: list[str] = []
    for alias in aliases:
        clauses.append("lower(slug || ' ' || title || ' ' || description) LIKE ?")
        params.append(f"%{alias.lower()}%")
    where = " OR ".join(clauses) or "1=0"
    return table_from_db(
        db_path,
        f"""
        SELECT slug, title, description, created_at, updated_at,
               CASE WHEN password_hash IS NULL THEN 0 ELSE 1 END AS protected
        FROM artifacts
        WHERE {where}
        ORDER BY updated_at DESC
        LIMIT 30
        """,
        tuple(params),
    )


def resolve_entity_paths(vault: Path, names: Iterable[str]) -> list[dict[str, str]]:
    people_dir = vault / "3_Resources" / "CRM" / "People"
    companies_dir = vault / "3_Resources" / "CRM" / "Companies"
    candidates: list[Path] = []
    for base in (people_dir, companies_dir):
        if base.exists():
            candidates.extend(base.glob("*.md"))
    by_norm = {normalize_entity_name(path.stem): path for path in candidates}
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for name in names:
        norm = normalize_entity_name(name)
        path = by_norm.get(norm)
        if not path:
            # tolerate hyphen/space duplicates and partial names
            for key, candidate in by_norm.items():
                if norm and (norm in key or key in norm):
                    path = candidate
                    break
        if path and str(path) not in seen:
            seen.add(str(path))
            out.append({"name": name, "path": str(path), "rel_path": str(path.relative_to(vault))})
        elif name not in seen:
            seen.add(name)
            out.append({"name": name, "path": "", "rel_path": "unresolved wikilink/frontmatter entity"})
    return out[:30]


def normalize_entity_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", name.lower())


def build_project_data(spec: ProjectSpec, vault: Path, kanban_db: Path, artifact_db: Path) -> dict[str, Any]:
    notes = [note for rel in spec.note_paths if (note := read_note(vault, rel))]
    primary = notes[0] if notes else None
    key_people: list[str] = []
    if primary:
        fm_people = primary.frontmatter.get("key_people") or []
        if isinstance(fm_people, str):
            key_people.extend([p.strip() for p in fm_people.split(",") if p.strip()])
        elif isinstance(fm_people, list):
            key_people.extend([str(p).strip() for p in fm_people if str(p).strip()])
        key_people.extend(primary.wikilinks)
    entities = resolve_entity_paths(vault, key_people)
    tasks = load_kanban_tasks(kanban_db, spec.aliases)
    artifacts = load_artifacts(artifact_db, spec.aliases)
    source_notes = []
    for note in notes:
        source_notes.append(
            {
                "rel_path": note.rel_path,
                "path": str(note.path),
                "status": note.frontmatter.get("status", ""),
                "priority": note.frontmatter.get("priority", ""),
                "due": note.frontmatter.get("due", ""),
                "summary": first_section_line(note, ("TL;DR", "Current truth", "Status", "Live work", "Overview")),
                "sections": {name: note.sections.get(name, "") for name in spec.preferred_sections if note.sections.get(name)},
                "checkboxes": note.checkboxes[:40],
            }
        )
    live_tasks = [t for t in tasks if t["status"] in {"running", "blocked", "ready", "todo"}]
    done_tasks = [t for t in tasks if t["status"] == "done"]
    blockers = [t for t in live_tasks if t["status"] == "blocked"]
    next_actions = [t for t in live_tasks if t["status"] in {"ready", "todo", "running"}]
    return {
        "key": spec.key,
        "name": spec.name,
        "slug": spec.slug,
        "aliases": spec.aliases,
        "accent": spec.accent,
        "objective": spec.objective,
        "owner_hint": spec.owner_hint,
        "generated_at": int(time.time()),
        "generated_at_iso": datetime.now().isoformat(timespec="seconds"),
        "source_notes": source_notes,
        "entities": entities,
        "kanban": {
            "active_count": len(live_tasks),
            "blocked_count": len(blockers),
            "done_sample_count": len(done_tasks),
            "blockers": blockers[:10],
            "next_actions": next_actions[:16],
            "recent_done": done_tasks[:10],
        },
        "artifacts": artifacts,
        "truth_boundary": "Skyvault and Kanban are truth. This artifact is a visual/action surface only; browser-local checks are convenience state.",
    }


def markdown_to_html(md: str, max_lines: int = 18) -> str:
    parts: list[str] = []
    for raw in md.splitlines()[:max_lines]:
        line = clean_markdown_line(raw)
        if not line:
            continue
        if raw.lstrip().startswith(('-', '*')) or re.match(r"\s*- \[[ xX]\]", raw):
            parts.append(f"<li>{html.escape(line)}</li>")
        elif raw.startswith("|"):
            parts.append(f"<li>{html.escape(line)}</li>")
        else:
            parts.append(f"<p>{html.escape(line)}</p>")
    if any(p.startswith("<li>") for p in parts):
        return "<ul>" + "".join(parts) + "</ul>"
    return "".join(parts)


def render_dashboard(data: dict[str, Any], all_projects: list[dict[str, Any]]) -> str:
    css_accent = data["accent"]
    source_cards = []
    for note in data["source_notes"]:
        sections = []
        for name, content in note["sections"].items():
            sections.append(
                f"<details><summary>{html.escape(name)}</summary>{markdown_to_html(content)}</details>"
            )
        source_cards.append(
            f"""
            <article class="card source-card">
              <div class="eyebrow">Skyvault note</div>
              <h3>{html.escape(note['rel_path'])}</h3>
              <p>{html.escape(note.get('summary') or 'No summary line extracted.')}</p>
              <div class="meta-row"><span>Status: {html.escape(str(note.get('status') or '—'))}</span><span>Due: {html.escape(str(note.get('due') or '—'))}</span></div>
              {''.join(sections)}
            </article>
            """
        )
    blockers = render_task_list(data["kanban"]["blockers"], "No blocked items matching this project query.")
    next_actions = render_task_list(data["kanban"]["next_actions"], "No active next actions matching this project query.")
    recent_done = render_task_list(data["kanban"]["recent_done"], "No recent completed tasks in the sample.")
    entities = "".join(
        f"<li><strong>{html.escape(e['name'])}</strong><span>{html.escape(e['rel_path'])}</span></li>" for e in data["entities"]
    ) or "<li><strong>No entities resolved</strong><span>Check project frontmatter/wikilinks.</span></li>"
    artifacts = "".join(
        f"<li><a href='{DEFAULT_PUBLIC_BASE_URL}/{html.escape(a['slug'])}'>{html.escape(a['title'] or a['slug'])}</a><span>{'protected' if a['protected'] else 'public'} · {html.escape(a.get('description') or '')}</span></li>"
        for a in data["artifacts"]
    ) or "<li><strong>No linked artifacts yet</strong><span>This dashboard becomes the first project surface.</span></li>"
    project_nav = "".join(
        f"<a class='{ 'active' if p['key'] == data['key'] else '' }' href='{DEFAULT_PUBLIC_BASE_URL}/{html.escape(p['slug'])}'>{html.escape(p['name'])}</a>"
        for p in all_projects
    )
    data_json = html.escape(json.dumps(data, ensure_ascii=False), quote=False)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(data['name'])} project cockpit</title>
<style>
:root {{ --bg:#08111f; --panel:#0d1b2f; --panel2:#10233d; --text:#edf6ff; --muted:#95a8c3; --line:rgba(255,255,255,.12); --accent:{css_accent}; --bad:#fb7185; --warn:#fbbf24; --ok:#34d399; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: radial-gradient(circle at top left, color-mix(in srgb, var(--accent) 20%, transparent), transparent 34rem), var(--bg); color:var(--text); }}
a {{ color:var(--accent); text-decoration:none; }}
a:hover {{ text-decoration:underline; }}
.shell {{ display:grid; grid-template-columns: 260px 1fr; min-height:100vh; }}
aside {{ border-right:1px solid var(--line); padding:28px 20px; position:sticky; top:0; height:100vh; background:rgba(8,17,31,.82); backdrop-filter: blur(16px); }}
.logo {{ font-weight:800; letter-spacing:-.04em; font-size:1.35rem; }}
.nav {{ display:flex; flex-direction:column; gap:10px; margin-top:28px; }}
.nav a {{ padding:10px 12px; border:1px solid var(--line); border-radius:14px; color:var(--text); }}
.nav a.active {{ border-color: color-mix(in srgb, var(--accent) 70%, white); background: color-mix(in srgb, var(--accent) 18%, transparent); }}
main {{ padding:34px; max-width:1440px; }}
.hero {{ border:1px solid var(--line); background:linear-gradient(135deg, rgba(255,255,255,.08), rgba(255,255,255,.03)); border-radius:28px; padding:28px; box-shadow: 0 24px 80px rgba(0,0,0,.25); }}
.eyebrow {{ color:var(--accent); text-transform:uppercase; letter-spacing:.12em; font-weight:800; font-size:.72rem; }}
h1 {{ margin:.25rem 0 .5rem; font-size: clamp(2rem, 5vw, 4.2rem); letter-spacing:-.075em; line-height:.95; }}
h2 {{ margin:0 0 16px; font-size:1.35rem; letter-spacing:-.03em; }}
h3 {{ margin:.25rem 0 .5rem; font-size:1.05rem; }}
p {{ color:var(--muted); line-height:1.55; }}
.grid {{ display:grid; grid-template-columns: repeat(12, 1fr); gap:18px; margin-top:18px; }}
.card {{ grid-column: span 4; border:1px solid var(--line); border-radius:22px; background:rgba(13,27,47,.76); padding:18px; overflow:hidden; }}
.card.wide {{ grid-column: span 8; }} .card.full {{ grid-column: 1 / -1; }}
.stat {{ font-size:2.2rem; font-weight:850; letter-spacing:-.06em; }}
.meta-row {{ display:flex; flex-wrap:wrap; gap:8px; color:var(--muted); font-size:.82rem; margin:.7rem 0; }}
.meta-row span, .pill {{ border:1px solid var(--line); border-radius:999px; padding:5px 9px; background:rgba(255,255,255,.04); }}
.task-list, .entity-list, .artifact-list {{ list-style:none; padding:0; margin:0; display:flex; flex-direction:column; gap:10px; }}
.task {{ border:1px solid var(--line); border-radius:16px; padding:12px; background:rgba(255,255,255,.035); }}
.task-top {{ display:flex; align-items:flex-start; justify-content:space-between; gap:12px; }}
.task-title {{ font-weight:760; }}
.status {{ font-size:.72rem; border-radius:999px; padding:4px 8px; border:1px solid var(--line); text-transform:uppercase; letter-spacing:.08em; }}
.status.blocked {{ color:var(--bad); border-color:color-mix(in srgb, var(--bad) 55%, transparent); }}
.status.ready, .status.todo, .status.running {{ color:var(--warn); border-color:color-mix(in srgb, var(--warn) 55%, transparent); }}
.status.done {{ color:var(--ok); border-color:color-mix(in srgb, var(--ok) 55%, transparent); }}
.task p {{ margin:.45rem 0 0; font-size:.9rem; }}
.entity-list li, .artifact-list li {{ display:flex; justify-content:space-between; gap:12px; border:1px solid var(--line); border-radius:14px; padding:10px 12px; color:var(--muted); }}
.entity-list strong {{ color:var(--text); }}
details {{ border-top:1px solid var(--line); margin-top:10px; padding-top:10px; }}
summary {{ cursor:pointer; font-weight:760; }}
details ul {{ padding-left:1.1rem; color:var(--muted); }}
.toolbar {{ display:flex; gap:10px; flex-wrap:wrap; margin-top:18px; }}
button, select {{ background:var(--panel2); color:var(--text); border:1px solid var(--line); border-radius:12px; padding:9px 11px; }}
.receipt {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size:.78rem; color:var(--muted); white-space:pre-wrap; }}
.local-check {{ display:flex; gap:8px; align-items:flex-start; margin:8px 0; color:var(--muted); }}
.source-card {{ min-height:260px; }}
@media (max-width: 980px) {{ .shell {{ grid-template-columns:1fr; }} aside {{ position:relative; height:auto; }} .card,.card.wide {{ grid-column:1 / -1; }} main {{ padding:18px; }} }}
</style>
</head>
<body>
<div class="shell">
<aside>
  <div class="logo">project cockpits</div>
  <p>Generated from Skyvault, Hermes Kanban, artifact metadata, and entity links.</p>
  <nav class="nav">{project_nav}<a href="{DEFAULT_PUBLIC_BASE_URL}/{DEFAULT_DEPLOY_SLUG}">Index</a></nav>
  <p class="receipt">Generated: {html.escape(data['generated_at_iso'])}\nBoundary: source truth stays in Skyvault/Kanban.</p>
</aside>
<main>
<section class="hero">
  <div class="eyebrow">visual command surface · not source of truth</div>
  <h1>{html.escape(data['name'])}</h1>
  <p>{html.escape(data['objective'])}</p>
  <div class="meta-row"><span>Owner lane: {html.escape(data['owner_hint'])}</span><span>Slug: {html.escape(data['slug'])}</span><span>{html.escape(data['truth_boundary'])}</span></div>
  <div class="toolbar"><button onclick="window.print()">Print / PDF</button><button onclick="localStorage.clear(); location.reload()">Clear local checkmarks</button><select id="taskFilter"><option value="all">All active cards</option><option value="blocked">Blocked only</option><option value="ready">Ready/todo/running only</option></select></div>
</section>
<section class="grid">
  <article class="card"><div class="eyebrow">active Kanban</div><div class="stat">{data['kanban']['active_count']}</div><p>Matching non-done task cards across aliases.</p></article>
  <article class="card"><div class="eyebrow">blocked</div><div class="stat">{data['kanban']['blocked_count']}</div><p>Things that need a human, credential, vendor, or external answer.</p></article>
  <article class="card"><div class="eyebrow">entities</div><div class="stat">{len(data['entities'])}</div><p>People/org links resolved from frontmatter and wikilinks.</p></article>
  <article class="card wide" id="next-actions"><h2>Next action cards</h2>{next_actions}</article>
  <article class="card" id="blockers"><h2>Blockers</h2>{blockers}</article>
  <article class="card wide"><h2>Source notes</h2><div class="grid">{''.join(source_cards)}</div></article>
  <article class="card"><h2>Entity graph / people links</h2><ul class="entity-list">{entities}</ul></article>
  <article class="card"><h2>Linked artifacts</h2><ul class="artifact-list">{artifacts}</ul></article>
  <article class="card wide"><h2>Recent completed receipts</h2>{recent_done}</article>
  <article class="card"><h2>Local review checklist</h2><label class="local-check"><input type="checkbox" data-local="reviewed-status"> Status read</label><label class="local-check"><input type="checkbox" data-local="picked-next-action"> Next action chosen</label><label class="local-check"><input type="checkbox" data-local="source-opened"> Source note opened</label><p>These checkboxes are browser-local only. If it matters, write it back to Kanban/Skyvault.</p></article>
</section>
<script id="project-data" type="application/json">{data_json}</script>
<script>
for (const box of document.querySelectorAll('[data-local]')) {{
  const key = 'cockpit:' + document.title + ':' + box.dataset.local;
  box.checked = localStorage.getItem(key) === '1';
  box.addEventListener('change', () => localStorage.setItem(key, box.checked ? '1' : '0'));
}}
document.getElementById('taskFilter').addEventListener('change', (event) => {{
  const value = event.target.value;
  for (const task of document.querySelectorAll('.task')) {{
    const status = task.dataset.status;
    task.style.display = value === 'all' || status === value || (value === 'ready' && ['ready','todo','running'].includes(status)) ? '' : 'none';
  }}
}});
</script>
</main>
</div>
</body>
</html>"""


def render_task_list(tasks: list[dict[str, Any]], empty: str) -> str:
    if not tasks:
        return f"<p>{html.escape(empty)}</p>"
    items = []
    for task in tasks:
        status = str(task.get("status", ""))
        assignee = task.get("assignee") or "unassigned"
        body = truncate(clean_markdown_line(task.get("body") or ""), 220)
        items.append(
            f"""
            <div class="task" data-status="{html.escape(status)}">
              <div class="task-top"><div><div class="task-title">{html.escape(task['title'])}</div><div class="meta-row"><span>{html.escape(task['id'])}</span><span>{html.escape(assignee)}</span><span>priority {html.escape(str(task.get('priority') or 0))}</span></div></div><span class="status {html.escape(status)}">{html.escape(status)}</span></div>
              <p>{html.escape(body)}</p>
            </div>
            """
        )
    return "<div class=\"task-list\">" + "".join(items) + "</div>"


def render_index(projects: list[dict[str, Any]]) -> str:
    cards = "".join(
        f"""
        <a class="project-card" href="{DEFAULT_PUBLIC_BASE_URL}/{html.escape(p['slug'])}" style="--accent:{html.escape(p['accent'])}">
          <span>{html.escape(p['name'])}</span>
          <strong>{p['kanban']['active_count']} active · {p['kanban']['blocked_count']} blocked</strong>
          <em>{html.escape(p['objective'])}</em>
        </a>
        """
        for p in projects
    )
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Project dashboards</title><style>
body{{margin:0;min-height:100vh;background:#08111f;color:#edf6ff;font-family:Inter,ui-sans-serif,system-ui;padding:40px}}h1{{font-size:clamp(2.4rem,7vw,6rem);letter-spacing:-.08em;line-height:.9;margin:0 0 1rem}}p{{color:#95a8c3;max-width:760px;line-height:1.55}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:18px;margin-top:28px}}.project-card{{display:flex;flex-direction:column;gap:10px;padding:24px;border:1px solid rgba(255,255,255,.14);border-radius:24px;background:linear-gradient(135deg,color-mix(in srgb,var(--accent) 20%,transparent),rgba(255,255,255,.035));color:#edf6ff;text-decoration:none}}.project-card span{{color:var(--accent);text-transform:uppercase;letter-spacing:.12em;font-size:.76rem;font-weight:800}}.project-card strong{{font-size:1.8rem;letter-spacing:-.05em}}.project-card em{{color:#95a8c3;font-style:normal}}</style></head><body><h1>Project dashboards</h1><p>Generated cockpit surfaces for recurring projects. Skyvault and Kanban stay truth; these pages are the visual/action layer.</p><div class="grid">{cards}</div></body></html>"""


def write_outputs(projects: list[dict[str, Any]], out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for project in projects:
        content = render_dashboard(project, projects)
        path = out_dir / f"{project['slug']}.html"
        path.write_text(content, encoding="utf-8")
        paths.append(path)
        standalone_dir = out_dir / project["slug"]
        standalone_dir.mkdir(parents=True, exist_ok=True)
        standalone_index = standalone_dir / "index.html"
        standalone_index.write_text(content, encoding="utf-8")
        paths.append(standalone_index)
    index = out_dir / "project-dashboard-index.html"
    directory_index = out_dir / "index.html"
    index_content = render_index(projects)
    index.write_text(index_content, encoding="utf-8")
    directory_index.write_text(index_content, encoding="utf-8")
    paths.append(index)
    paths.append(directory_index)
    data_path = out_dir / "project-dashboard-data.json"
    data_path.write_text(json.dumps(projects, indent=2, ensure_ascii=False), encoding="utf-8")
    paths.append(data_path)
    return paths


def deploy_outputs(out_dir: Path, password: str | None, public_base_url: str, artifactd: str, slug: str = DEFAULT_DEPLOY_SLUG) -> list[str]:
    """Deploy the generated dashboard directory as one multi-page artifact.

    The HTML pages intentionally link to sibling `.html` files. Deploying the
    whole output directory keeps those links working under /project-dashboards,
    instead of creating three disconnected single-file artifacts with broken
    relative nav.
    """
    cmd = [
        artifactd,
        "--home",
        "/Users/skylarpayne/.hermes/artifacts",
        "--public-base-url",
        public_base_url,
        "deploy",
        str(out_dir),
        "--slug",
        slug,
        "--title",
        "Project dashboards",
        "--description",
        "Generated HTV and wedding project cockpit surfaces from Skyvault notes, Hermes Kanban tasks, artifact metadata, and entity links.",
        "--pinned",
        "--capability",
        "artifact.describe",
        "--capability",
        "artifact.archive",
        "--capability",
        "kanban.comment",
        "--capability",
        "kanban.create_task",
    ]
    if password:
        cmd.extend(["--password", password])
    try:
        result = subprocess.run(cmd, check=True, text=True, capture_output=True)
    except subprocess.CalledProcessError as exc:
        details = (exc.stderr or exc.stdout or "artifactd deploy failed").strip()
        raise RuntimeError(f"artifactd deploy failed for {slug}: {details}") from None
    return [result.stdout.strip()]


def title_from_slug(slug: str) -> str:
    if slug == "project-dashboard-htv":
        return "Hack the Valley project cockpit"
    if slug == "project-dashboard-wedding":
        return "Our Wedding project cockpit"
    if slug == "project-dashboard-index":
        return "Project dashboard index"
    return slug.replace("-", " ").title()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", choices=["all", *PROJECTS.keys()], default="all")
    parser.add_argument("--vault", type=Path, default=DEFAULT_VAULT)
    parser.add_argument("--kanban-db", type=Path, default=DEFAULT_KANBAN_DB)
    parser.add_argument("--artifact-db", type=Path, default=DEFAULT_ARTIFACT_DB)
    parser.add_argument("--out", type=Path, default=Path("dist"))
    parser.add_argument("--deploy", action="store_true", help="Deploy the generated directory using artifactd CLI.")
    parser.add_argument("--password", default=None, help="Optional per-artifact password for deployment.")
    parser.add_argument("--public-base-url", default=DEFAULT_PUBLIC_BASE_URL)
    parser.add_argument("--deploy-slug", default=DEFAULT_DEPLOY_SLUG)
    parser.add_argument("--artifactd", default="/Users/skylarpayne/artifactd/.venv/bin/artifactd")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    keys = list(PROJECTS) if args.project == "all" else [args.project]
    projects = [build_project_data(PROJECTS[key], args.vault, args.kanban_db, args.artifact_db) for key in keys]
    paths = write_outputs(projects, args.out)
    print("generated:")
    for path in paths:
        print(f"  {path}")
    if args.deploy:
        deploy_log = deploy_outputs(args.out, args.password, args.public_base_url, args.artifactd, args.deploy_slug)
        print("deployed:")
        for entry in deploy_log:
            print(entry)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
