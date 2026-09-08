# pr-review

PR review health metrics for a GitHub org or user, fetched in batches through `gh api graphql`.
Built to measure whether a team's PR review standard is actually changing behaviour: are reviews
spread across the team, are they fast, do they say anything, and are approvals physically plausible.

## Metrics

| # | Metric | What it tells you |
|---|---|---|
| 1 | **Reviewer concentration** — top-1 / top-2 share of approvals and of review participation | Bottleneck and bus-factor risk; whether "anyone can and should review" is real |
| 2 | **Time to first review** — median, p90, % within SLA (business hours, weekends excluded) | Whether the first-feedback SLA holds |
| 3 | **Substantive-comment rate** — share of PRs with ≥1 non-nit, non-trivial comment or change request, overall and for large PRs | Whether review *happened*, without rewarding comment volume |
| 4 | **Faster-than-reading approvals** — approvals whose lines ÷ minutes since request exceed a plausible reading speed | Rubber stamps, as a matter of physics rather than opinion |

## Setup

```bash
gh auth login              # once; for GitHub Enterprise: gh auth login --hostname github.yourcompany.com
cp config.example.json config.json   # fill in owner + repos (config.json is git-ignored)
```

Python 3.9+ standard library only — nothing to install.

## Usage

```bash
# everything from config.json, last 30 days
./pr_review_metrics.py

# explicit window, JSON + Markdown outputs for a dashboard / retro
./pr_review_metrics.py --since 2026-08-01 --until 2026-08-31 --json aug.out.json --markdown aug.report.md

# ad-hoc, no config file
./pr_review_metrics.py --owner my-org --repos portal-backend,portal-frontend --since 2026-08-01

# GitHub Enterprise
./pr_review_metrics.py --host github.yourcompany.com ...

# team-level view for a shared dashboard: replace logins with reviewer-1, reviewer-2, ...
./pr_review_metrics.py --no-names
```

Omit `repos` to scan every repository owned by `owner`.

## How batching works

One GraphQL `search` request returns up to 50 merged PRs across all configured repos, with each PR's
reviews, review-thread comments, and review-request / ready-for-review events nested in the same
response. A 30-PR month is a single request; a 1,000-PR quarter is ~20. If a window would exceed
GitHub's 1,000-result search cap, the script halves the date range and recurses.

## Definitions

- **Reviewer** — any non-author, non-bot account that submitted a review or an inline comment.
  Each reviewer is counted once per PR (re-reviews don't inflate counts).
- **First review** — the first submitted review (approve / request changes / comment) or inline comment,
  measured from the earliest review-request event; falls back to the ready-for-review event, then PR creation.
  Weekends are excluded using `tz`; holidays are not.
- **Substantive** — a comment or review body that is not prefixed `nit`, is not a trivial phrase
  (`LGTM`, `+1`, …), and is at least `min_comment_chars` long; a "changes requested" review always counts.
- **Faster-than-reading** — for each approval on a PR of at least `min_lines_for_reading_check` lines,
  `(additions + deletions) / minutes` from the request aimed at that reviewer (or the earliest request)
  to the approval. Above `reading_speed_lpm` (default 100 lines/min) is flagged. `bare` marks approvals
  with no comments and no summary text.

## Caveats

- Metrics are **team health signals, not individual scorecards**. Track trends against a baseline;
  use `--no-names` for anything shared widely.
- Only the first 50 reviews / 50 threads × 30 comments per PR are fetched; enormous PRs may be undercounted.
- Reviews that started before a formal review request (e.g. after a Slack ping) will look faster than
  they were — read flagged rows as "open this PR in the retro", not as verdicts.
- The `search` API counts PRs by merge date; PRs merged outside the window are not included even if
  reviewed inside it.
