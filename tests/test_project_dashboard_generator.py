import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from project_dashboard_generator import (
    PROJECTS,
    ProjectSpec,
    build_project_data,
    deploy_outputs,
    extract_checkboxes,
    extract_sections,
    extract_wikilinks,
    infer_project_summary,
    is_project_health_candidate,
    load_kanban_tasks,
    parse_frontmatter,
    resolve_entity_paths,
    write_outputs,
)


def test_parse_frontmatter_lists_and_body():
    text = """---
status: active
key_people: [Jacqueline, Seth Parker]
related_files:
  - 3_Resources/CRM/People/Jacqueline.md
---
## TL;DR
- One thing
"""
    fm, body = parse_frontmatter(text)
    assert fm["status"] == "active"
    assert fm["key_people"] == ["Jacqueline", "Seth Parker"]
    assert fm["related_files"] == ["3_Resources/CRM/People/Jacqueline.md"]
    assert "TL;DR" in body


def test_extract_sections_and_wikilinks_and_checkboxes():
    md = """Intro
## TL;DR
- [[Jacqueline Aguilar]] owns this
- [ ] choose thing
- [x] done thing
### Nested
More [[Seth Parker|Seth]]
"""
    sections = extract_sections(md)
    assert "TL;DR" in sections
    assert "Nested" in sections
    assert extract_wikilinks(md) == ["Jacqueline Aguilar", "Seth Parker"]
    assert extract_checkboxes(md) == [
        {"done": "False", "text": "choose thing"},
        {"done": "True", "text": "done thing"},
    ]


def test_infer_project_summary_prioritizes_blockers_and_source_summary():
    data = {
        "source_notes": [{"summary": "Venue lock is the live project truth"}],
        "kanban": {
            "active_count": 4,
            "blocked_count": 2,
            "blockers": [{"title": "Approve vendor contract", "assignee": "skylar"}],
            "next_actions": [{"title": "Draft sponsor note", "assignee": "palmer"}],
        },
    }

    summary = infer_project_summary(data)

    assert summary["status"] == "blocked"
    assert summary["headline"] == "Blocked: 2 items need attention"
    assert summary["current_truth"] == "Venue lock is the live project truth"
    assert summary["next_move"] == "Unblock: Approve vendor contract"
    assert summary["owner"] == "skylar"


def test_project_health_candidate_rejects_daily_dashboard_telemetry():
    telemetry = {
        "title": "2026-05-18 daily dashboard progress log",
        "body": "Backing log for the protected daily dashboard action buttons. Current owning task anchors: HTV lanes: t_1 / t_2",
    }
    real_project_task = {"title": "HTV: pick up print order", "body": "Hack the Valley event ops"}

    assert not is_project_health_candidate(telemetry)
    assert is_project_health_candidate(real_project_task)


def test_wedding_kanban_aliases_do_not_match_generic_jacqueline_work(tmp_path):
    assert "jacqueline" not in PROJECTS["wedding"].aliases
    db = tmp_path / "kanban.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE tasks (id TEXT, title TEXT, status TEXT, priority INTEGER, assignee TEXT, created_at INTEGER, completed_at INTEGER, body TEXT)"
    )
    con.execute("INSERT INTO tasks VALUES ('t_1','Set up Jacqueline Outlook','blocked',5,'palmer',1,NULL,'agora comms')")
    con.execute("INSERT INTO tasks VALUES ('t_2','Our Wedding venue contract','blocked',5,'skylar',2,NULL,'wedding blocker')")
    con.commit(); con.close()

    tasks = load_kanban_tasks(db, PROJECTS["wedding"].aliases)

    assert [task["id"] for task in tasks] == ["t_2"]


def test_build_project_data_from_fixture(tmp_path):
    vault = tmp_path / "vault"
    people = vault / "3_Resources" / "CRM" / "People"
    project_dir = vault / "1_Projects"
    people.mkdir(parents=True)
    project_dir.mkdir(parents=True)
    (people / "Jacqueline Aguilar.md").write_text("# Jacqueline\n", encoding="utf-8")
    (project_dir / "Our Wedding.md").write_text(
        """---
status: active
priority: 🔴
key_people: [Jacqueline Aguilar]
---
## TL;DR
- Wedding date locked
## Live work
- [ ] Choose invitations
""",
        encoding="utf-8",
    )
    kanban_db = tmp_path / "kanban.db"
    con = sqlite3.connect(kanban_db)
    con.execute(
        "CREATE TABLE tasks (id TEXT, title TEXT, status TEXT, priority INTEGER, assignee TEXT, created_at INTEGER, completed_at INTEGER, body TEXT)"
    )
    con.execute(
        "INSERT INTO tasks VALUES ('t_1','Wedding next action','ready',5,'skylar',1,NULL,'Jacqueline decision')"
    )
    con.commit(); con.close()
    artifact_db = tmp_path / "artifacts.db"
    con = sqlite3.connect(artifact_db)
    con.execute(
        "CREATE TABLE artifacts (slug TEXT, title TEXT, description TEXT, created_at INTEGER, updated_at INTEGER, password_hash TEXT)"
    )
    con.execute("INSERT INTO artifacts VALUES ('wedding-demo','Wedding Demo','demo',1,2,NULL)")
    con.commit(); con.close()
    spec = ProjectSpec(
        key="wedding", name="Our Wedding", slug="project-dashboard-wedding", aliases=("wedding",),
        note_paths=("1_Projects/Our Wedding.md",), preferred_sections=("TL;DR", "Live work"),
        accent="#fff", owner_hint="owners", objective="objective"
    )
    data = build_project_data(spec, vault, kanban_db, artifact_db)
    assert data["source_notes"][0]["summary"] == "Wedding date locked"
    assert data["summary"]["current_truth"] == "Wedding date locked"
    assert data["summary"]["headline"] == "Active: 1 next actions"
    assert data["kanban"]["active_count"] == 1
    assert data["entities"][0]["rel_path"] == "3_Resources/CRM/People/Jacqueline Aguilar.md"
    assert data["artifacts"][0]["slug"] == "wedding-demo"


def test_resolve_entity_paths_follows_alias_notes_and_dedupes(tmp_path):
    people = tmp_path / "3_Resources" / "CRM" / "People"
    people.mkdir(parents=True)
    (people / "Kimberly Theisen.md").write_text(
        "---\ntype: person\n---\n# Kimberly Theisen\n",
        encoding="utf-8",
    )
    (people / "Kimberly-Thiesen.md").write_text(
        "---\ntype: alias\ncanonical: [[Kimberly Theisen]]\n---\n# Kimberly-Thiesen\n",
        encoding="utf-8",
    )

    entities = resolve_entity_paths(tmp_path, ["Kimberly Thiesen", "Kimberly Theisen"])

    assert entities == [
        {
            "name": "Kimberly Theisen",
            "path": str(people / "Kimberly Theisen.md"),
            "rel_path": "3_Resources/CRM/People/Kimberly Theisen.md",
        }
    ]


def test_write_outputs_creates_directory_deployable_index(tmp_path):
    project = {
        "key": "wedding",
        "name": "Our Wedding",
        "slug": "project-dashboard-wedding",
        "accent": "#fff",
        "objective": "Keep wedding clear",
        "owner_hint": "Skylar + Jacqueline",
        "generated_at_iso": "2026-05-18T12:00:00",
        "truth_boundary": "Truth stays elsewhere.",
        "summary": {"status": "clear", "headline": "Clear", "current_truth": "No summary", "next_move": "Pick next move", "owner": "Skylar + Palmer"},
        "source_notes": [],
        "entities": [],
        "kanban": {"active_count": 0, "blocked_count": 0, "done_sample_count": 0, "blockers": [], "next_actions": [], "recent_done": []},
        "artifacts": [],
    }

    paths = write_outputs([project], tmp_path)

    assert tmp_path / "index.html" in paths
    assert (tmp_path / "index.html").read_text(encoding="utf-8") == (tmp_path / "project-dashboard-index.html").read_text(encoding="utf-8")
    assert "https://artifacts.skylarbpayne.com/project-dashboard-wedding" in (tmp_path / "index.html").read_text(encoding="utf-8")
    assert tmp_path / "project-dashboard-wedding" / "index.html" in paths
    standalone = (tmp_path / "project-dashboard-wedding" / "index.html").read_text(encoding="utf-8")
    assert "https://artifacts.skylarbpayne.com/project-dashboard-wedding" in standalone
    assert "./project-dashboard-wedding.html" not in standalone


def test_deploy_outputs_deploys_single_directory_artifact_without_individual_password(tmp_path):
    out = tmp_path / "dashboards"
    out.mkdir()
    (out / "index.html").write_text("<h1>Index</h1>", encoding="utf-8")
    (out / "project-dashboard-wedding.html").write_text("<h1>Wedding</h1>", encoding="utf-8")
    fake_artifactd = tmp_path / "artifactd"
    log = tmp_path / "args.json"
    fake_artifactd.write_text(
        "#!/usr/bin/env python3\nimport json,sys,pathlib\npathlib.Path(" + repr(str(log)) + ").write_text(json.dumps(sys.argv[1:]))\nprint('deployed project-dashboards (public)')\n",
        encoding="utf-8",
    )
    fake_artifactd.chmod(0o755)

    deployed = deploy_outputs(out, "secret", "https://artifacts.example.com", str(fake_artifactd))
    args = __import__("json").loads(log.read_text(encoding="utf-8"))

    assert deployed == ["deployed project-dashboards (public)"]
    assert str(out) in args
    assert "--slug" in args and args[args.index("--slug") + 1] == "project-dashboards"
    assert "--password" not in args
    assert args.count("--capability") == 4


def test_deploy_outputs_deploys_directory_as_single_artifact(tmp_path):
    out_dir = tmp_path / "dashboards"
    out_dir.mkdir()
    (out_dir / "index.html").write_text("<h1>dashboards</h1>", encoding="utf-8")
    fake_artifactd = tmp_path / "artifactd"
    fake_artifactd.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "print('ARGS=' + ' '.join(sys.argv[1:]))\n",
        encoding="utf-8",
    )
    fake_artifactd.chmod(0o755)

    output = deploy_outputs(out_dir, "secret", "https://artifacts.example.com", str(fake_artifactd), "project-dashboards")

    assert len(output) == 1
    args = output[0]
    assert f"deploy {out_dir}" in args
    assert "--slug project-dashboards" in args
    assert "--password secret" not in args
    assert "project-dashboard-htv" not in args
