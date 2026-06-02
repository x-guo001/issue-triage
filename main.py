"""Command-line entry point for the GitHub issue triage tool.

Wires the pipeline together: fetch open issues, enrich them with deterministic
triage, run a single LLM analysis pass, then render a rich terminal report.
All known failures are turned into clean stderr messages — no tracebacks reach
the user.
"""

from __future__ import annotations

import argparse
import datetime
import pathlib
import sys

from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from analyzer import AnalysisResult, AnalyzerError, analyze
from github_client import GitHubError, default_max_issues, fetch_issues
from triage import EnrichedIssue, triage

_PYPROJECT = pathlib.Path(__file__).resolve().parent / "pyproject.toml"


def _read_version() -> str:
    """Read the project version from pyproject.toml.

    Uses tomllib on Python 3.11+ and falls back to a minimal line scan on
    3.10, so no third-party TOML parser is required.

    Returns:
        The version string, or "unknown" if it cannot be determined.
    """
    try:
        text = _PYPROJECT.read_text(encoding="utf-8")
    except OSError:
        return "unknown"
    try:
        import tomllib  # Python 3.11+

        return tomllib.loads(text).get("project", {}).get("version", "unknown")
    except ModuleNotFoundError:
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("version") and "=" in stripped:
                return stripped.split("=", 1)[1].strip().strip("\"'")
        return "unknown"


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Parse command-line arguments.

    Args:
        argv: Argument list to parse, or None to use sys.argv.

    Returns:
        The parsed arguments namespace.
    """
    parser = argparse.ArgumentParser(
        prog="issue-triage",
        description="Fetch open GitHub issues and triage them with an LLM.",
    )
    parser.add_argument(
        "--repo",
        required=True,
        metavar="OWNER/NAME",
        help="Public repository to triage, e.g. 'pallets/flask'.",
    )
    default_max = default_max_issues()
    parser.add_argument(
        "--max-issues",
        type=int,
        default=default_max,
        metavar="N",
        help=(
            f"Maximum issues to fetch (default: {default_max}, set via "
            "MAX_ISSUES in .env)."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"issue-triage {_read_version()}",
    )
    return parser.parse_args(argv)


def _render_header(
    console: Console,
    repo: str,
    total_issues: int,
    timestamp: str,
) -> None:
    """Render the report header panel."""
    body = Text()
    body.append("Repository:      ", style="bold")
    body.append(f"{repo}\n")
    body.append("Issues analyzed: ", style="bold")
    body.append(f"{total_issues}\n")
    body.append("Generated:       ", style="bold")
    body.append(timestamp)
    console.print(
        Panel(body, title="GitHub Issue Triage", border_style="cyan")
    )


def _render_fires(console: Console, enriched: list[EnrichedIssue]) -> None:
    """Render the FIRES section from urgency_score (code, not LLM)."""
    console.print()
    console.rule("[bold red]🔥 FIRES")
    fires = sorted(
        (item for item in enriched if item.urgency_score > 0),
        key=lambda item: item.urgency_score,
        reverse=True,
    )
    if not fires:
        console.print("None identified.")
        return
    table = Table(show_header=True, header_style="bold", box=None)
    table.add_column("Score", justify="right", style="red")
    table.add_column("Issue", no_wrap=True)
    table.add_column("Title")
    table.add_column("Signals", style="dim")
    for item in fires:
        signals = []
        if item.is_hot:
            signals.append("hot")
        if item.has_no_response:
            signals.append("no-response")
        if item.is_stale:
            signals.append("stale")
        table.add_row(
            str(item.urgency_score),
            f"#{item.issue.number}",
            item.issue.title,
            ", ".join(signals) or "—",
        )
    console.print(table)


def _render_stale(console: Console, enriched: list[EnrichedIssue]) -> None:
    """Render the STALE section from is_stale (code, not LLM)."""
    console.print()
    console.rule("[bold yellow]🕸  STALE")
    stale = [item for item in enriched if item.is_stale]
    if not stale:
        console.print("None identified.")
        return
    table = Table(show_header=True, header_style="bold", box=None)
    table.add_column("Issue", no_wrap=True)
    table.add_column("Title")
    table.add_column("Idle (days)", justify="right")
    table.add_column("Comments", justify="right")
    for item in stale:
        table.add_row(
            f"#{item.issue.number}",
            item.issue.title,
            str(item.days_since_updated),
            str(item.issue.comments),
        )
    console.print(table)


def _title_for(number: int, titles: dict[int, str]) -> str:
    """Look up an issue title by number, tolerating unknown numbers."""
    return titles.get(number, "")


def _render_clusters(console: Console, analysis: AnalysisResult) -> None:
    """Render the CLUSTERS section from the LLM analysis."""
    console.print()
    console.rule("[bold magenta]🧩 CLUSTERS")
    if not analysis.clusters:
        console.print("None identified.")
        return
    for cluster in analysis.clusters:
        numbers = ", ".join(f"#{n}" for n in cluster.issues)
        console.print(f"• [bold]{cluster.theme}[/]  ({numbers})")


def _render_blockers(
    console: Console,
    analysis: AnalysisResult,
    titles: dict[int, str],
) -> None:
    """Render the RELEASE BLOCKERS section from the LLM analysis."""
    console.print()
    console.rule("[bold red]🚧 RELEASE BLOCKERS")
    if not analysis.release_blockers:
        console.print("None identified.")
        return
    for blocker in analysis.release_blockers:
        title = _title_for(blocker.issue, titles)
        console.print(f"• [bold]#{blocker.issue}[/] {title}".rstrip())
        console.print(f"    [dim]{blocker.reason}[/]")


def _render_quick_wins(
    console: Console,
    analysis: AnalysisResult,
    titles: dict[int, str],
) -> None:
    """Render the QUICK WINS section from the LLM analysis."""
    console.print()
    console.rule("[bold green]✅ QUICK WINS")
    if not analysis.quick_wins:
        console.print("None identified.")
        return
    for win in analysis.quick_wins:
        title = _title_for(win.issue, titles)
        console.print(f"• [bold]#{win.issue}[/] {title}".rstrip())
        console.print(f"    [dim]{win.reason}[/]")


def _render_one_thing(console: Console, analysis: AnalysisResult) -> None:
    """Render the ONE THING section prominently."""
    console.print()
    message = analysis.one_thing.strip() or "None identified."
    console.print(
        Panel(
            Text(message, style="bold"),
            title="⭐ ONE THING",
            border_style="bold green",
            padding=(1, 2),
        )
    )


def _print_report(
    console: Console,
    repo: str,
    enriched: list[EnrichedIssue],
    total_issues: int,
    analysis: AnalysisResult,
) -> None:
    """Render the full report, in order, to the console."""
    titles = {item.issue.number: item.issue.title for item in enriched}
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _render_header(console, repo, total_issues, timestamp)
    _render_fires(console, enriched)
    _render_stale(console, enriched)
    _render_clusters(console, analysis)
    _render_blockers(console, analysis, titles)
    _render_quick_wins(console, analysis, titles)
    _render_one_thing(console, analysis)


def main(argv: list[str] | None = None) -> int:
    """Run the triage pipeline and print the report.

    Args:
        argv: Optional argument list (defaults to sys.argv).

    Returns:
        Process exit code: 0 on success, 1 on a handled error.
    """
    load_dotenv()
    args = _parse_args(argv)

    console = Console()
    err_console = Console(stderr=True)

    try:
        with console.status(
            "[bold cyan]Fetching open issues…", spinner="dots"
        ):
            issues = fetch_issues(args.repo, args.max_issues)
            enriched, stats = triage(issues)

        with console.status(
            "[bold cyan]Analyzing with Claude…", spinner="dots"
        ):
            analysis = analyze(enriched, stats)

        _print_report(
            console, args.repo, enriched, stats.total_issues, analysis
        )
        return 0
    except (GitHubError, AnalyzerError) as exc:
        err_console.print(f"[bold red]Error:[/] {exc}")
        return 1
    except KeyboardInterrupt:
        err_console.print("[yellow]Cancelled.[/]")
        return 130
    except Exception as exc:  # noqa: BLE001 - last resort: never show a traceback
        err_console.print(f"[bold red]Unexpected error:[/] {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
