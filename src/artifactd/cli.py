from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional, List

import typer

from .server import create_app, _workspace_home_payload
from .store import ArtifactStore
from .workspaces import resolve_workspace_home
from .hermes_plugin_installer import install_hermes_plugin
from .actions import KanbanExecutor

app = typer.Typer(help="Deploy and serve tiny static HTML artifacts.")
workspaces_app = typer.Typer(help="Install, inspect, and smoke-test Hermes Workspaces for a profile.")
app.add_typer(workspaces_app, name="workspaces")

_home_option = typer.Option(Path(os.environ.get("ARTIFACTD_HOME", "~/.hermes/artifacts")).expanduser(), "--home", help="Artifact storage home.")
_public_base_option = typer.Option(os.environ.get("ARTIFACTD_PUBLIC_BASE_URL"), "--public-base-url", help="Public HTTPS base URL, e.g. https://artifacts.example.com")
_port_option = typer.Option(8787, "--port", help="Local server port.")
_status_option = typer.Option("active", "--status", help="Artifact status filter: active, archived, or all.")


@app.callback()
def main(ctx: typer.Context, home: Path = _home_option, public_base_url: Optional[str] = _public_base_option):
    ctx.obj = {"home": home, "public_base_url": _normalize_base_url(public_base_url)}


@workspaces_app.command("install")
def workspaces_install(
    profile: str = typer.Option(..., "--profile", help="Hermes profile name."),
    hermes_root: Optional[Path] = typer.Option(None, "--hermes-root", help="Root Hermes home containing profiles/<name>."),
    profile_home: Optional[Path] = typer.Option(None, "--profile-home", help="Explicit profile-scoped HERMES_HOME."),
    password: Optional[str] = typer.Option(None, "--password", help="Set the default workspace password for this profile."),
):
    workspace_home = resolve_workspace_home(profile, hermes_root=hermes_root, profile_home=profile_home)
    store = ArtifactStore(workspace_home)
    if password:
        store.set_workspace_password(password)
    typer.echo(f"profile={profile}")
    typer.echo(f"workspace_home={workspace_home}")
    typer.echo("installed=true")
    typer.echo(f"workspace_password_configured={str(store.workspace_password_configured()).lower()}")


@workspaces_app.command("status")
def workspaces_status(
    profile: str = typer.Option(..., "--profile", help="Hermes profile name."),
    hermes_root: Optional[Path] = typer.Option(None, "--hermes-root", help="Root Hermes home containing profiles/<name>."),
    profile_home: Optional[Path] = typer.Option(None, "--profile-home", help="Explicit profile-scoped HERMES_HOME."),
):
    workspace_home = resolve_workspace_home(profile, hermes_root=hermes_root, profile_home=profile_home)
    store = ArtifactStore(workspace_home)
    typer.echo(f"profile={profile}")
    typer.echo(f"workspace_home={workspace_home}")
    typer.echo(f"workspace_password_configured={str(store.workspace_password_configured()).lower()}")
    typer.echo(f"active_things={len(list(store.list(status='active')))}")
    typer.echo(f"pinned_things={len(store.list_workspace_things(bucket='pinned'))}")
    typer.echo(f"requires_action_things={len(store.list_workspace_things(bucket='requires-action'))}")


@workspaces_app.command("home")
def workspaces_home(
    profile: str = typer.Option(..., "--profile", help="Hermes profile name."),
    hermes_root: Optional[Path] = typer.Option(None, "--hermes-root", help="Root Hermes home containing profiles/<name>."),
    profile_home: Optional[Path] = typer.Option(None, "--profile-home", help="Explicit profile-scoped HERMES_HOME."),
):
    workspace_home = resolve_workspace_home(profile, hermes_root=hermes_root, profile_home=profile_home)
    payload = _workspace_home_payload(
        ArtifactStore(workspace_home),
        profile=profile,
        executor=KanbanExecutor(profile=profile),
        csrf_token="",
    )
    typer.echo(json.dumps(payload, sort_keys=True))


@workspaces_app.command("start")
def workspaces_start(
    profile: str = typer.Option(..., "--profile", help="Hermes profile name."),
    hermes_root: Optional[Path] = typer.Option(None, "--hermes-root", help="Root Hermes home containing profiles/<name>."),
    profile_home: Optional[Path] = typer.Option(None, "--profile-home", help="Explicit profile-scoped HERMES_HOME."),
    port: int = _port_option,
):
    workspace_home = resolve_workspace_home(profile, hermes_root=hermes_root, profile_home=profile_home)
    typer.echo(f"profile={profile}")
    typer.echo(f"workspace_home={workspace_home}")
    typer.echo(f"serve_command=ARTIFACTD_PROFILE={profile} artifactd --home {workspace_home} serve --profile {profile} --port {port}")


@workspaces_app.command("smoke")
def workspaces_smoke(
    profile: str = typer.Option(..., "--profile", help="Hermes profile name."),
    hermes_root: Optional[Path] = typer.Option(None, "--hermes-root", help="Root Hermes home containing profiles/<name>."),
    profile_home: Optional[Path] = typer.Option(None, "--profile-home", help="Explicit profile-scoped HERMES_HOME."),
    password: str = typer.Option("workspace-smoke-password", "--password", help="Default workspace password to configure for the smoke."),
):
    workspace_home = resolve_workspace_home(profile, hermes_root=hermes_root, profile_home=profile_home)
    store = ArtifactStore(workspace_home)
    store.set_workspace_password(password)
    smoke_dir = workspace_home / ".smoke-source"
    smoke_dir.mkdir(parents=True, exist_ok=True)
    smoke_html = smoke_dir / "index.html"
    smoke_html.write_text(
        "<!doctype html><html><body><h1>Hermes Workspaces smoke</h1><p>Profile-owned generated Thing.</p></body></html>",
        encoding="utf-8",
    )
    thing = store.register_thing(
        smoke_dir,
        slug="hermes-workspaces-smoke",
        title="Hermes Workspaces smoke",
        description="Protected smoke Thing for profile-scoped Hermes Workspaces.",
        capabilities=["artifact.describe"],
    )
    typer.echo(f"profile={profile}")
    typer.echo(f"workspace_home={workspace_home}")
    typer.echo(f"created {thing.slug}")
    typer.echo(f"auth_mode={thing.auth_mode}")
    typer.echo("status=ok")


@workspaces_app.command("import-legacy")
def workspaces_import_legacy(
    profile: str = typer.Option(..., "--profile", help="Hermes profile name."),
    hermes_root: Optional[Path] = typer.Option(None, "--hermes-root", help="Root Hermes home containing profiles/<name>."),
    profile_home: Optional[Path] = typer.Option(None, "--profile-home", help="Explicit profile-scoped HERMES_HOME."),
    from_home: Path = typer.Option(..., "--from-home", exists=True, file_okay=False, help="Existing artifactd home to copy into this profile's Workspaces registry."),
):
    workspace_home = resolve_workspace_home(profile, hermes_root=hermes_root, profile_home=profile_home)
    workspace_store = ArtifactStore(workspace_home)
    legacy_store = ArtifactStore(from_home)
    report = workspace_store.import_legacy_artifacts(legacy_store)
    typer.echo(f"profile={profile}")
    typer.echo(f"workspace_home={workspace_home}")
    typer.echo(f"from_home={from_home}")
    typer.echo(f"imported={report['imported']}")
    typer.echo(f"updated={report['updated']}")
    typer.echo(f"skipped={report['skipped']}")


@workspaces_app.command("register")
def workspaces_register(
    source: Path = typer.Argument(..., exists=True, help="HTML file or directory containing index.html."),
    profile: str = typer.Option(..., "--profile", help="Hermes profile name."),
    hermes_root: Optional[Path] = typer.Option(None, "--hermes-root", help="Root Hermes home containing profiles/<name>."),
    profile_home: Optional[Path] = typer.Option(None, "--profile-home", help="Explicit profile-scoped HERMES_HOME."),
    slug: str = typer.Option(..., "--slug", help="Workspace Thing slug."),
    title: Optional[str] = typer.Option(None, "--title", help="Display title."),
    description: Optional[str] = typer.Option(None, "--description", help="Short searchable description."),
    capability: Optional[List[str]] = typer.Option(None, "--capability", help="Allowed named server-side capability. Repeat for multiple."),
    tag: Optional[List[str]] = typer.Option(None, "--tag", help="Organization tag. Repeat for multiple."),
    requires_action: bool = typer.Option(False, "--requires-action", help="Surface this Thing in the requires-action bucket."),
    pinned: bool = typer.Option(False, "--pinned", help="Pin this Thing in Home."),
    public: bool = typer.Option(False, "--public", help="Make public instead of profile-auth protected."),
):
    workspace_home = resolve_workspace_home(profile, hermes_root=hermes_root, profile_home=profile_home)
    thing = ArtifactStore(workspace_home).register_thing(
        source,
        slug=slug,
        title=title,
        description=description,
        capabilities=capability,
        tags=tag,
        requires_action=requires_action,
        pinned=pinned,
        public=public,
    )
    typer.echo(f"profile={profile}")
    typer.echo(f"workspace_home={workspace_home}")
    typer.echo(f"registered {thing.slug}")
    typer.echo(f"auth_mode={thing.auth_mode}")


@workspaces_app.command("install-plugin")
def workspaces_install_plugin(
    profile: str = typer.Option(..., "--profile", help="Hermes profile name."),
    hermes_root: Optional[Path] = typer.Option(None, "--hermes-root", help="Root Hermes home containing profiles/<name>."),
    profile_home: Optional[Path] = typer.Option(None, "--profile-home", help="Explicit profile-scoped HERMES_HOME."),
    runtime_path: Optional[Path] = typer.Option(None, "--runtime-path", help="artifactd executable the Hermes plugin should call."),
    port: int = _port_option,
    public_base_url: Optional[str] = _public_base_option,
    enable: bool = typer.Option(False, "--enable", help="Add artifactd_workspaces to plugins.enabled in profile config.yaml."),
    force: bool = typer.Option(False, "--force", help="Replace an existing generated artifactd_workspaces plugin directory."),
):
    plugin_dir = install_hermes_plugin(
        profile=profile,
        hermes_root=hermes_root,
        profile_home=profile_home,
        runtime_path=runtime_path,
        port=port,
        public_base_url=_normalize_base_url(public_base_url),
        enable=enable,
        force=force,
    )
    typer.echo(f"profile={profile}")
    typer.echo(f"plugin_dir={plugin_dir}")
    typer.echo(f"installed_plugin=true")
    typer.echo(f"enabled={str(enable).lower()}")


@app.command()
def deploy(
    ctx: typer.Context,
    source: Path = typer.Argument(..., exists=True, help="HTML file or directory containing index.html."),
    slug: str = typer.Option(..., "--slug", "-s", help="Public artifact slug."),
    title: Optional[str] = typer.Option(None, "--title", help="Display title."),
    description: Optional[str] = typer.Option(None, "--description", "-d", help="Short description used on the artifacts home page and search."),
    capability: Optional[List[str]] = typer.Option(None, "--capability", help="Allow a named server-side action capability. Repeat for multiple."),
    tag: Optional[List[str]] = typer.Option(None, "--tag", help="Organization tag. Repeat for multiple."),
    pinned: bool = typer.Option(False, "--pinned", help="Pin artifact so archive/prune will not move it."),
    expires_at: Optional[int] = typer.Option(None, "--expires-at", help="Unix timestamp when prune should archive/delete this artifact."),
    port: int = _port_option,
):
    store = ArtifactStore(ctx.obj["home"])
    artifact = store.deploy(source, slug=slug, title=title, description=description, capabilities=capability, tags=tag, pinned=pinned, expires_at=expires_at)
    visibility = _visibility(artifact)
    typer.echo(f"deployed {artifact.slug} ({visibility})")
    typer.echo(f"local_url={_local_url(artifact.slug, port)}")
    if ctx.obj.get("public_base_url"):
        typer.echo(f"public_url={_public_url(ctx.obj['public_base_url'], artifact.slug)}")


@app.command("list")
def list_artifacts(
    ctx: typer.Context,
    port: int = _port_option,
    query: str = typer.Option("", "--query", "-q", help="Filter by title, slug, or description."),
    status: str = _status_option,
):
    store = ArtifactStore(ctx.obj["home"])
    artifacts = list(store.search(query, status=status) if query else store.list(status=status))
    if not artifacts:
        typer.echo("no artifacts deployed")
        return
    for artifact in artifacts:
        urls = [_local_url(artifact.slug, port)]
        if ctx.obj.get("public_base_url"):
            urls.append(_public_url(ctx.obj["public_base_url"], artifact.slug))
        fields = [artifact.slug, artifact.status, _visibility(artifact)]
        if artifact.pinned:
            fields.append("pinned")
        if artifact.expires_at is not None:
            fields.append(f"expires_at={artifact.expires_at}")
        if artifact.capabilities:
            fields.append("actions=" + ",".join(artifact.capabilities))
        if artifact.tags:
            fields.append("tags=" + ",".join(artifact.tags))
        if artifact.title:
            fields.append(artifact.title)
        if artifact.description:
            fields.append(artifact.description)
        fields.extend(urls)
        typer.echo("\t".join(fields))


@app.command()
def unprotect(ctx: typer.Context, slug: str):
    store = ArtifactStore(ctx.obj["home"])
    artifact = store.unprotect(slug)
    typer.echo(f"unprotected {artifact.slug}")


@app.command()
def describe(
    ctx: typer.Context,
    slug: str,
    title: Optional[str] = typer.Option(None, "--title", help="Updated display title."),
    description: Optional[str] = typer.Option(None, "--description", "-d", help="Updated searchable description."),
    tag: Optional[List[str]] = typer.Option(None, "--tag", help="Replace organization tags. Repeat for multiple."),
    pinned: Optional[bool] = typer.Option(None, "--pinned/--unpinned", help="Pin or unpin artifact."),
    expires_at: Optional[int] = typer.Option(None, "--expires-at", help="Updated Unix expiration timestamp."),
    clear_expires_at: bool = typer.Option(False, "--clear-expires-at", help="Remove expiration timestamp."),
):
    if title is None and description is None and tag is None and pinned is None and expires_at is None and not clear_expires_at:
        raise typer.BadParameter("provide --title, --description, --tag, --pinned/--unpinned, --expires-at, or --clear-expires-at")
    store = ArtifactStore(ctx.obj["home"])
    artifact = store.update_metadata(slug, title=title, description=description, tags=tag, pinned=pinned, expires_at=expires_at, clear_expires_at=clear_expires_at)
    typer.echo(f"updated {artifact.slug}")


@app.command()
def archive(ctx: typer.Context, slug: str):
    store = ArtifactStore(ctx.obj["home"])
    before = store.get(slug)
    artifact = store.archive(slug)
    if before and before.pinned:
        typer.echo(f"skipped {artifact.slug} (pinned)")
    else:
        typer.echo(f"archived {artifact.slug}")


@app.command()
def restore(ctx: typer.Context, slug: str):
    store = ArtifactStore(ctx.obj["home"])
    artifact = store.restore(slug)
    typer.echo(f"restored {artifact.slug}")


@app.command()
def prune(
    ctx: typer.Context,
    dry_run: bool = typer.Option(True, "--dry-run/--apply", help="Preview prune actions unless --apply is used."),
    now: Optional[int] = typer.Option(None, "--now", help="Unix timestamp override for tests/manual checks."),
):
    store = ArtifactStore(ctx.obj["home"])
    report = store.prune(now=now or int(time.time()), dry_run=dry_run)
    if not report:
        typer.echo("nothing to prune")
        return
    prefix = "would " if dry_run else ""
    for item in report:
        typer.echo(f"{prefix}{item['action']} {item['slug']}: {item['reason']}")


@app.command()
def delete(ctx: typer.Context, slug: str):
    store = ArtifactStore(ctx.obj["home"])
    store.delete(slug)
    typer.echo(f"deleted {slug}")


@app.command()
def serve(
    ctx: typer.Context,
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = _port_option,
    profile: Optional[str] = typer.Option(None, "--profile", help="Owning Hermes profile for workspace capability actions."),
):
    import uvicorn

    cookie_secret = os.environ.get("ARTIFACTD_COOKIE_SECRET")
    if not cookie_secret:
        typer.secho("warning: ARTIFACTD_COOKIE_SECRET is unset; using dev-only cookie secret", fg=typer.colors.YELLOW, err=True)
    if profile:
        os.environ["ARTIFACTD_PROFILE"] = profile
    uvicorn.run(create_app(ctx.obj["home"], cookie_secret=cookie_secret), host=host, port=port)


@app.command("tunnel-runbook")
def tunnel_runbook(port: int = _port_option, hostname: Optional[str] = typer.Option(None, "--hostname")):
    typer.echo("Dev tunnel:")
    typer.echo(f"  cloudflared tunnel --url http://localhost:{port}")
    if hostname:
        typer.echo("\nNamed tunnel:")
        typer.echo("  cloudflared tunnel create artifacts")
        typer.echo(f"  cloudflared tunnel route dns artifacts {hostname}")
        typer.echo("  cloudflared tunnel run artifacts")


def _normalize_base_url(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    return value.rstrip("/")


def _local_url(slug: str, port: int) -> str:
    return f"http://127.0.0.1:{port}/{slug}"


def _public_url(base_url: str, slug: str) -> str:
    return f"{base_url}/{slug}"


def _visibility(artifact) -> str:
    if getattr(artifact, "uses_profile_auth", False):
        return "protected(profile)"
    return "protected(legacy)" if artifact.has_password else "public"
