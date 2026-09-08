#!/usr/bin/env python3
"""
PR review health metrics, pulled from GitHub in batches via GraphQL search.

Metrics
  1. Reviewer concentration       top-1 / top-2 share of approvals, and of review participation
  2. Time to first review         business hours (weekends excluded) from review request to first
                                  submitted review: median, p90, % within SLA
  3. Substantive-comment rate     share of PRs that got at least one non-nit, non-trivial comment
                                  (or a "changes requested") before merge; overall and for large PRs
  4. Faster-than-reading approvals  approvals whose (lines changed / minutes since request) exceeds a
                                  plausible reading speed -- physical evidence of a rubber stamp

Batching: one GraphQL `search` request returns up to 50 merged PRs across ALL configured repos with
their reviews, review-thread comments and review-request events nested in the same response. A
30-PR/month team is one request; a 1,000-PR quarter is ~20. The date window is split automatically
if a search would exceed GitHub's 1,000-result cap.

Requirements: python3 (stdlib only) and an authenticated `gh` CLI (`gh auth login`).
For GitHub Enterprise: `--host github.yourcompany.com` (or set GH_HOST).
"""

import argparse
import json
import math
import os
import statistics
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

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

DEFAULTS = {
    "owner": "",                      # GitHub org or user handle that owns the repos
    "repos": [],                      # repo names under owner; empty = every repo of the owner
    "since": None,                    # YYYY-MM-DD, inclusive (default: 30 days ago)
    "until": None,                    # YYYY-MM-DD, inclusive (default: today)
    "host": None,                     # GitHub Enterprise host, e.g. github.td.com
    "tz": "America/Toronto",          # timezone used to decide what a weekend is
    "sla_hours": 24,                  # first-review SLA, in business hours
    "large_pr_lines": 200,            # additions+deletions at/above which a PR counts as large
    "reading_speed_lpm": 100,         # lines per minute above which an approval is implausible
    "min_lines_for_reading_check": 50,  # ignore tiny PRs in the reading-speed check
    "min_comment_chars": 12,          # shorter comments (e.g. "LGTM", "+1") are not substantive
    "bots": ["dependabot", "github-actions", "copilot", "renovate"],
    "exclude_authors": [],            # PR authors to drop entirely (e.g. automation accounts)
    "no_names": False,                # replace logins with reviewer-1, reviewer-2, ...
}

TRIVIAL_PHRASES = {"lgtm", "looks good", "looks good to me", "approved", "approve", "+1", "ok", "okay",
                   "ship it", "nice", "thanks", "thank you", "done", "good", "great"}


# ----------------------------------------------------------------------------- helpers

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
    return (100.0 * n / d) if d else 0.0


def fmt_pct(n, d):
    return f"{pct(n, d):.0f}% ({n}/{d})"


def fmt_h(h):
    return "n/a" if h is None else f"{h:.1f}h"


# ----------------------------------------------------------------------------- fetching

def gh_graphql(variables, host=None):
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
    """Return raw PR nodes merged in [since, until]; splits the window when the search cap is hit."""
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
                print(f"  window {since}..{until} has {count} PRs (> {SEARCH_CAP}); splitting", file=sys.stderr)
                return fetch_window(cfg, since, mid) + fetch_window(cfg, mid + timedelta(days=1), until)
            print(f"  {since}..{until}: {count} merged PRs, ~{math.ceil(count / PAGE_SIZE)} request(s)", file=sys.stderr)
        nodes += [n for n in search["nodes"] if n]  # non-PR issues come back as empty objects
        rl = data.get("rateLimit") or {}
        print(f"    fetched {len(nodes)} (rate limit remaining: {rl.get('remaining')})", file=sys.stderr)
        if not search["pageInfo"]["hasNextPage"]:
            return nodes
        after = search["pageInfo"]["endCursor"]


# ----------------------------------------------------------------------------- normalisation

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
        reviews.append({
            "reviewer": login, "state": r["state"], "at": parse_ts(r["submittedAt"]),
            "body": r.get("body") or "", "n_comments": (r.get("comments") or {}).get("totalCount", 0),
        })

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
        "requests": requests, "ready_at": ready_at,
        "review_baseline": min(req_times) if req_times else (ready_at or created),
        "reviews": reviews, "thread_comments": thread_comments,
    }


# ----------------------------------------------------------------------------- metrics

def metric_concentration(prs):
    approvals, participation, people = Counter(), Counter(), set()
    for pr in prs:
        people.add(pr["author"])
        seen_appr, seen_part = set(), set()
        for r in pr["reviews"]:
            seen_part.add(r["reviewer"])
            if r["state"] == "APPROVED":
                seen_appr.add(r["reviewer"])
        for c in pr["thread_comments"]:
            seen_part.add(c["reviewer"])
        approvals.update(seen_appr)
        participation.update(seen_part)
        people.update(seen_part)

    def shares(counter):
        total = sum(counter.values())
        ranked = counter.most_common()
        top1 = ranked[0][1] if ranked else 0
        top2 = top1 + (ranked[1][1] if len(ranked) > 1 else 0)
        return {"total": total, "top1_pct": pct(top1, total), "top2_pct": pct(top2, total),
                "by_reviewer": [{"reviewer": k, "count": v, "pct": pct(v, total)} for k, v in ranked]}

    return {"approvals": shares(approvals), "participation": shares(participation),
            "distinct_reviewers": len(participation), "people_seen": len(people)}


def metric_time_to_first_review(prs, cfg, tz):
    hours, no_review = [], []
    for pr in prs:
        events = [r["at"] for r in pr["reviews"]] + [c["at"] for c in pr["thread_comments"]]
        if not events:
            no_review.append(pr)
            continue
        h = business_hours(pr["review_baseline"], min(events), tz)
        hours.append({"pr": f"{pr['repo']}#{pr['number']}", "url": pr["url"], "hours": h})
    vals = [x["hours"] for x in hours]
    within = sum(1 for v in vals if v <= cfg["sla_hours"])
    return {"n": len(vals), "median_h": statistics.median(vals) if vals else None, "p90_h": percentile(vals, 90),
            "within_sla": within, "sla_hours": cfg["sla_hours"],
            "merged_without_review": [{"pr": f"{p['repo']}#{p['number']}", "url": p["url"]} for p in no_review],
            "per_pr": sorted(hours, key=lambda x: -x["hours"])}


def pr_has_substantive_feedback(pr, cfg):
    for r in pr["reviews"]:
        if r["state"] == "CHANGES_REQUESTED" or is_substantive_text(r["body"], cfg["min_comment_chars"]):
            return True
    return any(is_substantive_text(c["body"], cfg["min_comment_chars"]) for c in pr["thread_comments"])


def metric_substantive_rate(prs, cfg):
    def bucket(subset):
        n = len(subset)
        k = sum(1 for p in subset if pr_has_substantive_feedback(p, cfg))
        silent = sum(1 for p in subset if not p["thread_comments"] and not any(r["body"].strip() for r in p["reviews"]))
        return {"n": n, "with_substantive": k, "pct": pct(k, n), "no_comments_at_all": silent}
    large = [p for p in prs if p["lines"] >= cfg["large_pr_lines"]]
    return {"all": bucket(prs), "large": bucket(large), "large_pr_lines": cfg["large_pr_lines"]}


def metric_fast_approvals(prs, cfg):
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
                flagged.append({
                    "pr": f"{pr['repo']}#{pr['number']}", "url": pr["url"], "reviewer": r["reviewer"],
                    "lines": pr["lines"], "files": pr["files"], "minutes": round(minutes, 1), "lines_per_min": round(lpm),
                    "bare": (not r["body"].strip() and r["n_comments"] == 0 and r["reviewer"] not in commenters),
                })
    flagged.sort(key=lambda x: -x["lines_per_min"])
    return {"eligible_approvals": eligible, "flagged": flagged, "pct": pct(len(flagged), eligible),
            "reading_speed_lpm": cfg["reading_speed_lpm"], "min_lines": cfg["min_lines_for_reading_check"]}


# ----------------------------------------------------------------------------- anonymisation & report

def anonymise(results, prs):
    names = {}
    def alias(login):
        if login not in names:
            names[login] = f"reviewer-{len(names) + 1}"
        return names[login]
    for key in ("approvals", "participation"):
        for row in results["concentration"][key]["by_reviewer"]:
            row["reviewer"] = alias(row["reviewer"])
    for row in results["fast_approvals"]["flagged"]:
        row["reviewer"] = alias(row["reviewer"])
    return results


def render(results, cfg, since, until, n_prs):
    c, t, s, f = results["concentration"], results["time_to_first_review"], results["substantive"], results["fast_approvals"]
    out = []
    p = out.append
    p(f"# PR review health — {cfg['owner']} — {since} to {until}")
    p("")
    p(f"Merged PRs analysed: **{n_prs}** across {len(results['repos'])} repo(s); {results['excluded_prs']} bot/excluded-author PRs dropped. "
      "Bots and PR authors are excluded from reviewer stats.")
    p("")
    p("## 1. Reviewer concentration")
    p("")
    p(f"- **Approvals**: top-1 share **{c['approvals']['top1_pct']:.0f}%**, top-2 share **{c['approvals']['top2_pct']:.0f}%** "
      f"({c['approvals']['total']} approvals, one per reviewer per PR)")
    p(f"- **Review participation** (any review or comment): top-1 **{c['participation']['top1_pct']:.0f}%**, "
      f"top-2 **{c['participation']['top2_pct']:.0f}%**; {c['distinct_reviewers']} distinct reviewers out of {c['people_seen']} people seen")
    p("")
    p("| Reviewer | Approvals | Share | PRs reviewed | Share |")
    p("|---|---:|---:|---:|---:|")
    appr = {r["reviewer"]: r for r in c["approvals"]["by_reviewer"]}
    for row in c["participation"]["by_reviewer"]:
        a = appr.get(row["reviewer"], {"count": 0, "pct": 0.0})
        p(f"| {row['reviewer']} | {a['count']} | {a['pct']:.0f}% | {row['count']} | {row['pct']:.0f}% |")
    p("")
    p("## 2. Time to first review (business hours, weekends excluded)")
    p("")
    p(f"- Median **{fmt_h(t['median_h'])}**, p90 **{fmt_h(t['p90_h'])}**, within {t['sla_hours']}h SLA: **{fmt_pct(t['within_sla'], t['n'])}**")
    p(f"- Merged without any review: **{len(t['merged_without_review'])}**"
      + (" — " + ", ".join(x["pr"] for x in t["merged_without_review"][:10]) if t["merged_without_review"] else ""))
    slow = [x for x in t["per_pr"] if x["hours"] > t["sla_hours"]][:10]
    if slow:
        p("- Slowest: " + ", ".join(f"{x['pr']} ({x['hours']:.0f}h)" for x in slow))
    p("")
    p("## 3. Substantive-comment rate")
    p("")
    p(f"- All PRs: **{fmt_pct(s['all']['with_substantive'], s['all']['n'])}** received a non-nit comment or a change request; "
      f"{s['all']['no_comments_at_all']} had no comments at all")
    p(f"- Large PRs (≥ {s['large_pr_lines']} lines): **{fmt_pct(s['large']['with_substantive'], s['large']['n'])}**; "
      f"{s['large']['no_comments_at_all']} had no comments at all")
    p("")
    p("## 4. Faster-than-reading approvals")
    p("")
    p(f"- Approvals on PRs ≥ {f['min_lines']} lines: {f['eligible_approvals']}; flagged above {f['reading_speed_lpm']} lines/min: "
      f"**{fmt_pct(len(f['flagged']), f['eligible_approvals'])}**")
    if f["flagged"]:
        p("")
        p("| PR | Reviewer | Lines | Minutes since request | Lines/min | Bare approval |")
        p("|---|---|---:|---:|---:|:---:|")
        for row in f["flagged"][:30]:
            p(f"| {row['pr']} | {row['reviewer']} | {row['lines']} | {row['minutes']} | {row['lines_per_min']} | {'yes' if row['bare'] else ''} |")
        if len(f["flagged"]) > 30:
            p(f"| … {len(f['flagged']) - 30} more in JSON output | | | | | |")
    p("")
    p("_Definitions: first review = first submitted review or inline comment by a non-author human, measured from the first "
      "review request (or ready-for-review / PR creation if never requested). Substantive = not prefixed `nit`, not a "
      f"trivial phrase, ≥ {cfg['min_comment_chars']} chars, or an explicit change request. Reading speed uses the request "
      "aimed at that reviewer when one exists._")
    return "\n".join(out)


# ----------------------------------------------------------------------------- main

def load_config(args):
    cfg = dict(DEFAULTS)
    path = args.config or ("config.json" if os.path.exists("config.json") else None)
    if path:
        with open(path) as fh:
            cfg.update({k: v for k, v in json.load(fh).items() if k in DEFAULTS})
    for key in DEFAULTS:
        val = getattr(args, key, None)
        if val is not None and val is not False:
            cfg[key] = val
    if isinstance(cfg["repos"], str):
        cfg["repos"] = [r.strip() for r in cfg["repos"].split(",") if r.strip()]
    if isinstance(cfg["bots"], str):
        cfg["bots"] = [b.strip().lower() for b in cfg["bots"].split(",") if b.strip()]
    if isinstance(cfg["exclude_authors"], str):
        cfg["exclude_authors"] = [a.strip() for a in cfg["exclude_authors"].split(",") if a.strip()]
    if not cfg["owner"]:
        sys.exit("error: --owner (GitHub org or user handle) is required, via CLI or config.json")
    return cfg


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="JSON config file (default: ./config.json if present)")
    ap.add_argument("--owner", help="GitHub org or user handle owning the repos")
    ap.add_argument("--repos", help="comma-separated repo names; omit to scan every repo of the owner")
    ap.add_argument("--since", help="YYYY-MM-DD inclusive (default: 30 days ago)")
    ap.add_argument("--until", help="YYYY-MM-DD inclusive (default: today)")
    ap.add_argument("--host", help="GitHub Enterprise host (default: github.com or $GH_HOST)")
    ap.add_argument("--tz", help="timezone for weekend detection (default America/Toronto)")
    ap.add_argument("--sla-hours", dest="sla_hours", type=float)
    ap.add_argument("--large-pr-lines", dest="large_pr_lines", type=int)
    ap.add_argument("--reading-speed", dest="reading_speed_lpm", type=float, help="lines/min threshold")
    ap.add_argument("--min-lines", dest="min_lines_for_reading_check", type=int)
    ap.add_argument("--min-comment-chars", dest="min_comment_chars", type=int)
    ap.add_argument("--bots", help="comma-separated login substrings treated as bots")
    ap.add_argument("--exclude-authors", dest="exclude_authors", help="comma-separated PR authors to drop")
    ap.add_argument("--no-names", dest="no_names", action="store_true", help="anonymise reviewer logins")
    ap.add_argument("--json", dest="json_out", help="also write full results to this JSON file")
    ap.add_argument("--markdown", dest="md_out", help="also write the report to this Markdown file")
    args = ap.parse_args()

    cfg = load_config(args)
    until = date.fromisoformat(cfg["until"]) if cfg["until"] else date.today()
    since = date.fromisoformat(cfg["since"]) if cfg["since"] else until - timedelta(days=30)
    tz = load_tz(cfg["tz"])

    print(f"Fetching merged PRs for {cfg['owner']} ({', '.join(cfg['repos']) or 'all repos'}) {since}..{until}", file=sys.stderr)
    raw = fetch_window(cfg, since, until)
    prs = [normalise(n, cfg) for n in raw]
    prs = [p for p in prs if p["author"] not in cfg["exclude_authors"] and not is_bot(p["author"], cfg["bots"])]
    excluded = len(raw) - len(prs)
    if not prs:
        sys.exit("No merged PRs found in that window.")

    results = {
        "owner": cfg["owner"], "since": since.isoformat(), "until": until.isoformat(),
        "repos": sorted({p["repo"] for p in prs}), "pr_count": len(prs), "excluded_prs": excluded, "config": cfg,
        "concentration": metric_concentration(prs),
        "time_to_first_review": metric_time_to_first_review(prs, cfg, tz),
        "substantive": metric_substantive_rate(prs, cfg),
        "fast_approvals": metric_fast_approvals(prs, cfg),
    }
    if cfg["no_names"]:
        results = anonymise(results, prs)

    report = render(results, cfg, since, until, len(prs))
    print(report)
    if args.md_out:
        with open(args.md_out, "w") as fh:
            fh.write(report + "\n")
    if args.json_out:
        with open(args.json_out, "w") as fh:
            json.dump(results, fh, indent=2, default=str)
        print(f"\nJSON written to {args.json_out}", file=sys.stderr)


if __name__ == "__main__":
    main()
