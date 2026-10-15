#!/usr/bin/env python3
"""Safely delete only same-repository, merged-PR branches still at their PR head.

Designed for GitHub Actions with a short-lived GITHUB_TOKEN with contents:write.
Dry-run is the default; --execute is required. No tokens appear in output.
Never delete main, protected, foreign, unmerged, reopened or advanced branches.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from urllib.parse import quote

ELIGIBLE_NAME = re.compile(r"(?:p[0-9]+|fix|feature|feat|docs|chore|test)/[a-zA-Z0-9._/-]+\Z")


def api(route: str, *, delete: bool = False):
    command = ["gh", "api", f"repos/{os.environ['GITHUB_REPOSITORY']}/{route}"]
    if delete:
        command += ["--method", "DELETE"]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode:
        # Do not echo token, GitHub JSON body, or sensitive endpoint values.
        raise RuntimeError(f"GitHub API {'DELETE' if delete else 'GET'} failed for {route}")
    return None if delete else json.loads(result.stdout)


def pages(resource: str, *, params: str = ""):
    for page in range(1, 101):
        result = api(f"{resource}?per_page=100&page={page}{('&' + params) if params else ''}")
        if not isinstance(result, list):
            raise RuntimeError(f"Unexpected GitHub response type for {resource}")
        yield from result
        if len(result) < 100:
            return
    raise RuntimeError("Too many GitHub pages, refusing incomplete inspection")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Actually delete verified merged refs")
    args = parser.parse_args()
    if os.environ.get("GITHUB_REPOSITORY") != "babico/TeleDrive":
        raise RuntimeError("Repository identity mismatch; explicit review required")
    if not os.environ.get("GH_TOKEN"):
        raise RuntimeError("GitHub Actions token not configured")
    closed = list(pages("pulls", params="state=closed&sort=updated&direction=desc"))
    merged = [pr for pr in closed if pr.get("merged_at") and
              pr.get("base", {}).get("ref") == "main" and
              (pr.get("head", {}).get("repo") or {}).get("full_name") ==
              os.environ["GITHUB_REPOSITORY"]]
    open_prs = list(pages("pulls", params="state=open"))
    active = {pr.get("head", {}).get("ref") for pr in open_prs
              if (pr.get("head", {}).get("repo") or {}).get("full_name") ==
              os.environ["GITHUB_REPOSITORY"]}
    current = list(pages("branches"))
    eligible = []
    for candidate in current:
        name, sha = candidate.get("name", ""), candidate.get("commit", {}).get("sha")
        if (not ELIGIBLE_NAME.fullmatch(name) or name == "main" or
                candidate.get("protected", True) or name in active or not sha):
            continue
        if not any(pr["head"]["ref"] == name and pr["head"]["sha"] == sha
                   for pr in merged):
            continue
        eligible.append((name, sha))
    for name, sha in sorted(eligible):
        # Double-check the branch has not moved between initial listing and
        # deletion. (No CAS is exposed by REST DELETE; use a dedicated
        # maintenance window for the final deletion.)
        ref = api("git/ref/heads/" + quote(name, safe="/"))
        if ref.get("object", {}).get("sha") != sha:
            print(f"SKIP: head advanced for {name}")
            continue
        if args.execute:
            api("git/refs/heads/" + quote(name, safe="/"), delete=True)
            print(f"DELETED merged branch: {name}")
        else:
            print(f"DRY RUN eligible merged branch: {name}")
    print(f"Inspected {len(current)} refs, {len(merged)} merged main PRs; "
          f"eligible={len(eligible)}, execute={args.execute}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"BRANCH_CLEANUP_ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
