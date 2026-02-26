"""Glock CLI entry point."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Optional

import click

from apps.cli.src import __version__
from apps.cli.src.session.host import SessionHost
from apps.cli.src.tui.app import GlockTUI


@click.group(invoke_without_command=True)
@click.version_option(version=__version__, prog_name="glock")
@click.option(
    "--cwd",
    type=click.Path(exists=True, file_okay=False, dir_okay=True),
    help="Working directory for the session",
)
@click.option(
    "--resume",
    type=str,
    help="Resume a previous session by ID",
)
@click.option(
    "--server",
    type=str,
    envvar="GLOCK_SERVER",
    default="wss://gateway.glock.dev",
    help="Gateway server URL",
)
@click.option(
    "--debug",
    is_flag=True,
    help="Enable debug logging",
)
@click.pass_context
def cli(
    ctx: click.Context,
    cwd: Optional[str],
    resume: Optional[str],
    server: str,
    debug: bool,
) -> None:
    """Glock - AI coding assistant.

    Start an interactive coding session by running 'glock' without arguments.
    """
    # Set up logging
    if debug:
        import logging
        logging.basicConfig(level=logging.DEBUG)

    # Store options in context
    ctx.ensure_object(dict)
    ctx.obj["cwd"] = cwd or os.getcwd()
    ctx.obj["resume"] = resume
    ctx.obj["server"] = server
    ctx.obj["debug"] = debug

    # If no subcommand, start interactive session
    if ctx.invoked_subcommand is None:
        asyncio.run(start_session(ctx.obj))


@cli.command()
@click.pass_context
def sessions(ctx: click.Context) -> None:
    """List previous sessions."""
    asyncio.run(list_sessions(ctx.obj))


@cli.command()
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.pass_context
def status(ctx: click.Context, as_json: bool) -> None:
    """Show current session status."""
    asyncio.run(show_status(ctx.obj, as_json))


@cli.command()
@click.pass_context
def doctor(ctx: click.Context) -> None:
    """Check system requirements and configuration."""
    asyncio.run(run_doctor(ctx.obj))


async def start_session(options: dict) -> None:
    """Start an interactive Glock session."""
    from rich.console import Console
    console = Console()

    try:
        # Create session host
        host = SessionHost(
            server_url=options["server"],
            workspace_dir=options["cwd"],
        )

        # Connect or resume
        if options["resume"]:
            await host.resume(options["resume"])
        else:
            await host.connect()

        # Start TUI
        tui = GlockTUI(host, console)
        await tui.run()

    except KeyboardInterrupt:
        pass
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        if options.get("debug"):
            console.print_exception()
        sys.exit(1)
    finally:
        if "host" in locals():
            await host.disconnect()


async def list_sessions(options: dict) -> None:
    """List previous sessions."""
    from rich.console import Console
    from rich.table import Table

    console = Console()

    # Load session history from local storage
    from apps.cli.src.storage.local import LocalStorage
    storage = LocalStorage()

    sessions = await storage.get_sessions()

    if not sessions:
        console.print("[dim]No previous sessions found.[/dim]")
        return

    table = Table(title="Previous Sessions")
    table.add_column("Session ID", style="cyan")
    table.add_column("Workspace", style="green")
    table.add_column("Last Active", style="yellow")
    table.add_column("Status", style="magenta")

    for session in sessions[:20]:
        table.add_row(
            session.get("session_id", "")[:16] + "...",
            session.get("workspace_label", "")[:30],
            session.get("last_active", ""),
            session.get("status", ""),
        )

    console.print(table)


async def show_status(options: dict, as_json: bool) -> None:
    """Show current status."""
    from rich.console import Console
    import json

    console = Console()

    status_info = {
        "version": __version__,
        "server": options["server"],
        "workspace": options["cwd"],
    }

    if as_json:
        console.print(json.dumps(status_info, indent=2))
    else:
        console.print(f"[bold]Glock[/bold] v{__version__}")
        console.print(f"Server: {options['server']}")
        console.print(f"Workspace: {options['cwd']}")


async def run_doctor(options: dict) -> None:
    """Run system diagnostics."""
    from rich.console import Console

    console = Console()
    console.print("[bold]Glock System Check[/bold]")
    console.print()

    checks = [
        ("Python version", check_python_version),
        ("Network connectivity", lambda: check_network(options["server"])),
        ("Git available", check_git),
        ("Workspace valid", lambda: check_workspace(options["cwd"])),
    ]

    all_passed = True
    for name, check_fn in checks:
        try:
            result = check_fn()
            if asyncio.iscoroutine(result):
                result = await result
            if result:
                console.print(f"  [green]✓[/green] {name}")
            else:
                console.print(f"  [red]✗[/red] {name}")
                all_passed = False
        except Exception as e:
            console.print(f"  [red]✗[/red] {name}: {e}")
            all_passed = False

    console.print()
    if all_passed:
        console.print("[green]All checks passed![/green]")
    else:
        console.print("[yellow]Some checks failed. See above for details.[/yellow]")


def check_python_version() -> bool:
    """Check Python version is 3.11+."""
    return sys.version_info >= (3, 11)


async def check_network(server_url: str) -> bool:
    """Check network connectivity to server."""
    import httpx

    # Convert wss:// to https:// for health check
    http_url = server_url.replace("wss://", "https://").replace("ws://", "http://")
    health_url = f"{http_url}/health"

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(health_url, timeout=5.0)
            return response.status_code == 200
    except Exception:
        return False


def check_git() -> bool:
    """Check if git is available."""
    import shutil
    return shutil.which("git") is not None


def check_workspace(path: str) -> bool:
    """Check if workspace is valid."""
    workspace = Path(path)
    return workspace.exists() and workspace.is_dir()


def main() -> None:
    """CLI entry point."""
    cli(obj={})


if __name__ == "__main__":
    main()
