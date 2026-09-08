#!/usr/bin/env python3
# =============================================================================================
#  PR REVIEW HEALTH METRICS  —  single-file tool, monthly breakdown
# =============================================================================================
#
#  WHAT THIS DOES
#  --------------
#  Pulls every merged pull request from a set of GitHub repositories, month by month starting
#  at START_MONTH (default 2026-06), and prints ONE Markdown table: one column per month plus a
#  Total column, one row per statistic. Four metric families:
#
#    1. Reviewer concentration   top-1 / top-2 share of approvals, and of review participation
#                                (how concentrated review work is on one or two people)
#    2. Time to first review     business hours (weekends excluded) from the review request to
#                                the first submitted review: median, p90, % within the SLA
#    3. Substantive-comment rate share of PRs that received at least one non-nit, non-trivial
#                                comment or a "changes requested", overall and for large PRs
#    4. Faster-than-reading approvals   approvals where (lines changed / minutes since the review
#                                request) exceeds a plausible reading speed: physical evidence of
#                                a rubber stamp
#
#  PREREQUISITES  (an operator — human or AI — must make sure these are true before running)
#  -------------
#    1. python3 3.9 or newer. No third-party packages; standard library only.
#    2. The GitHub CLI `gh` installed and logged in with an account that can read the repos:
#           gh auth login                                        # github.com
#           gh auth login --hostname github.yourcompany.com      # GitHub Enterprise
#       Verify with:  gh auth status
#    3. Network access to GitHub from the machine running this.
#
#  HOW TO CONFIGURE  (two options; command-line flags override the CONFIG block)
#  ----------------
#    Option A — edit the CONFIG block right below this header:
#         OWNER        the GitHub org or user handle that owns the repos (REQUIRED)
#         REPOS        list of repo names under OWNER; empty list = every repo of OWNER
#         HOST         GitHub Enterprise hostname, or None for github.com
#         START_MONTH  first month to report, "YYYY-MM"
#    Option B — pass flags:
#         python3 pr_review_metrics.py --owner my-org --repos portal-backend,portal-frontend
#         python3 pr_review_metrics.py --host github.company.com --start 2026-06
#
#  HOW TO RUN
#  ----------
#         python3 pr_review_metrics.py                          # table to stdout, progress to stderr
#         python3 pr_review_metrics.py --markdown report.md     # also save the table
#         python3 pr_review_metrics.py --json results.json      # also save every number + per-PR details
#         python3 pr_review_metrics.py --no-names               # anonymise reviewers as reviewer-1, reviewer-2, …
#         python3 pr_review_metrics.py --help                   # all flags
#
#  OUTPUT
#  ------
#  Section 1: the summary table (rows = statistics, columns = months + Total).
#  Section 2: per-month drill-down lists — top reviewers, PRs merged without review, and the
#             approvals flagged as faster than reading — so a retro can open the actual PRs.
#  --json writes the same plus per-PR detail; --markdown writes the printed report to a file.
#
#  HOW THE FETCHING IS BATCHED
#  ---------------------------
#  One GraphQL `search` request returns up to 50 merged PRs across ALL configured repos, with each
#  PR's reviews, inline review comments, and review-request / ready-for-review events nested in the
#  same response. Each month is one search window (a 30-PR month = one request). If a window would
#  exceed GitHub's 1,000-result search cap, the window is halved and fetched recursively.
#
#  METRIC DEFINITIONS
#  ------------------
#    Reviewer      any account that is not the PR author and not a bot and submitted a review or an
#                  inline comment. Counted once per PR (re-reviews do not inflate counts).
#    First review  first submitted review (approve / changes requested / comment) or inline comment,
#                  timed from the earliest review-request event; falls back to the ready-for-review
#                  event, then PR creation. Weekends excluded using TZ; public holidays are NOT.
#    Substantive   comment or review text that is not prefixed "nit", is not a trivial phrase
#                  ("LGTM", "+1", …) and has at least MIN_COMMENT_CHARS characters; a
#                  "changes requested" review always counts.
#    Large PR      additions + deletions >= LARGE_PR_LINES.
#    Faster-than-reading   for each approval on a PR with >= MIN_LINES_FOR_READING_CHECK lines:
#                  lines / minutes between the review request aimed at that reviewer (or the earliest
#                  request) and the approval; flagged when above READING_SPEED_LPM lines per minute.
#                  "bare" = the approval carried no comments and no summary text.
#
#  CAVEATS  (say these out loud when presenting the numbers)
#  -------
#    - These are TEAM health signals, not individual scorecards. Compare months to each other.
#    - Only the first 50 reviews and 50 threads x 30 comments per PR are fetched; giant PRs may be
#      undercounted.
#    - A reviewer who started reading before a formal review request (e.g. after a chat ping) will
#      look faster than they were. Treat flagged rows as "open this PR and look", not as verdicts.
#    - PRs are bucketed by MERGE date. The current month is partial ("to date").
#
#  TROUBLESHOOTING
#  ---------------
#    "gh: command not found"            install GitHub CLI (https://cli.github.com), then gh auth login
#    "gh auth login" / HTTP 401         not logged in, or logged in to the wrong host -> use --host
#    "Could not resolve to a Repository" OWNER or a REPOS entry is misspelled or not readable
#    "No merged PRs found"              wrong window or wrong repos; check --start / REPOS
#    rate limit exhausted               wait for the reset time printed in stderr and rerun
# =============================================================================================

import argparse
import json
import math
import os
import statistics
import subprocess
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

# ------------------------------------------------------------------ CONFIG  (edit these) -----
OWNER = ""                              # REQUIRED: GitHub org or user handle, e.g. "my-org"
REPOS = []                              # e.g. ["portal-backend", "portal-frontend"]; [] = all repos of OWNER
HOST = None                             # e.g. "github.mycompany.com"; None = github.com
START_MONTH = "2026-06"                 # first month in the report, "YYYY-MM"
END_DATE = None                         # "YYYY-MM-DD" inclusive; None = today

TZ = "America/Toronto"                  # timezone used to decide what a weekend is
SLA_HOURS = 24                          # first-review SLA in business hours
LARGE_PR_LINES = 200                    # additions + deletions at/above which a PR is "large"
READING_SPEED_LPM = 100                 # lines per minute above which an approval is implausible
MIN_LINES_FOR_READING_CHECK = 50        # ignore tiny PRs in the reading-speed check
MIN_COMMENT_CHARS = 12                  # shorter comments ("LGTM", "+1") are not substantive
BOTS = ["dependabot", "github-actions", "copilot", "renovate"]   # login substrings treated as bots
EXCLUDE_AUTHORS = []                    # PR authors to drop entirely (automation accounts)
NO_NAMES = False                        # True = print reviewer-1, reviewer-2, … instead of logins
# ------------------------------------------------------------------------------------------------

PAGE_SIZE = 50
SEARCH_CAP = 1000  # GitHub search never returns more than this many results per query

GRAPHQL = """
query($q: String!, $after: String, $pageSize: Int!) {
  rateLimit { cost remaining resetAt }
  search(query: $q, type: ISSUE, first: $pageSize, after: $after) {
    issueCount
    pageInfo { hasNextPage endCursor }
    nodes {
      ... on PullRequest {
        number title url isDraft createdAt mergedAt additions deletions changedFiles
        repository { nameWithOwner }
        author { login }
        timelineItems(first: 50, itemTypes: [REVIEW_REQUESTED_EVENT, READY_FOR_REVIEW_EVENT]) {
          nodes {
            __typename
            ... on ReviewRequestedEvent {
              createdAt
              requestedReviewer { __typename ... on User { login } ... on Team { slug } }
            }
            ... on ReadyForReviewEvent { createdAt }
          }
        }
        reviews(first: 50) {
          nodes { author { login } state submittedAt body comments { totalCount } }
        }
        reviewThreads(first: 50) {
          nodes { comments(first: 30) { nodes { author { login } body createdAt } } }
        }
      }
    }
  }
}
"""

TRIVIAL_PHRASES = {"lgtm", "looks good", "looks good to me", "approved", "approve", "+1", "ok", "okay",
                   "ship it", "nice", "thanks", "thank you", "done", "good", "great"}


# ============================================================================ helpers ========

def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def load_tz(name):
    if ZoneInfo is None:
        return timezone.utc
    try:
        return ZoneInfo(name)
    except Exception:
        print(f"warning: timezone {name!r} not found, using UTC for weekend detection", file=sys.stderr)
        return timezone.utc


def business_hours(t0, t1, tz):
    """Hours between t0 and t1 excluding Saturdays and Sundays (in tz). Holidays are not excluded."""
    if t0 is None or t1 is None or t1 <= t0:
        return 0.0
    cur, end, total = t0.astimezone(tz), t1.astimezone(tz), timedelta()
    while cur < end:
        next_midnight = (cur + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        seg_end = min(next_midnight, end)
        if cur.weekday() < 5:
            total += seg_end - cur
        cur = seg_end
    return total.total_seconds() / 3600.0


def percentile(values, p):
    if not values:
        return None
    s = sorted(values)
    k = max(0, min(len(s) - 1, math.ceil(p / 100.0 * len(s)) - 1))
    return s[k]


def is_bot(login, bots):
    if not login:
        return True
    low = login.lower()
    return low.endswith("[bot]") or any(b in low for b in bots)


def is_substantive_text(text, min_chars):
    t = (text or "").strip()
    if not t:
        return False
    low = t.lower()
    if low.startswith("nit") and (len(low) == 3 or not low[3].isalpha()):
        return False
    if low.strip(" .!:") in TRIVIAL_PHRASES:
        return False
    return len(t) >= min_chars


def pct(n, d):
    return (100.0 * n / d) if d else None


def month_windows(start_month, end_date):
    """[(label, since, until)] for every month from start_month to end_date inclusive."""
    y, m = (int(x) for x in start_month.split("-"))
    out = []
    while date(y, m, 1) <= end_date:
        first = date(y, m, 1)
        last = (date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)) - timedelta(days=1)
        out.append((first.strftime("%Y-%m"), first, min(last, end_date)))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


# ============================================================================ fetching =======

def gh_graphql(variables, host):
    cmd = ["gh", "api", "graphql", "-f", f"query={GRAPHQL}"]
    for k, v in variables.items():
        if v is None:
            continue
        cmd += (["-F", f"{k}={v}"] if isinstance(v, int) else ["-f", f"{k}={v}"])
    env = dict(os.environ)
    if host:
        env["GH_HOST"] = host
    res = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if res.returncode != 0:
        sys.exit(f"gh api graphql failed:\n{res.stderr.strip()}\n{res.stdout.strip()}")
    data = json.loads(res.stdout)
    if data.get("errors"):
        sys.exit("GraphQL errors:\n" + json.dumps(data["errors"], indent=2))
    return data["data"]


def build_search(cfg, since, until):
    scope = " ".join(f"repo:{cfg['owner']}/{r}" for r in cfg["repos"]) if cfg["repos"] else f"user:{cfg['owner']}"
    return f"is:pr is:merged merged:{since.isoformat()}..{until.isoformat()} {scope}"


def fetch_window(cfg, since, until):
    """Raw PR nodes merged in [since, until]; halves the window when the search cap is hit."""
    q = build_search(cfg, since, until)
    nodes, after, first = [], None, True
    while True:
        data = gh_graphql({"q": q, "after": after, "pageSize": PAGE_SIZE}, cfg["host"])
        search = data["search"]
        if first:
            first = False
            count = search["issueCount"]
            if count > SEARCH_CAP and (until - since).days >= 1:
                mid = since + (until - since) / 2
                print(f"    {since}..{until}: {count} PRs > {SEARCH_CAP}, splitting window", file=sys.stderr)
                return fetch_window(cfg, since, mid) + fetch_window(cfg, mid + timedelta(days=1), until)
            print(f"    {since}..{until}: {count} merged PRs, ~{max(1, math.ceil(count / PAGE_SIZE))} request(s)", file=sys.stderr)
        nodes += [n for n in search["nodes"] if n]
        rl = data.get("rateLimit") or {}
        print(f"      fetched {len(nodes)} (rate limit remaining {rl.get('remaining')}, resets {rl.get('resetAt')})", file=sys.stderr)
        if not search["pageInfo"]["hasNextPage"]:
            return nodes
        after = search["pageInfo"]["endCursor"]


# ============================================================================ normalise ======

def normalise(node, cfg):
    author = (node.get("author") or {}).get("login") or ""
    bots = cfg["bots"]

    requests, ready_at = [], None
    for ev in (node.get("timelineItems") or {}).get("nodes") or []:
        if not ev:
            continue
        if ev["__typename"] == "ReviewRequestedEvent":
            rr = ev.get("requestedReviewer") or {}
            requests.append((parse_ts(ev["createdAt"]), rr.get("login") or rr.get("slug")))
        elif ev["__typename"] == "ReadyForReviewEvent":
            ready_at = parse_ts(ev["createdAt"])

    reviews = []
    for r in (node.get("reviews") or {}).get("nodes") or []:
        login = (r.get("author") or {}).get("login") or ""
        if r["state"] == "PENDING" or login == author or is_bot(login, bots) or not r.get("submittedAt"):
            continue
        reviews.append({"reviewer": login, "state": r["state"], "at": parse_ts(r["submittedAt"]),
                        "body": r.get("body") or "", "n_comments": (r.get("comments") or {}).get("totalCount", 0)})

    thread_comments = []
    for th in (node.get("reviewThreads") or {}).get("nodes") or []:
        for c in (th.get("comments") or {}).get("nodes") or []:
            login = (c.get("author") or {}).get("login") or ""
            if login == author or is_bot(login, bots):
                continue
            thread_comments.append({"reviewer": login, "body": c.get("body") or "", "at": parse_ts(c["createdAt"])})

    created = parse_ts(node["createdAt"])
    req_times = [t for t, _ in requests]
    return {
        "repo": node["repository"]["nameWithOwner"], "number": node["number"], "title": node["title"],
        "url": node["url"], "author": author, "created": created, "merged": parse_ts(node.get("mergedAt")),
        "lines": (node.get("additions") or 0) + (node.get("deletions") or 0), "files": node.get("changedFiles") or 0,
        "requests": requests, "review_baseline": min(req_times) if req_times else (ready_at or created),
        "reviews": reviews, "thread_comments": thread_comments,
    }


# ============================================================================ metrics ========

def compute(prs, cfg, tz, dropped):
    """All statistics for one bucket of PRs."""
    # --- 1. concentration
    approvals, participation, people = Counter(), Counter(), set()
    for pr in prs:
        people.add(pr["author"])
        appr = {r["reviewer"] for r in pr["reviews"] if r["state"] == "APPROVED"}
        part = {r["reviewer"] for r in pr["reviews"]} | {c["reviewer"] for c in pr["thread_comments"]}
        approvals.update(appr)
        participation.update(part)
        people.update(part)

    def shares(counter):
        total = sum(counter.values())
        ranked = counter.most_common()
        top1 = ranked[0][1] if ranked else 0
        top2 = top1 + (ranked[1][1] if len(ranked) > 1 else 0)
        return {"total": total, "top1_pct": pct(top1, total), "top2_pct": pct(top2, total),
                "by_reviewer": [{"reviewer": k, "count": v, "pct": pct(v, total)} for k, v in ranked]}

    # --- 2. time to first review
    hours, no_review = [], []
    for pr in prs:
        events = [r["at"] for r in pr["reviews"]] + [c["at"] for c in pr["thread_comments"]]
        if not events:
            no_review.append({"pr": f"{pr['repo']}#{pr['number']}", "url": pr["url"], "lines": pr["lines"]})
            continue
        hours.append({"pr": f"{pr['repo']}#{pr['number']}", "url": pr["url"],
                      "hours": business_hours(pr["review_baseline"], min(events), tz)})
    vals = [h["hours"] for h in hours]

    # --- 3. substantive-comment rate
    def substantive(pr):
        if any(r["state"] == "CHANGES_REQUESTED" or is_substantive_text(r["body"], cfg["min_comment_chars"]) for r in pr["reviews"]):
            return True
        return any(is_substantive_text(c["body"], cfg["min_comment_chars"]) for c in pr["thread_comments"])

    def silent(pr):
        return not pr["thread_comments"] and not any(r["body"].strip() for r in pr["reviews"])

    large = [p for p in prs if p["lines"] >= cfg["large_pr_lines"]]

    # --- 4. faster-than-reading approvals
    eligible, flagged = 0, []
    for pr in prs:
        if pr["lines"] < cfg["min_lines_for_reading_check"]:
            continue
        commenters = {c["reviewer"] for c in pr["thread_comments"]}
        for r in pr["reviews"]:
            if r["state"] != "APPROVED":
                continue
            eligible += 1
            own = [t for t, who in pr["requests"] if who == r["reviewer"] and t <= r["at"]]
            start = min(own) if own else pr["review_baseline"]
            minutes = max(0.5, (r["at"] - start).total_seconds() / 60.0)
            lpm = pr["lines"] / minutes
            if lpm > cfg["reading_speed_lpm"]:
                flagged.append({"pr": f"{pr['repo']}#{pr['number']}", "url": pr["url"], "reviewer": r["reviewer"],
                                "lines": pr["lines"], "files": pr["files"], "minutes": round(minutes, 1),
                                "lines_per_min": round(lpm),
                                "bare": not r["body"].strip() and r["n_comments"] == 0 and r["reviewer"] not in commenters})
    flagged.sort(key=lambda x: -x["lines_per_min"])

    return {
        "prs": len(prs), "dropped": dropped,
        "distinct_reviewers": len(participation), "people_seen": len(people),
        "approvals": shares(approvals), "participation": shares(participation),
        "ttfr_n": len(vals), "ttfr_median_h": statistics.median(vals) if vals else None,
        "ttfr_p90_h": percentile(vals, 90), "within_sla": sum(1 for v in vals if v <= cfg["sla_hours"]),
        "merged_without_review": no_review, "slowest": sorted(hours, key=lambda h: -h["hours"])[:10],
        "subst_all": sum(1 for p in prs if substantive(p)), "silent_all": sum(1 for p in prs if silent(p)),
        "large_n": len(large), "subst_large": sum(1 for p in large if substantive(p)),
        "silent_large": sum(1 for p in large if silent(p)),
        "eligible_approvals": eligible, "flagged": flagged, "flagged_bare": sum(1 for f in flagged if f["bare"]),
    }


# ============================================================================ report =========

def anonymise(buckets):
    names = {}

    def alias(login):
        if login not in names:
            names[login] = f"reviewer-{len(names) + 1}"
        return names[login]

    for b in buckets.values():
        for key in ("approvals", "participation"):
            for row in b[key]["by_reviewer"]:
                row["reviewer"] = alias(row["reviewer"])
        for row in b["flagged"]:
            row["reviewer"] = alias(row["reviewer"])


def f_pct(n, d):
    return "—" if not d else f"{100.0 * n / d:.0f}% ({n}/{d})"


def f_share(v):
    return "—" if v is None else f"{v:.0f}%"


def f_h(v):
    return "—" if v is None else f"{v:.1f}h"


def render(buckets, cfg, labels):
    p = []
    cols = labels + ["Total"]
    p.append(f"# PR review health — {cfg['owner']} ({', '.join(cfg['repos']) or 'all repos'}) — monthly from {labels[0]}")
    p.append("")
    p.append(f"Current month is partial (to {cfg['end_date']}). Bots and PR authors are excluded from reviewer statistics; "
             f"business hours exclude weekends ({cfg['tz']}).")
    p.append("")
    rows = [
        ("Merged PRs analysed", lambda b: str(b["prs"])),
        ("Bot / excluded-author PRs dropped", lambda b: str(b["dropped"])),
        ("Distinct reviewers / people seen", lambda b: f"{b['distinct_reviewers']} / {b['people_seen']}"),
        ("Approvals (unique reviewer × PR)", lambda b: str(b["approvals"]["total"])),
        ("Approval concentration — top-1", lambda b: f_share(b["approvals"]["top1_pct"])),
        ("Approval concentration — top-2", lambda b: f_share(b["approvals"]["top2_pct"])),
        ("Review participation — top-1", lambda b: f_share(b["participation"]["top1_pct"])),
        ("Review participation — top-2", lambda b: f_share(b["participation"]["top2_pct"])),
        ("Time to first review — median", lambda b: f_h(b["ttfr_median_h"])),
        ("Time to first review — p90", lambda b: f_h(b["ttfr_p90_h"])),
        (f"First review within {cfg['sla_hours']:g}h SLA", lambda b: f_pct(b["within_sla"], b["ttfr_n"])),
        ("Merged without any review", lambda b: str(len(b["merged_without_review"]))),
        ("Substantive-comment rate — all PRs", lambda b: f_pct(b["subst_all"], b["prs"])),
        (f"Substantive-comment rate — large PRs (≥{cfg['large_pr_lines']} lines)", lambda b: f_pct(b["subst_large"], b["large_n"])),
        ("PRs with no comments at all", lambda b: str(b["silent_all"])),
        ("Large PRs with no comments at all", lambda b: f"{b['silent_large']} / {b['large_n']}"),
        (f"Faster-than-reading approvals (>{cfg['reading_speed_lpm']:g} lines/min, PRs ≥{cfg['min_lines_for_reading_check']} lines)",
         lambda b: f_pct(len(b["flagged"]), b["eligible_approvals"])),
        ("  …of which bare (no comments, no text)", lambda b: str(b["flagged_bare"])),
    ]
    p.append("## 1. Summary")
    p.append("")
    p.append("| Statistic | " + " | ".join(cols) + " |")
    p.append("|---|" + "---:|" * len(cols))
    for label, fn in rows:
        p.append(f"| {label} | " + " | ".join(fn(buckets[c]) if buckets[c]["prs"] else "—" for c in cols) + " |")
    p.append("")

    p.append("## 2. Drill-down by month")
    for c in labels:
        b = buckets[c]
        p.append("")
        p.append(f"### {c}  ({b['prs']} merged PRs)")
        if not b["prs"]:
            p.append("- no merged PRs")
            continue
        top = ", ".join(f"{r['reviewer']} {r['pct']:.0f}%" for r in b["approvals"]["by_reviewer"][:3]) or "none"
        p.append(f"- Top approvers: {top}")
        part = ", ".join(f"{r['reviewer']} {r['count']}" for r in b["participation"]["by_reviewer"][:5]) or "none"
        p.append(f"- Most active reviewers (PRs reviewed): {part}")
        if b["merged_without_review"]:
            p.append("- Merged without review: " + ", ".join(x["pr"] for x in b["merged_without_review"][:10])
                     + (f" (+{len(b['merged_without_review']) - 10} more)" if len(b["merged_without_review"]) > 10 else ""))
        slow = [s for s in b["slowest"] if s["hours"] > cfg["sla_hours"]][:5]
        if slow:
            p.append("- Slowest first reviews: " + ", ".join(f"{s['pr']} ({s['hours']:.0f}h)" for s in slow))
        if b["flagged"]:
            p.append("")
            p.append(f"Faster-than-reading approvals in {c}:")
            p.append("")
            p.append("| PR | Reviewer | Lines | Minutes since request | Lines/min | Bare |")
            p.append("|---|---|---:|---:|---:|:---:|")
            for f in b["flagged"][:15]:
                p.append(f"| {f['pr']} | {f['reviewer']} | {f['lines']} | {f['minutes']} | {f['lines_per_min']} | {'yes' if f['bare'] else ''} |")
            if len(b["flagged"]) > 15:
                p.append(f"| … {len(b['flagged']) - 15} more in JSON | | | | | |")
    p.append("")
    p.append("_Definitions and caveats are in the header comment of pr_review_metrics.py._")
    return "\n".join(p)


# ============================================================================ main ===========

def main():
    ap = argparse.ArgumentParser(description="PR review health metrics, monthly. See the header comment for full instructions.")
    ap.add_argument("--owner", default=OWNER, help="GitHub org or user handle owning the repos")
    ap.add_argument("--repos", default=",".join(REPOS), help="comma-separated repo names; empty = all repos of owner")
    ap.add_argument("--host", default=HOST, help="GitHub Enterprise host (default github.com)")
    ap.add_argument("--start", default=START_MONTH, help="first month YYYY-MM (default %(default)s)")
    ap.add_argument("--end", default=END_DATE, help="last day YYYY-MM-DD inclusive (default today)")
    ap.add_argument("--tz", default=TZ)
    ap.add_argument("--sla-hours", dest="sla_hours", type=float, default=SLA_HOURS)
    ap.add_argument("--large-pr-lines", dest="large_pr_lines", type=int, default=LARGE_PR_LINES)
    ap.add_argument("--reading-speed", dest="reading_speed_lpm", type=float, default=READING_SPEED_LPM)
    ap.add_argument("--min-lines", dest="min_lines_for_reading_check", type=int, default=MIN_LINES_FOR_READING_CHECK)
    ap.add_argument("--min-comment-chars", dest="min_comment_chars", type=int, default=MIN_COMMENT_CHARS)
    ap.add_argument("--bots", default=",".join(BOTS), help="comma-separated login substrings treated as bots")
    ap.add_argument("--exclude-authors", dest="exclude_authors", default=",".join(EXCLUDE_AUTHORS))
    ap.add_argument("--no-names", dest="no_names", action="store_true", default=NO_NAMES, help="anonymise reviewer logins")
    ap.add_argument("--json", dest="json_out", help="also write all results (with per-PR detail) to this JSON file")
    ap.add_argument("--markdown", dest="md_out", help="also write the report to this Markdown file")
    a = ap.parse_args()

    if not a.owner:
        sys.exit("error: OWNER is required — set it in the CONFIG block or pass --owner")
    end_date = date.fromisoformat(a.end) if a.end else date.today()
    cfg = {
        "owner": a.owner, "repos": [r.strip() for r in a.repos.split(",") if r.strip()], "host": a.host,
        "start": a.start, "end_date": end_date.isoformat(), "tz": a.tz, "sla_hours": a.sla_hours,
        "large_pr_lines": a.large_pr_lines, "reading_speed_lpm": a.reading_speed_lpm,
        "min_lines_for_reading_check": a.min_lines_for_reading_check, "min_comment_chars": a.min_comment_chars,
        "bots": [b.strip().lower() for b in a.bots.split(",") if b.strip()],
        "exclude_authors": [x.strip() for x in a.exclude_authors.split(",") if x.strip()], "no_names": a.no_names,
    }
    tz = load_tz(cfg["tz"])
    windows = month_windows(cfg["start"], end_date)
    if not windows:
        sys.exit(f"error: no months between {cfg['start']} and {end_date}")

    print(f"Fetching merged PRs for {cfg['owner']} ({', '.join(cfg['repos']) or 'all repos'}), "
          f"{len(windows)} month(s) from {windows[0][0]} to {end_date}", file=sys.stderr)
    buckets, all_prs, all_dropped = {}, [], 0
    for label, since, until in windows:
        print(f"  {label}", file=sys.stderr)
        raw = fetch_window(cfg, since, until)
        prs = [normalise(n, cfg) for n in raw]
        prs = [p for p in prs if p["author"] not in cfg["exclude_authors"] and not is_bot(p["author"], cfg["bots"])]
        dropped = len(raw) - len(prs)
        buckets[label] = compute(prs, cfg, tz, dropped)
        all_prs += prs
        all_dropped += dropped
    buckets["Total"] = compute(all_prs, cfg, tz, all_dropped)
    if not all_prs:
        sys.exit("No merged PRs found in that window — check OWNER / REPOS / --start.")
    if cfg["no_names"]:
        anonymise(buckets)

    labels = [w[0] for w in windows]
    report = render(buckets, cfg, labels)
    print(report)
    if a.md_out:
        with open(a.md_out, "w") as fh:
            fh.write(report + "\n")
        print(f"\nMarkdown written to {a.md_out}", file=sys.stderr)
    if a.json_out:
        with open(a.json_out, "w") as fh:
            json.dump({"config": cfg, "months": labels, "buckets": buckets}, fh, indent=2, default=str)
        print(f"JSON written to {a.json_out}", file=sys.stderr)


if __name__ == "__main__":
    main()
