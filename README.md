# issue-triage

Point it at a public GitHub repository and it gives a maintainer a fast read on
the open-issue backlog: which issues are on fire, which have gone stale, which
look like the same underlying problem, what might block a release, what could be
closed quickly, and the single most important thing to do today. It combines
plain deterministic rules (issue age, comment counts, urgency scoring) with one
LLM pass that reads the issue text and makes the judgment calls code cannot —
then prints it all as a clean terminal report.

## Prerequisites

- **Python 3.10 or newer**
- **An Anthropic API key** — required; the analysis step calls Claude.
- **A GitHub personal access token** — optional. Without one you share GitHub's
  unauthenticated limit of 60 requests/hour; with one (public-repo read scope is
  enough) the limit rises to 5,000/hour.

## Installation

```bash
git clone https://github.com/x-guo001/issue-triage issue-triage
cd issue-triage

python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

pip install -e .
```

This installs the tool and its dependencies (`requests`, `python-dotenv`,
`rich`, `anthropic`) from `pyproject.toml`.

As a simpler alternative, if you would rather not install the package itself,
you can install just the dependencies and run `main.py` directly:

```bash
pip install requests python-dotenv rich anthropic
```

## Configuration

Configuration is read from a `.env` file in the project root. Copy the provided
template and fill in your values:

```bash
cp .env.example .env
```

`.env` is git-ignored and must never be committed. The variables:

| Variable            | Required | Purpose                                                                 |
| ------------------- | -------- | ----------------------------------------------------------------------- |
| `GITHUB_TOKEN`      | No       | GitHub token; raises the API rate limit from 60 to 5,000 requests/hour. |
| `LLM_PROVIDER`      | No       | Which LLM backend to use. Defaults to `anthropic`.                      |
| `ANTHROPIC_API_KEY` | Yes      | Your Anthropic API key, used when `LLM_PROVIDER=anthropic`.             |
| `MAX_ISSUES`        | No       | Default number of issues to fetch when `--max-issues` is omitted. Falls back to 50 if unset or invalid. |
| `LOG_LEVEL`         | No       | Logging verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`).               |

See `.env.example` for the exact format and placeholder values.

## Usage

Basic command — triage a repository (defaults to 50 issues, or whatever
`MAX_ISSUES` is set to in `.env`):

```bash
python main.py --repo owner/name
```

Limit how many open issues are fetched and analyzed (overrides `MAX_ISSUES`):

```bash
python main.py --repo owner/name --max-issues 30
```

Examples:

```bash
# Triage the requests library
python main.py --repo psf/requests

# Triage a larger project, capped at 30 issues
python main.py --repo deepchem/deepchem --max-issues 30
```

Other flags: `--version` prints the version, `--help` shows usage.

## How provider swapping works

The LLM backend is selected by the `LLM_PROVIDER` environment variable, which
defaults to `anthropic`. The analysis layer is written against this seam rather
than hard-coding a vendor, so adding a second backend (for example OpenAI) is a
matter of implementing one client function and adding a branch — no changes to
the pipeline, the CLI, or the report. Today only `anthropic` is implemented;
any other value produces a clean "not supported yet" message.

## Understanding the output

The report has seven sections, in order. Two are produced by deterministic code
and four by the LLM:

- **Header** — repository, number of issues analyzed, and a timestamp.
- **FIRES** *(code)* — issues with a non-zero urgency score, ranked highest
  first. Urgency is computed from rules: newly active issues, issues with no
  response, stale issues, and bug/regression labels.
- **STALE** *(code)* — issues untouched for over 30 days with little
  discussion, shown with how long they've been idle.
- **CLUSTERS** *(LLM)* — groups of issues that appear to share one root
  problem, by number.
- **RELEASE BLOCKERS** *(LLM)* — issues whose text suggests they would block
  a release, with the reasoning.
- **QUICK WINS** *(LLM)* — issues that could likely be closed quickly
  (duplicate, needs-info, out of scope), with why.
- **ONE THING** *(LLM)* — the single most important action to take today.

Any section with nothing to report prints `None identified.`

## Limitations

- **Issue cap.** Defaults to 50 issues per run (or `MAX_ISSUES` from `.env`);
  override per run with `--max-issues`. The cap exists to bound both GitHub
  paging and LLM input.
- **Public repositories only.** Private repos are not supported.
- **One LLM call per run.** The entire backlog is analyzed in a single request
  with a capped output, which keeps it fast and cheap but means very large
  repositories should be triaged in batches by tuning `--max-issues`.
