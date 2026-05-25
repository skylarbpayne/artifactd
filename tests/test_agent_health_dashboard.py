import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from agent_health_dashboard import (
    Check,
    artifactd_status_from_codes,
    codex_auth_summary,
    extract_markdown_items,
    kanban_readiness_summary,
    latest_release_summary,
    openchronicle_index_summary,
    parse_cron_list_output,
    parse_df_output,
    parse_memory_config,
    process_inventory,
    render_html,
    repair_queue,
    skill_usage_summary,
    summarize_recent_log_errors,
)


def test_process_inventory_groups_agent_runtime_processes_without_leaking_full_commands():
    ps_output = """  101 artifactd /Users/skylarpayne/artifactd/.venv/bin/artifactd serve --port 8787 --secret-token abc123
  102 cloudflared /opt/homebrew/bin/cloudflared tunnel run artifacts
  103 python /Users/skylarpayne/.hermes/hermes-agent/venv/bin/hermes gateway start -p palmer
  104 codex /opt/homebrew/bin/codex exec build this
  105 bash /bin/bash -lc sleep 10
"""

    inventory = process_inventory(ps_output)

    assert inventory["counts"] == {
        "artifactd": 1,
        "cloudflared": 1,
        "hermes": 1,
        "codex": 1,
        "other": 1,
    }
    assert inventory["total_tracked"] == 4
    assert inventory["samples"]["artifactd"] == ["101 artifactd serve --port 8787"]
    assert "secret-token" not in str(inventory)


def test_codex_auth_summary_reports_presence_without_token_contents(tmp_path):
    auth_file = tmp_path / ".codex" / "auth.json"
    auth_file.parent.mkdir()
    auth_file.write_text('{"auth_mode":"chatgpt", "last_refresh":"2099-05-24T15:59:37Z", "OPENAI_API_KEY":"sk-secret", "tokens":"do-not-show"}', encoding="utf-8")

    summary = codex_auth_summary(tmp_path)

    assert summary["status"] == "ok"
    assert "auth_mode=chatgpt" in summary["evidence"]
    assert "last_refresh=present" in summary["evidence"]
    assert "secret" not in str(summary)
    assert "do-not-show" not in str(summary)


def test_render_html_includes_ops_console_language():
    html = render_html(
        [
            Check(
                id="runtime-processes",
                agent="Palmer host",
                app="Runtime processes",
                account="local machine",
                operation="ps inventory",
                status="ok",
                summary="Agent runtime processes visible",
                evidence="artifactd=1, hermes=2",
            )
        ],
        "2026-05-21 19:30 PDT",
    )

    assert "ops console" in html.lower()
    assert "morning rounds" in html.lower()
    assert "Runtime processes" in html
    assert "Codex" in html
    assert "OpenChronicle/entity graph" in html
    assert "compaction/memory/Hindsight" in html


def test_render_html_includes_morning_rounds_recommendation_when_supplied():
    html = render_html(
        [
            Check(
                id="kanban",
                agent="Palmer",
                app="Hermes Kanban",
                account="shared board",
                operation="stats",
                status="ok",
                summary="Kanban reachable",
            )
        ],
        "2026-05-24 07:30 PDT",
        {
            "gate_rollups": {"system": "ok", "truth": "warn", "priority": "ok", "execution": "ok"},
            "recommendation": "Do a truth-refresh pass first.",
            "first_90_minutes": "Refresh the active ledger.",
            "items": [
                {"gate": "truth", "status": "warn", "title": "Active work ledger", "summary": "A little stale", "evidence": "ledger.md", "next_action": "Refresh it", "owner": "Palmer"}
            ],
            "kanban": {"executable": [{"id": "t_ready", "title": "Ready task", "status": "ready", "age_hours": 1.0}], "blocked_context": [], "approval_needed": [], "stale": []},
            "priority_now": [],
            "palmer_safe": [],
        },
    )

    assert "Morning recommendation" in html
    assert "Do a truth-refresh pass first." in html
    assert "Executable now" in html
    assert "t_ready" in html


def test_kanban_readiness_summary_detects_executable_context_gaps_and_approvals(tmp_path):
    import sqlite3

    db = tmp_path / "kanban.db"
    now = 1_800_000_000
    with sqlite3.connect(db) as conn:
        conn.execute(
            """
            create table tasks (
                id text primary key,
                title text,
                body text,
                assignee text,
                status text,
                priority integer,
                created_at integer,
                started_at integer,
                last_heartbeat_at integer
            )
            """
        )
        conn.executemany(
            "insert into tasks values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("t_ready", "Build useful thing", "Acceptance criteria and paths are explicit. " * 8, "palmer", "ready", 0, now - 3600, None, None),
                ("t_thin", "Thin task", "tbd", "palmer", "ready", 0, now - 3600, None, None),
                ("t_approval", "Skylar: approve send", "Needs approval before external send. " * 8, "skylar", "blocked", 0, now - 100 * 3600, None, None),
            ],
        )

    summary = kanban_readiness_summary(db, now_ts=now)

    assert summary["counts"] == {"ready": 2, "blocked": 1}
    assert [task["id"] for task in summary["executable"]] == ["t_ready"]
    assert [task["id"] for task in summary["blocked_context"]] == ["t_thin"]
    assert [task["id"] for task in summary["approval_needed"]] == ["t_approval"]
    assert summary["stale"][0]["id"] == "t_approval"


def test_extract_markdown_items_reads_named_section(tmp_path):
    note = tmp_path / "current-priorities.md"
    note.write_text("""# Current Priorities
## Now
- **HTV** — lock print path.
- [[Wonderly]] — proof packet.
## Later
- Not this.
""", encoding="utf-8")

    assert extract_markdown_items(note, "Now") == ["**HTV** — lock print path.", "Wonderly — proof packet."]


def test_artifactd_status_accepts_protected_routes_as_healthy_without_old_smoke_slug():
    assert artifactd_status_from_codes("401", "401", {"401", "200"})["status"] == "ok"
    assert artifactd_status_from_codes("401", "404", {"401", "200"})["status"] == "warn"
    assert artifactd_status_from_codes("000", "000", {"401", "200"})["status"] == "fail"


def test_parse_cron_list_output_counts_active_paused_and_failed_runs():
    text = """
  abc123 [active]
    Name:      Morning brief
    Schedule:  45 7 * * *
    Last run:  2026-05-21T07:51:08-07:00  ok

  def456 [paused]
    Name:      Old watchdog
    Schedule:  every 10m
    Last run:  2026-05-20T07:51:08-07:00  error

  ghi789 [active]
    Name:      Decision batch
    Schedule:  30 16 * * 1-5
    Last run:  2026-05-21T16:31:57-07:00  failed
"""

    summary = parse_cron_list_output(text)

    assert summary["total"] == 3
    assert summary["active"] == 2
    assert summary["paused"] == 1
    assert summary["failed_last_runs"] == 2
    assert summary["failed_jobs"] == ["Old watchdog", "Decision batch"]


def test_summarize_recent_log_errors_redacts_and_limits(tmp_path):
    log = tmp_path / "gateway.error.log"
    log.write_text(
        "INFO fine\nERROR failed with Bearer secret-token-value\nTraceback: boom\nWARN not counted\n",
        encoding="utf-8",
    )

    summary = summarize_recent_log_errors([log], max_lines=10)

    assert summary["error_lines"] == 2
    assert summary["files_checked"] == 1
    assert "Bearer REDACTED" in summary["samples"][0]
    assert "secret-token-value" not in str(summary)


def test_parse_df_output_reports_free_space_percentages():
    parsed = parse_df_output("""Filesystem 1024-blocks Used Available Capacity Mounted on
/dev/disk3s1 100000000 60000000 40000000 60% /System/Volumes/Data
""")

    assert parsed["available_gb"] == 38.1
    assert parsed["free_percent"] == 40.0
    assert parsed["used_percent"] == 60.0


def test_parse_memory_config_extracts_hindsight_and_compaction_settings():
    config = """
memory:
  provider: hindsight
  memory_enabled: true
  user_profile_enabled: true
context:
  engine: compressor
"""

    parsed = parse_memory_config(config)

    assert parsed == {
        "provider": "hindsight",
        "memory_enabled": "true",
        "user_profile_enabled": "true",
        "context_engine": "compressor",
    }


def test_skill_usage_summary_surfaces_top_usage_and_feedback_patches(tmp_path):
    usage = tmp_path / ".usage.json"
    usage.write_text(
        '{"artifact-deployment":{"state":"active","use_count":7,"patch_count":1},"codex":{"state":"active","use_count":3}}',
        encoding="utf-8",
    )

    summary = skill_usage_summary(usage)

    assert summary["status"] == "ok"
    assert "active=2" in summary["evidence"]
    assert "patched_for_feedback=1" in summary["evidence"]
    assert "artifact-deployment(7)" in summary["evidence"]


def test_latest_release_summary_counts_openchronicle_manifest(tmp_path):
    release = tmp_path / "releases" / "2026-05-24T15-59-37Z_abc"
    release.mkdir(parents=True)
    release.joinpath("manifest.json").write_text(
        '{"created_at":"2099-05-24T15:59:37Z","capture_files":12,"memory_files":2,"contains":["capture_json","sqlite_backup"]}',
        encoding="utf-8",
    )

    summary = latest_release_summary(tmp_path)

    assert summary["status"] == "ok"
    assert "captures=12" in summary["evidence"]
    assert "sqlite_backup=True" in summary["evidence"]


def test_openchronicle_index_summary_reports_entity_table_counts(tmp_path):
    import sqlite3

    current = tmp_path / "current"
    current.mkdir()
    db = current / "index.db"
    with sqlite3.connect(db) as conn:
        for table in ["captures", "sessions", "timeline_blocks", "entries", "extractor_records", "entities", "entity_mentions", "entity_edges"]:
            conn.execute(f"create table {table} (id integer primary key)")
            conn.execute(f"insert into {table} default values")

    summary = openchronicle_index_summary(tmp_path)

    assert summary["status"] == "ok"
    assert "captures=1" in summary["evidence"]
    assert "entities=1" in summary["evidence"]


def test_repair_queue_turns_warning_snapshot_into_clearing_actions():
    checks = [
        Check(
            id="echo-canva",
            agent="Echo",
            app="Canva",
            account="Echo profile",
            operation="canva_oauth.py smoke",
            status="warn",
            summary="Canva not wired in Echo profile",
            evidence="Missing CANVA_CLIENT_ID and/or CANVA_CLIENT_SECRET in /Users/skylarpayne/.hermes/profiles/echo/.env",
            remediation="If Echo needs Canva API access, add credentials.",
        ),
        Check(
            id="recent-log-health",
            agent="Palmer",
            app="Recent logs",
            account="palmer profile",
            operation="scan last log lines",
            status="warn",
            summary="Recent logs contain errors",
            evidence="recent_error_lines=177 | port 9712 is in use by a non-hindsight process",
            remediation="Open logs.",
        ),
        Check(
            id="hermes-updates",
            agent="Palmer host",
            app="Hermes updates/releases",
            account="hermes-agent checkout",
            operation="hermes --version + origin compare",
            status="warn",
            summary="Hermes update state needs review",
            evidence="Update available: 170 commits behind; releases_since_installed=0",
            remediation="Review before update.",
        ),
    ]

    queue = repair_queue(checks, {"items": [{"title": "Current priorities note", "status": "warn", "summary": "stale", "next_action": "Refresh it"}]})

    assert [item["id"] for item in queue][:3] == ["recent-log-health", "hermes-updates", "truth-current-priorities-note"]
    assert queue[0]["recommended_action"].startswith("Run a live Hindsight")
    assert "optional" in queue_by_id(queue, "echo-canva")["investigation"].lower()
    assert "Automate" in queue_by_id(queue, "hermes-updates")["feedback_loop"]


def test_render_html_surfaces_issue_clearing_board():
    html = render_html(
        [
            Check(
                id="openchronicle-index-entities",
                agent="Palmer",
                app="OpenChronicle entity extraction",
                account="current/index.db",
                operation="SQLite table counts",
                status="warn",
                summary="OpenChronicle core data present, entity tables missing/unpopulated",
                evidence="captures=1; entities=missing; entity_mentions=missing; entity_edges=missing",
                remediation="Wire entity extractor.",
            )
        ],
        "2026-05-25 08:30 PDT",
        {"gate_rollups": {}, "recommendation": "Repair first.", "first_90_minutes": "Investigate warnings.", "items": [], "kanban": {}, "priority_now": [], "palmer_safe": []},
    )

    assert "Issue clearing board" in html
    assert "Investigated finding" in html
    assert "Automate next" in html
    assert "OpenChronicle index is readable" in html


def queue_by_id(queue, item_id):
    return next(item for item in queue if item["id"] == item_id)
