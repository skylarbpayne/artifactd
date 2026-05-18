from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import typer

from .server import create_app
from .store import ArtifactStore

app = typer.Typer(help="Deploy and serve tiny static HTML artifacts.")

_home_option = typer.Option(Path(os.environ.get("ARTIFACTD_HOME", "~/.hermes/artifacts")).expanduser(), "--home", help="Artifact storage home.")
_port_option = typer.Option(8787, "--port", help="Local server port.")


@app.callback()
def main(ctx: typer.Context, home: Path = _home_option):
    ctx.obj = {"home": home}


@app.command()
def deploy(
    ctx: typer.Context,
    source: Path = typer.Argument(..., exists=True, help="HTML file or directory containing index.html."),
    slug: str = typer.Option(..., "--slug", "-s", help="Public artifact slug."),
    title: Optional[str] = typer.Option(None, "--title", help="Display title."),
    password: Optional[str] = typer.Option(None, "--password", help="Protect artifact with this password."),
    port: int = _port_option,
):
    store = ArtifactStore(ctx.obj["home"])
    artifact = store.deploy(source, slug=slug, title=title, password=password)
    visibility = "protected" if artifact.has_password else "public"
    typer.echo(f"deployed {artifact.slug} ({visibility})")
    typer.echo(f"local_url=http://127.0.0.1:{port}/{artifact.slug}")


@app.command("list")
def list_artifacts(ctx: typer.Context, port: int = _port_option):
    store = ArtifactStore(ctx.obj["home"])
    artifacts = list(store.list())
    if not artifacts:
        typer.echo("no artifacts deployed")
        return
    for artifact in artifacts:
        visibility = "protected" if artifact.has_password else "public"
        typer.echo(f"{artifact.slug}\t{visibility}\thttp://127.0.0.1:{port}/{artifact.slug}")


@app.command()
def protect(ctx: typer.Context, slug: str, password: str = typer.Option(..., "--password", prompt=True, hide_input=True)):
    store = ArtifactStore(ctx.obj["home"])
    artifact = store.protect(slug, password)
    typer.echo(f"protected {artifact.slug}")


@app.command()
def unprotect(ctx: typer.Context, slug: str):
    store = ArtifactStore(ctx.obj["home"])
    artifact = store.unprotect(slug)
    typer.echo(f"unprotected {artifact.slug}")


@app.command()
def delete(ctx: typer.Context, slug: str):
    store = ArtifactStore(ctx.obj["home"])
    store.delete(slug)
    typer.echo(f"deleted {slug}")


@app.command()
def serve(ctx: typer.Context, host: str = typer.Option("127.0.0.1", "--host"), port: int = _port_option):
    import uvicorn

    cookie_secret = os.environ.get("ARTIFACTD_COOKIE_SECRET")
    if not cookie_secret:
        typer.secho("warning: ARTIFACTD_COOKIE_SECRET is unset; using dev-only cookie secret", fg=typer.colors.YELLOW, err=True)
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
