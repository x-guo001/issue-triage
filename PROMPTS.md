# Prompts

This document records the key structured prompts used to direct Claude Code
while building this tool. It is included to make the development process
transparent: the tool was built layer by layer, with each layer specified by a
deliberate prompt rather than ad-hoc back-and-forth.

A few prompting techniques recur throughout and are worth noting up front:

- **Layering.** The build was split into explicit layers (scaffolding →
  deterministic pre-processing → LLM analysis → CLI). Each prompt scoped one
  layer so it could be reviewed before the next began.
- **Step-gating.** Several prompts asked Claude to complete one step, report,
  and wait for confirmation — keeping a human in the loop at each commit.
- **Show-before-write.** For the larger modules, Claude was asked to show the
  proposed file before writing it to disk, so the design could be corrected
  cheaply (in chat) rather than expensively (in code review).
- **Code-vs-LLM separation.** The prompts deliberately drew a line between work
  that deterministic code should do and work that only an LLM can do.

---

## 1. Layer 1 — Project scaffolding

**Purpose:** Stand up the repository, secrets hygiene, dependency manifest, and
the GitHub client. The prompt front-loads the full project requirements (so
later layers have shared context) but explicitly scopes the work to "Layer 1
only," and enforces a secrets-first ordering: `.gitignore` with `.env` listed
before any other file exists, committed on its own.

```text
I'm building a GitHub issue triage tool for a job assessment. Please set up the
project from scratch.

Requirements:
- Python 3.10+, Google Python Style Guide
- Takes a public GitHub repo as input (owner/name format)
- Fetches open issues via GitHub REST API
- Uses an LLM to summarize and prioritize the issues
- Outputs a clean terminal report using rich
- Secrets managed via .env (never committed)
- LLM provider is Anthropic (Claude). Design it so OpenAI can be added later by
  changing one env var
- Bounded: max 50 issues default, max_tokens cap, clean error messages not raw
  tracebacks

Please do Layer 1 only for now:
1. Create a folder called issue-triage
2. cd into it and run git init
3. Create .gitignore with .env listed FIRST, before any other files exist
4. Make the first git commit with only .gitignore
5. Create .env.example with placeholder values for: GITHUB_TOKEN, LLM_PROVIDER,
   ANTHROPIC_API_KEY, MAX_ISSUES, LOG_LEVEL
6. Create pyproject.toml with dependencies: requests, python-dotenv, rich,
   anthropic
7. Create github_client.py with: a fetch_issues(repo, max_issues) function,
   pagination, pull request filtering, body truncation at first 500 characters
   of each issue body, and clean error handling for 403/404/timeout/connection
   errors
8. Commit each file as you go with descriptive commit messages

Do NOT create a .env file - I will do that manually after.

After each step, tell me what you did and wait for me to confirm before moving
to the next step.
```

**Why it worked:** The numbered, ordered steps plus "wait for me to confirm"
turned scaffolding into a reviewable sequence. The secrets-first ordering
(`.gitignore` committed alone, first) made it structurally impossible to leak
`.env`.

---

## 2. Layer 2a — `triage.py` (deterministic pre-processor)

**Purpose:** Define the traditional-code layer that runs *before* the LLM,
enriching each issue with calculated fields and aggregate stats. The prompt is
precise about every rule (thresholds, scoring weights) so the logic is fully
specified and testable, with no room for the model to improvise.

```text
Before writing analyzer.py, I want to split the work properly between
traditional code and LLM.

FILE 1: triage.py (traditional code pre-processor)
This runs BEFORE the LLM and enriches each Issue object with calculated fields:
- days_since_created: integer, calculated from created_at to today
- days_since_updated: integer, calculated from updated_at to today
- is_stale: True if days_since_updated > 30 and comments < 3
- is_hot: True if days_since_created < 7 and comments >= 5
- has_no_response: True if comments == 0
- urgency_score: integer score (hot=3 points, has_no_response=1 point,
  is_stale=1 point, each label matching "bug" or "regression" adds 2 points)

Also produce these aggregate stats using only traditional code (no LLM needed):
- total_issues: count
- hot_count: how many are hot
- stale_count: how many are stale
- no_response_count: how many have zero comments
- top_labels: 5 most common labels across all issues

Show me triage.py first, wait for my approval, then show me analyzer.py.
```

**Why it worked:** Exhaustively specifying the scoring rules kept all business
logic in deterministic code where it can be unit-tested. The reviewer later
highlighted one resulting design choice — an injectable `today` parameter — as a
good example of testable design.

---

## 3. Layer 2b — `analyzer.py` (LLM analysis layer)

**Purpose:** Define the LLM layer so it is asked *only* for what code cannot do.
The prompt enumerates the four judgments the model should make, explicitly tells
the model the pre-computed fields are already accurate (so it should not
recalculate dates), and reserves the FIRES/STALE sections for traditional code.

```text
FILE 2: analyzer.py (LLM layer)
This receives the enriched issues from triage.py and calls the Anthropic API.
The prompt should:
- Tell the LLM the pre-calculated fields are already accurate (no need to
  recalculate dates)
- Ask ONLY for what traditional code cannot do:
  1. CLUSTERS: groups of issues that appear to be the same root problem
  2. RELEASE BLOCKERS: issues that sound like they would block a release
  3. QUICK WINS: issues that could be closed quickly
  4. ONE THING: single most important action today, one sentence
- The FIRES section should be built by traditional code from urgency_score, not
  the LLM
- The STALE section should be built by traditional code from is_stale, not the
  LLM
- Set max_tokens=1000
- Use claude-sonnet-4-6
- Handle auth errors and rate limits cleanly

This is a pipeline not an agent — single LLM call, no loops.
Show me analyzer.py before creating it.
```

**Refinements made during review:** the model string was verified against the
installed Anthropic SDK before approval; aggregate stats were added as the first
line of the prompt for richer context; and the JSON-parsing approach (over
tool-use) was retained deliberately.

**Why it worked:** Naming the four LLM tasks and fencing off everything else
("built by traditional code, not the LLM") produced a tight prompt and a small,
predictable output. Calling it "a pipeline not an agent" set the expectation of
a single bounded call.

---

## 4. Layer 3 — `main.py` (CLI entry point)

**Purpose:** Wire the pipeline together behind a clean command-line interface,
with progress feedback, an ordered report, and error handling that never shows a
raw traceback.

```text
Please build main.py — the CLI entry point that wires everything together.

Requirements:
- Load .env first thing using load_dotenv()
- Accept two CLI arguments:
  --repo (required): repository in owner/name format
  --max-issues (optional): integer, defaults to 50
- Call the pipeline in order: fetch_issues() -> triage() -> analyze()
- Show a progress spinner using rich while fetching and while waiting for LLM
- Print the final report using rich with these sections in order:
  1. Header: repo name, total issues analyzed, timestamp
  2. FIRES: urgency_score > 0, sorted by score descending, from code (no LLM)
  3. STALE: is_stale=True, from code (no LLM)
  4. CLUSTERS: from analyzer
  5. RELEASE BLOCKERS: from analyzer
  6. QUICK WINS: from analyzer
  7. ONE THING: from analyzer, displayed prominently
- If any section has nothing to show, print "None identified."
- Catch GitHubError and AnalyzerError and print clean error messages to stderr,
  exit with code 1
- No raw tracebacks should ever reach the user
- Add a --version flag that prints the version from pyproject.toml

Show me main.py before creating it.
```

**Why it worked:** Specifying the section order, the empty-state text, the exit
code, and the "no raw tracebacks" rule left no ambiguity in the user-facing
contract. "Show me main.py before creating it" kept the design reviewable before
it hit disk.

---

## Summary

| Layer | File(s)            | Prompt focus                                  |
| ----- | ------------------ | --------------------------------------------- |
| 1     | scaffolding        | Ordered, step-gated setup; secrets-first      |
| 2a    | `triage.py`        | Fully-specified deterministic rules           |
| 2b    | `analyzer.py`      | LLM asked only for non-deterministic judgments|
| 3     | `main.py`          | CLI contract: order, errors, exit codes       |

The throughline: **say exactly what deterministic code must do, fence the LLM to
only the judgment calls, and review each layer before the next.**
