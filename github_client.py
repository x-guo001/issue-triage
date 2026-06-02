"""GitHub REST API client for fetching open issues.

This module exposes :func:`fetch_issues`, which retrieves open issues for a
public repository, transparently handling pagination, filtering out pull
requests, and truncating long issue bodies. All network and API failures are
surfaced as :class:`GitHubError` with human-readable messages so that callers
can present clean errors instead of raw tracebacks.
"""

from __future__ import annotations

import dataclasses
import os
from typing import Optional

import requests

API_ROOT = "https://api.github.com"

# GitHub caps page size at 100 items per request.
_PER_PAGE = 100

# Default and hard ceilings on how many issues we will pull.
DEFAULT_MAX_ISSUES = 50

# Issue bodies are truncated to this many characters to bound LLM input.
_BODY_TRUNCATE_LENGTH = 500

# Per-request network timeout, in seconds (connect, read).
_REQUEST_TIMEOUT = 15


def default_max_issues() -> int:
    """Return the default issue cap from the environment, or 50.

    Reads the ``MAX_ISSUES`` environment variable (populated from ``.env``).
    A missing, non-integer, or non-positive value falls back to
    :data:`DEFAULT_MAX_ISSUES`.

    Returns:
        The resolved default maximum number of issues to fetch.
    """
    raw = os.environ.get("MAX_ISSUES")
    if raw is None:
        return DEFAULT_MAX_ISSUES
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_ISSUES
    return value if value > 0 else DEFAULT_MAX_ISSUES


class GitHubError(Exception):
    """Raised when the GitHub API cannot be reached or returns an error.

    The message is intended to be shown directly to an end user, so it should
    be concise and free of stack-trace detail.
    """


@dataclasses.dataclass(frozen=True)
class Issue:
    """A single open GitHub issue, normalized for downstream triage.

    Attributes:
        number: The issue number within the repository.
        title: The issue title.
        body: The issue body, truncated to the first 500 characters.
        url: The HTML URL of the issue on github.com.
        labels: The names of labels attached to the issue.
        comments: The number of comments on the issue.
        created_at: ISO-8601 creation timestamp.
        updated_at: ISO-8601 last-update timestamp.
    """

    number: int
    title: str
    body: str
    url: str
    labels: tuple[str, ...]
    comments: int
    created_at: str
    updated_at: str


def _validate_repo(repo: str) -> tuple[str, str]:
    """Validate and split an ``owner/name`` repository identifier.

    Args:
        repo: Repository identifier in ``owner/name`` form.

    Returns:
        A ``(owner, name)`` tuple.

    Raises:
        GitHubError: If ``repo`` is not in ``owner/name`` form.
    """
    parts = repo.strip().split("/")
    if len(parts) != 2 or not all(parts):
        raise GitHubError(
            f"Invalid repository '{repo}'. Expected 'owner/name' "
            "(for example, 'pallets/flask')."
        )
    return parts[0], parts[1]


def _auth_headers() -> dict[str, str]:
    """Build request headers, including a bearer token when available.

    Returns:
        A mapping of HTTP headers. A ``GITHUB_TOKEN`` environment variable, if
        set, is sent as a bearer token to raise the API rate limit.
    """
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _truncate_body(body: Optional[str]) -> str:
    """Truncate an issue body to the first 500 characters.

    Args:
        body: The raw issue body, which may be ``None``.

    Returns:
        The body truncated to 500 characters, with an ellipsis appended when
        truncation occurred. ``None`` bodies become an empty string.
    """
    if not body:
        return ""
    if len(body) <= _BODY_TRUNCATE_LENGTH:
        return body
    return body[:_BODY_TRUNCATE_LENGTH] + "…"


def _raise_for_status(response: requests.Response, repo: str) -> None:
    """Translate an unsuccessful HTTP response into a :class:`GitHubError`.

    Args:
        response: The HTTP response to inspect.
        repo: The repository identifier, used for clearer messages.

    Raises:
        GitHubError: If the response status indicates failure. The message
            distinguishes rate limiting (403), missing repositories (404), and
            other errors.
    """
    if response.ok:
        return

    if response.status_code == 404:
        raise GitHubError(
            f"Repository '{repo}' was not found. Check the owner/name and "
            "that the repository is public."
        )

    if response.status_code == 403:
        remaining = response.headers.get("X-RateLimit-Remaining")
        if remaining == "0":
            raise GitHubError(
                "GitHub API rate limit exceeded. Set GITHUB_TOKEN in your "
                ".env to raise the limit, or wait and try again."
            )
        raise GitHubError(
            "Access to the GitHub API was forbidden (403). Verify your "
            "GITHUB_TOKEN has permission to read this repository."
        )

    raise GitHubError(
        f"GitHub API request failed with status {response.status_code}: "
        f"{response.reason}."
    )


def fetch_issues(
    repo: str,
    max_issues: int = DEFAULT_MAX_ISSUES,
) -> list[Issue]:
    """Fetch up to ``max_issues`` open issues for a public repository.

    Pull requests are excluded (the issues endpoint returns both), and each
    issue body is truncated to its first 500 characters. Results are pulled
    page by page until ``max_issues`` is reached or the repository has no more
    open issues.

    Args:
        repo: Repository identifier in ``owner/name`` form, for example
            ``"pallets/flask"``.
        max_issues: Maximum number of issues to return. Values below 1 yield an
            empty list.

    Returns:
        A list of :class:`Issue` objects, at most ``max_issues`` long, ordered
        as returned by the GitHub API (most recently created first).

    Raises:
        GitHubError: If the repository identifier is malformed, the network is
            unreachable or times out, or the API returns an error status.
    """
    owner, name = _validate_repo(repo)
    if max_issues < 1:
        return []

    headers = _auth_headers()
    issues: list[Issue] = []
    page = 1

    while len(issues) < max_issues:
        params = {
            "state": "open",
            "per_page": _PER_PAGE,
            "page": page,
        }
        try:
            response = requests.get(
                f"{API_ROOT}/repos/{owner}/{name}/issues",
                headers=headers,
                params=params,
                timeout=_REQUEST_TIMEOUT,
            )
        except requests.exceptions.Timeout as exc:
            raise GitHubError(
                "The request to GitHub timed out. Check your connection and "
                "try again."
            ) from exc
        except requests.exceptions.ConnectionError as exc:
            raise GitHubError(
                "Could not connect to GitHub. Check your network connection."
            ) from exc

        _raise_for_status(response, repo)

        try:
            payload = response.json()
        except ValueError as exc:
            raise GitHubError(
                "GitHub returned an unexpected (non-JSON) response."
            ) from exc

        if not payload:
            break

        for item in payload:
            # The issues endpoint also returns pull requests; skip them.
            if "pull_request" in item:
                continue
            issues.append(
                Issue(
                    number=item["number"],
                    title=item["title"],
                    body=_truncate_body(item.get("body")),
                    url=item["html_url"],
                    labels=tuple(
                        label["name"] for label in item.get("labels", [])
                    ),
                    comments=item.get("comments", 0),
                    created_at=item["created_at"],
                    updated_at=item["updated_at"],
                )
            )
            if len(issues) >= max_issues:
                break

        # A short page means there are no further results to request.
        if len(payload) < _PER_PAGE:
            break
        page += 1

    return issues
