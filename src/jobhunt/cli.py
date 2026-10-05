"""Find job postings that fit a candidate profile, and draft cover letters for the best ones.

Pulls open roles from the company job boards in config/companies.yaml (Greenhouse, Lever,
Ashby, Workday, SmartRecruiters), drops those that fail the hard filters in
config/preferences.yaml, has Claude score the rest 1-10 against config/profile.md, and assembles
cover letters for the top scorers from the pre-written modules in config/kit/.

    jobhunt fetch   [--company NAME] [--dry-run]     pull postings, filter, record new ones
    jobhunt score   [--limit N] [--rescore]          score unscored jobs with Claude
    jobhunt list    [--min-score N]                  show scored jobs and their keys
    jobhunt letter  [--min-score N] [--job KEY] [--force]
                                                     letters for high scorers that have none yet
    jobhunt run                                      fetch -> score -> letter

A job key is source:company_slug:external_id, e.g. greenhouse:huntress:7777533003.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Callable
from pathlib import Path

import httpx

from jobhunt import config, storage
from jobhunt import filter as jfilter
from jobhunt.generate import generate_letter
from jobhunt.llm import Completer, make_completer
from jobhunt.schema import Company, Job
from jobhunt.score import score_job
from jobhunt.sources import fetch_company

log = logging.getLogger("jobhunt")


def _client() -> httpx.Client:
    return httpx.Client(
        timeout=20.0,
        headers={"User-Agent": "jobhunt/0.1 (+personal job search tool)"},
        follow_redirects=True,
    )


# --------------------------------------------------------------------------- commands


# A board that 404s this many fetches in a row is removed from companies.yaml.
MAX_CONSECUTIVE_404S = 3


def _fetch_board(
    company: Company,
    client: httpx.Client,
    wants_body: Callable[[Job], bool],
    misses: storage.MissLedger,
    dead: set[str],
    verbose: bool,
) -> list[Job] | None:
    """One board's postings, or None if it failed. A failing board never stops the run."""
    try:
        return fetch_company(company, client, wants_body=wants_body)
    except httpx.HTTPStatusError as e:
        status = e.response.status_code
        log.warning("%s: HTTP %s — check slug/ATS", company.name, status)
        if status == 404 and misses.miss(company.key) >= MAX_CONSECUTIVE_404S:
            dead.add(company.key)
    except httpx.HTTPError as e:
        log.warning("%s: %s", company.name, e)
    except Exception as e:  # malformed data from one board; the rest of the run still counts
        log.warning("%s: skipped, %s: %s", company.name, type(e).__name__, e, exc_info=verbose)
    return None


def cmd_fetch(args: argparse.Namespace, data_dir: Path) -> int:
    companies = config.load_companies(args.companies)
    if args.company:
        companies = [c for c in companies if c.name.lower() == args.company.lower()]
        if not companies:
            print(f"no company named {args.company!r} in companies.yaml", file=sys.stderr)
            return 2
    prefs = config.load_preferences()
    seen = storage.SeenSet(data_dir / "seen.json")
    misses = storage.MissLedger(data_dir / "misses.json")

    def title_passes(job: Job) -> bool:
        return jfilter.check_title(job, prefs) is None

    new_jobs: list[Job] = []
    dead: set[str] = set()
    interrupted = False
    with _client() as client:
        try:
            for company in companies:
                jobs = _fetch_board(company, client, title_passes, misses, dead, args.verbose)
                if jobs is None:
                    continue
                misses.clear(company.key)
                passed, rejected = jfilter.apply(jobs, prefs)
                fresh = [j for j in passed if j.key not in seen]
                counts = f"total={len(jobs):<4} passed={len(passed):<3} new={len(fresh)}"
                print(f"{company.name:<16} {counts}")
                if args.verbose:
                    for r in rejected:
                        print(f"    - {r.job.title[:60]:<60} {r.reason}")
                new_jobs.extend(fresh)
        except KeyboardInterrupt:
            # Keep what the finished boards found; the next run picks up the rest.
            interrupted = True
            print("\ninterrupted: keeping the boards fetched so far", file=sys.stderr)

    if args.dry_run:
        for j in new_jobs:
            print(f"  + {j.company}: {j.title} ({j.location}) {j.url}")
        return 130 if interrupted else 0

    for name in config.remove_companies(args.companies, dead) if dead else []:
        print(f"removed {name} from {args.companies.name}: {MAX_CONSECUTIVE_404S} 404s in a row")
    for key in dead:
        misses.clear(key)
    misses.save()

    for j in new_jobs:
        storage.append_jsonl(data_dir / "jobs.jsonl", j.model_dump())
        seen.add(j.key)
    seen.save()
    print(f"\n{len(new_jobs)} new job(s) recorded.")
    return 130 if interrupted else 0


def _completer() -> Completer:
    return make_completer()


def cmd_score(args: argparse.Namespace, data_dir: Path, complete: Completer | None = None) -> int:
    jobs = storage.load_jobs(data_dir)
    scored_keys = {s.job.key for s in storage.load_scores(data_dir)}
    todo = [j for j in jobs if args.rescore or j.key not in scored_keys]
    if args.limit:
        todo = todo[: args.limit]
    if not todo:
        print("nothing to score.")
        return 0

    complete = complete or _completer()
    profile = config.load_profile()
    kit = config.load_kit()
    for j in todo:
        try:
            sj = score_job(j, profile, kit, complete)
        except (ValueError, KeyError) as e:  # unusable model reply; stays unscored for next run
            log.warning("%s: %s — skipped (%s: %s)", j.key, j.title, type(e).__name__, e)
            continue
        storage.append_jsonl(data_dir / "scores.jsonl", sj.model_dump())
        print(f"{sj.score.score:>2}/10  {j.company}: {j.title}")
        print(f"       {sj.score.rationale}")
    return 0


def cmd_letter(
    args: argparse.Namespace,
    data_dir: Path,
    output_dir: Path,
    complete: Completer | None = None,
) -> int:
    prefs = config.load_preferences()
    threshold = args.min_score or prefs.scoring.min_score_for_letter
    scored = storage.load_scores(data_dir)
    # Keep the latest score per job if rescored.
    latest = {s.job.key: s for s in scored}
    targets = [s for s in latest.values() if s.score.score >= threshold]
    if args.job:
        targets = [s for s in latest.values() if s.job.key == args.job]
    if not targets:
        print(f"no jobs at or above {threshold}/10.")
        return 0
    if not args.force:
        done = storage.lettered_job_keys(output_dir)
        if had := sum(s.job.key in done for s in targets):
            print(f"{had} job(s) already have a letter; --force regenerates them.")
        targets = [s for s in targets if s.job.key not in done]
        if not targets:
            return 0

    complete = complete or _completer()
    profile = config.load_profile()
    kit = config.load_kit()
    skipped = 0
    for s in targets:
        try:
            letter = generate_letter(s, profile, kit, complete)
        except (ValueError, KeyError) as e:  # unusable model reply; the next run retries it
            log.warning("%s: %s — skipped (%s: %s)", s.job.key, s.job.title, type(e).__name__, e)
            skipped += 1
            continue
        path = storage.write_letter(letter, output_dir)
        print(f"wrote {path}  (modules: {', '.join(letter.modules_used)})")
    if skipped:
        print(f"{skipped} letter(s) skipped; see the warnings above.")
    return 0


def cmd_list(args: argparse.Namespace, data_dir: Path) -> int:
    scored = storage.load_scores(data_dir)
    latest = {s.job.key: s for s in scored}
    rows = sorted(latest.values(), key=lambda s: -s.score.score)
    if args.min_score:
        rows = [s for s in rows if s.score.score >= args.min_score]
    for s in rows:
        print(f"{s.score.score:>2}/10  {s.job.company:<14} {s.job.title[:55]:<55} {s.job.key}")
        print(f"        {s.job.url}")
    print(f"\n{len(rows)} scored job(s).")
    return 0


def cmd_run(args: argparse.Namespace, data_dir: Path, output_dir: Path) -> int:
    rc = cmd_fetch(args, data_dir)
    if rc:
        return rc
    rc = cmd_score(args, data_dir)
    if rc:
        return rc
    return cmd_letter(args, data_dir, output_dir)


# --------------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="jobhunt", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-v", "--verbose", action="store_true", help="log progress and filter reasons")
    p.add_argument("--data-dir", type=Path, default=storage.DEFAULT_DATA_DIR,
                   help="runtime state: seen.json, jobs.jsonl, scores.jsonl (default: data/)")
    p.add_argument("--output-dir", type=Path, default=storage.DEFAULT_OUTPUT_DIR,
                   help="where letters are written (default: output/)")
    p.add_argument("--companies", type=Path, default=config.DEFAULT_CONFIG_DIR / "companies.yaml",
                   help="boards to fetch (default: config/companies.yaml)")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="COMMAND")

    company_help = ("fetch only this company, matched case-insensitively against its name: "
                    "field in companies.yaml (not the slug)")
    dry_run_help = "show what would be recorded without recording it"
    limit_help = "score at most N jobs"
    rescore_help = "score jobs again even if they already have a score"
    force_help = "write letters again for jobs that already have one in the output directory"
    min_score_help = "letter threshold (default: scoring.min_score_for_letter in preferences.yaml)"
    job_help = ("write a letter for one scored job, whatever its score. A job key is "
                "source:company_slug:external_id, e.g. greenhouse:huntress:7777533003; "
                "`jobhunt list` shows each scored job's key")

    f = sub.add_parser("fetch", help="pull postings, filter, record new ones")
    f.add_argument("--company", metavar="NAME", help=company_help)
    f.add_argument("--dry-run", action="store_true", help=dry_run_help)

    s = sub.add_parser("score", help="score unscored jobs with Claude")
    s.add_argument("--limit", type=int, metavar="N", help=limit_help)
    s.add_argument("--rescore", action="store_true", help=rescore_help)

    li = sub.add_parser("list", help="show scored jobs and their keys")
    li.add_argument("--min-score", type=int, metavar="N", help="hide jobs scoring below N")

    le = sub.add_parser("letter", help="generate letters for high scorers")
    le.add_argument("--min-score", type=int, metavar="N", help=min_score_help)
    le.add_argument("--job", metavar="KEY", help=job_help)
    le.add_argument("--force", action="store_true", help=force_help)

    r = sub.add_parser("run", help="fetch -> score -> letter")
    r.add_argument("--company", metavar="NAME", help=company_help)
    r.add_argument("--dry-run", action="store_true", help=dry_run_help)
    r.add_argument("--limit", type=int, metavar="N", help=limit_help)
    r.add_argument("--rescore", action="store_true", help=rescore_help)
    r.add_argument("--min-score", type=int, metavar="N", help=min_score_help)
    r.add_argument("--job", metavar="KEY", help=job_help)
    r.add_argument("--force", action="store_true", help=force_help)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        if args.cmd == "fetch":
            return cmd_fetch(args, args.data_dir)
        if args.cmd == "score":
            return cmd_score(args, args.data_dir)
        if args.cmd == "letter":
            return cmd_letter(args, args.data_dir, args.output_dir)
        if args.cmd == "list":
            return cmd_list(args, args.data_dir)
        if args.cmd == "run":
            return cmd_run(args, args.data_dir, args.output_dir)
    except config.ConfigMissing as e:
        print(e, file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
