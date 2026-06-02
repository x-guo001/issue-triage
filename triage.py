"""Deterministic pre-processing of GitHub issues before LLM analysis.

Everything that can be computed with plain arithmetic and rules lives here, so
the LLM is only asked to do what code cannot. :func:`triage` enriches each
:class:`~github_client.Issue` with derived fields (age, staleness, urgency) and
produces aggregate statistics, all without any network or model calls.
"""

from __future__ import annotations

import collections
import dataclasses
import datetime

from github_client import Issue

# Staleness: untouched for over a month with little discussion.
_STALE_DAYS = 30
_STALE_MAX_COMMENTS = 3

# Hotness: brand new and already actively discussed.
_HOT_DAYS = 7
_HOT_MIN_COMMENTS = 5

# Urgency point values.
_HOT_POINTS = 3
_NO_RESPONSE_POINTS = 1
_STALE_POINTS = 1
_LABEL_POINTS = 2

# Substrings (case-insensitive) that mark a label as urgent.
_URGENT_LABEL_KEYWORDS = ("bug", "regression")

# Number of labels to surface in aggregate stats.
_TOP_LABELS_LIMIT = 5


@dataclasses.dataclass(frozen=True)
class EnrichedIssue:
    """An :class:`~github_client.Issue` plus deterministically derived fields.

    Attributes:
        issue: The original issue this enrichment was computed from.
        days_since_created: Whole days from ``created_at`` to today (>= 0).
        days_since_updated: Whole days from ``updated_at`` to today (>= 0).
        is_stale: True when not updated in over 30 days and comments < 3.
        is_hot: True when created within 7 days and comments >= 5.
        has_no_response: True when the issue has zero comments.
        urgency_score: Weighted score; higher means more pressing.
    """

    issue: Issue
    days_since_created: int
    days_since_updated: int
    is_stale: bool
    is_hot: bool
    has_no_response: bool
    urgency_score: int


@dataclasses.dataclass(frozen=True)
class TriageStats:
    """Aggregate statistics computed across all enriched issues.

    Attributes:
        total_issues: Number of issues triaged.
        hot_count: How many issues are hot.
        stale_count: How many issues are stale.
        no_response_count: How many issues have zero comments.
        top_labels: Up to five ``(label, count)`` pairs, most common first.
    """

    total_issues: int
    hot_count: int
    stale_count: int
    no_response_count: int
    top_labels: list[tuple[str, int]]


def _today() -> datetime.date:
    """Return today's date in UTC.

    Returns:
        The current UTC calendar date, matching the timezone GitHub uses for
        its ``created_at`` and ``updated_at`` timestamps.
    """
    return datetime.datetime.now(datetime.timezone.utc).date()


def _parse_date(timestamp: str) -> datetime.date:
    """Parse a GitHub ISO-8601 timestamp into a UTC date.

    Args:
        timestamp: An ISO-8601 string such as ``"2024-05-20T12:34:56Z"``.

    Returns:
        The corresponding UTC calendar date.

    Raises:
        ValueError: If the timestamp cannot be parsed.
    """
    text = timestamp.strip()
    # datetime.fromisoformat does not accept a trailing 'Z' before Python 3.11.
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.astimezone(datetime.timezone.utc).date()


def _days_since(timestamp: str, today: datetime.date) -> int:
    """Compute whole days from a timestamp to ``today``, clamped at zero.

    Args:
        timestamp: An ISO-8601 timestamp.
        today: The reference date to measure against.

    Returns:
        The non-negative number of days elapsed. Future timestamps (e.g. from
        clock skew) yield 0 rather than a negative value.
    """
    return max(0, (today - _parse_date(timestamp)).days)


def _label_points(labels: tuple[str, ...]) -> int:
    """Score labels that signal a bug or regression.

    Args:
        labels: The issue's label names.

    Returns:
        Two points for each label whose name contains "bug" or "regression"
        (case-insensitive).
    """
    points = 0
    for label in labels:
        lowered = label.lower()
        if any(keyword in lowered for keyword in _URGENT_LABEL_KEYWORDS):
            points += _LABEL_POINTS
    return points


def enrich_issue(issue: Issue, today: datetime.date) -> EnrichedIssue:
    """Compute derived fields for a single issue.

    Args:
        issue: The issue to enrich.
        today: The reference date for age calculations.

    Returns:
        An :class:`EnrichedIssue` with all derived fields populated.
    """
    days_since_created = _days_since(issue.created_at, today)
    days_since_updated = _days_since(issue.updated_at, today)

    is_stale = (
        days_since_updated > _STALE_DAYS
        and issue.comments < _STALE_MAX_COMMENTS
    )
    is_hot = (
        days_since_created < _HOT_DAYS
        and issue.comments >= _HOT_MIN_COMMENTS
    )
    has_no_response = issue.comments == 0

    urgency_score = _label_points(issue.labels)
    if is_hot:
        urgency_score += _HOT_POINTS
    if has_no_response:
        urgency_score += _NO_RESPONSE_POINTS
    if is_stale:
        urgency_score += _STALE_POINTS

    return EnrichedIssue(
        issue=issue,
        days_since_created=days_since_created,
        days_since_updated=days_since_updated,
        is_stale=is_stale,
        is_hot=is_hot,
        has_no_response=has_no_response,
        urgency_score=urgency_score,
    )


def compute_stats(enriched: list[EnrichedIssue]) -> TriageStats:
    """Aggregate enriched issues into summary statistics.

    Args:
        enriched: The enriched issues to summarize.

    Returns:
        A :class:`TriageStats` describing the whole set.
    """
    label_counts: collections.Counter[str] = collections.Counter()
    for item in enriched:
        label_counts.update(item.issue.labels)

    return TriageStats(
        total_issues=len(enriched),
        hot_count=sum(1 for item in enriched if item.is_hot),
        stale_count=sum(1 for item in enriched if item.is_stale),
        no_response_count=sum(1 for item in enriched if item.has_no_response),
        top_labels=label_counts.most_common(_TOP_LABELS_LIMIT),
    )


def triage(
    issues: list[Issue],
    today: datetime.date | None = None,
) -> tuple[list[EnrichedIssue], TriageStats]:
    """Enrich issues and compute aggregate statistics.

    This is the single entry point the rest of the application should use. It
    performs no network or LLM calls.

    Args:
        issues: The raw issues fetched from GitHub.
        today: Reference date for age calculations. Defaults to the current UTC
            date; accepted as a parameter to keep the logic testable.

    Returns:
        A ``(enriched_issues, stats)`` tuple. ``enriched_issues`` preserves the
        input order.
    """
    reference = today or _today()
    enriched = [enrich_issue(issue, reference) for issue in issues]
    return enriched, compute_stats(enriched)
