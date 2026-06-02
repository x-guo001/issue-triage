"""LLM analysis layer for triaged GitHub issues.

Receives issues already enriched by triage.py and asks an LLM only for the
judgments that deterministic code cannot make: clustering related issues,
flagging release blockers, spotting quick wins, and naming the single most
important action. Age, staleness, urgency, the FIRES list, and the STALE list
are all computed in code (triage.py) and are intentionally NOT requested here.

The provider is selected by the LLM_PROVIDER environment variable so a second
backend (e.g. OpenAI) can be added later without touching call sites. Only
"anthropic" is implemented today.

This is a single-call pipeline: one request to the model, no loops or agents.
"""

from __future__ import annotations

import dataclasses
import json
import os

import anthropic

from triage import EnrichedIssue, TriageStats

_DEFAULT_PROVIDER = "anthropic"
_DEFAULT_MODEL = "claude-sonnet-4-6"
_MAX_TOKENS = 1000

_SYSTEM_PROMPT = (
    "You are a senior open-source maintainer triaging issues. The numeric "
    "and boolean fields you are given (ages in days, is_stale, is_hot, "
    "has_no_response, urgency_score) were computed by trusted code and are "
    "ACCURATE. Do not recalculate dates or second-guess them. Focus only on "
    "judgments that require reading and reasoning about the issue text."
)


class AnalyzerError(Exception):
    """Raised when LLM analysis cannot be completed.

    The message is intended to be shown directly to an end user, so it should
    be concise and free of stack-trace detail.
    """


@dataclasses.dataclass(frozen=True)
class Cluster:
    """A group of issues that appear to share one root cause.

    Attributes:
        issues: The issue numbers belonging to the cluster.
        theme: A short description of the shared root problem.
    """

    issues: list[int]
    theme: str


@dataclasses.dataclass(frozen=True)
class Blocker:
    """An issue that sounds like it would block a release.

    Attributes:
        issue: The issue number.
        reason: Why it would block a release.
    """

    issue: int
    reason: str


@dataclasses.dataclass(frozen=True)
class QuickWin:
    """An issue that could likely be closed quickly.

    Attributes:
        issue: The issue number.
        reason: Why it is a quick win (duplicate, needs-info, out of scope).
    """

    issue: int
    reason: str


@dataclasses.dataclass(frozen=True)
class AnalysisResult:
    """The LLM's contribution to triage.

    FIRES and STALE are deliberately absent: those are built from
    urgency_score and is_stale in code, not by the model.

    Attributes:
        clusters: Groups of issues sharing a root cause.
        release_blockers: Issues that would block a release.
        quick_wins: Issues that could be closed quickly.
        one_thing: The single most important action to take today.
    """

    clusters: list[Cluster]
    release_blockers: list[Blocker]
    quick_wins: list[QuickWin]
    one_thing: str


def _format_issue(item: EnrichedIssue) -> str:
    """Render one enriched issue as a compact block for the prompt.

    Args:
        item: The enriched issue to render.

    Returns:
        A multi-line string with the issue's code-computed fields, title, and
        truncated body.
    """
    issue = item.issue
    labels = ", ".join(issue.labels) if issue.labels else "none"
    body = issue.body.strip() or "(no description)"
    return (
        f"#{issue.number} | comments={issue.comments} | "
        f"created={item.days_since_created}d ago | "
        f"updated={item.days_since_updated}d ago | "
        f"is_stale={item.is_stale} is_hot={item.is_hot} "
        f"urgency={item.urgency_score} | labels: {labels}\n"
        f"title: {issue.title}\n"
        f"body: {body}"
    )


def _build_user_prompt(
    enriched: list[EnrichedIssue],
    stats: TriageStats,
) -> str:
    """Assemble the user message: snapshot, instructions, schema, and issues.

    Args:
        enriched: The enriched issues to analyze.
        stats: Aggregate stats used to open the prompt with a snapshot line.

    Returns:
        The full user-message string to send to the model.
    """
    digest = "\n\n".join(_format_issue(item) for item in enriched)
    return (
        f"Repository snapshot: {stats.total_issues} open issues — "
        f"{stats.hot_count} hot, {stats.stale_count} stale, "
        f"{stats.no_response_count} with no response.\n\n"
        "Here are the open issues, each with code-computed fields that are "
        "already accurate:\n\n"
        f"{digest}\n\n"
        "Return ONLY these four judgments as a single JSON object, with no "
        "markdown fences and no commentary:\n"
        "{\n"
        '  "clusters": [{"issues": [<numbers>], "theme": "<short>"}],\n'
        '  "release_blockers": [{"issue": <number>, "reason": "<short>"}],\n'
        '  "quick_wins": [{"issue": <number>, "reason": "<short>"}],\n'
        '  "one_thing": "<single sentence: the most important action today>"\n'
        "}\n\n"
        "Definitions:\n"
        "- clusters: issues that look like the same root problem (by number).\n"
        "- release_blockers: issues that would block a release; explain why.\n"
        "- quick_wins: issues closable quickly (duplicate, needs-info, out of "
        "scope); explain why.\n"
        "- one_thing: the single most important action to take today.\n"
        "Use only issue numbers shown above. Omit a category with an empty "
        "list if nothing fits."
    )


def _client() -> anthropic.Anthropic:
    """Construct an Anthropic client after validating configuration.

    Returns:
        A configured Anthropic client.

    Raises:
        AnalyzerError: If the provider is unsupported or the key is missing.
    """
    provider = os.environ.get("LLM_PROVIDER", _DEFAULT_PROVIDER).lower()
    if provider != "anthropic":
        raise AnalyzerError(
            f"LLM provider '{provider}' is not supported yet. "
            "Set LLM_PROVIDER=anthropic."
        )
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise AnalyzerError(
            "ANTHROPIC_API_KEY is not set. Add it to your .env file."
        )
    return anthropic.Anthropic()


def _parse_response(text: str) -> AnalysisResult:
    """Parse the model's JSON reply into an AnalysisResult.

    Args:
        text: The raw text returned by the model.

    Returns:
        The parsed AnalysisResult.

    Raises:
        AnalyzerError: If the reply is not valid JSON in the expected shape.
    """
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise AnalyzerError("The LLM did not return a JSON object.")
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise AnalyzerError("The LLM returned malformed JSON.") from exc

    try:
        return AnalysisResult(
            clusters=[
                Cluster(
                    issues=list(c.get("issues", [])),
                    theme=c.get("theme", ""),
                )
                for c in data.get("clusters", [])
            ],
            release_blockers=[
                Blocker(issue=b["issue"], reason=b.get("reason", ""))
                for b in data.get("release_blockers", [])
            ],
            quick_wins=[
                QuickWin(issue=q["issue"], reason=q.get("reason", ""))
                for q in data.get("quick_wins", [])
            ],
            one_thing=data.get("one_thing", ""),
        )
    except (KeyError, TypeError) as exc:
        raise AnalyzerError(
            "The LLM returned JSON in an unexpected shape."
        ) from exc


def analyze(
    enriched: list[EnrichedIssue],
    # reserved: aggregate stats available for richer prompt context in a
    # future iteration
    stats: TriageStats,
    *,
    model: str = _DEFAULT_MODEL,
    max_tokens: int = _MAX_TOKENS,
) -> AnalysisResult:
    """Run LLM analysis over already-enriched issues.

    This is a single-call pipeline: exactly one request to the model, with no
    loops, retries, or agentic tool use.

    Args:
        enriched: Issues enriched by triage.py.
        stats: Aggregate stats; used to open the prompt with a snapshot line.
        model: The model identifier to call.
        max_tokens: Hard cap on the model's output tokens.

    Returns:
        An AnalysisResult with clusters, release blockers, quick wins, and the
        single most important action.

    Raises:
        AnalyzerError: On configuration, network, auth, rate-limit, or parsing
            failures — always with a clean, user-facing message.
    """
    if not enriched:
        return AnalysisResult([], [], [], "No open issues to act on.")

    client = _client()
    try:
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=_SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": _build_user_prompt(enriched, stats),
                }
            ],
        )
    except anthropic.AuthenticationError as exc:
        raise AnalyzerError(
            "Anthropic authentication failed. Check ANTHROPIC_API_KEY in "
            "your .env file."
        ) from exc
    except anthropic.RateLimitError as exc:
        raise AnalyzerError(
            "Anthropic rate limit reached. Wait a moment and try again."
        ) from exc
    except anthropic.APIConnectionError as exc:
        raise AnalyzerError(
            "Could not connect to the Anthropic API. Check your network."
        ) from exc
    except anthropic.APIStatusError as exc:
        raise AnalyzerError(
            f"Anthropic API returned an error (HTTP {exc.status_code})."
        ) from exc

    text = "".join(
        block.text for block in response.content if block.type == "text"
    )
    return _parse_response(text)
