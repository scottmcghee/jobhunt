"""Find job postings that fit a candidate profile, and draft cover letters for the best ones.

Pulls open roles from the company job boards in config/companies.yaml (Greenhouse, Lever,
Ashby, Workday, SmartRecruiters), drops those that fail the hard filters in
config/preferences.yaml, has Claude score the rest 1-10 against config/profile.md, and assembles
cover letters for the top scorers from the pre-written modules in config/kit/.

    jobhunt fetch   [--company NAME] [--dry-run] [--workers N] [--per-host N]
                                                     pull postings, filter, record new ones
    jobhunt score   [--limit N] [--rescore]          score unscored jobs with Claude
    jobhunt list    [--min-score N]                  show scored jobs and their keys
    jobhunt letter  [--min-score N] [--job KEY] [--force]
                                                     letters for high scorers that have none yet
    jobhunt run     [--workers N] [--per-host N]     fetch -> score -> letter

A job key is source:company_slug:external_id, e.g. greenhouse:huntress:7777533003.
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import Executor, ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx

from jobhunt import config, settings, storage, throttle
from jobhunt import filter as jfilter
from jobhunt.generate import generate_letter
from jobhunt.llm import Completer, backend_name, make_completer, model_name
from jobhunt.runner import BoardRunner
from jobhunt.schema import Company, Job
from jobhunt.score import score_job
from jobhunt.sources import fetch_company, rate_group

log = logging.getLogger("jobhunt")


def _transport(
    fetch: settings.FetchSettings, workers: int, per_host: int
) -> throttle.ThrottledTransport:
    # Polite by construction: per-group concurrency limits, and retries on 429 (see throttle.py).
    # `workers` caps requests in flight across all groups and sizes the connection pool to match.
    pool = httpx.Limits(max_connections=workers, max_keepalive_connections=workers)
    return throttle.ThrottledTransport(
        start=fetch.start_per_host,
        ceiling=per_host,
        max_in_flight=workers,
        limits=pool,
        max_retries=fetch.max_retries,
        max_retry_after=fetch.max_retry_after,
        cooldown=fetch.cooldown,
        transient_retries=fetch.transient_retries,
    )


def _client(transport: httpx.BaseTransport, fetch: settings.FetchSettings) -> httpx.Client:
    return httpx.Client(
        transport=transport,
        timeout=fetch.timeout,
        headers={"User-Agent": fetch.user_agent},
        follow_redirects=True,
    )


def _print_stats(transport: throttle.ThrottledTransport) -> None:
    print("requests by host:", file=sys.stderr)
    for group, st in transport.stats().items():
        print(
            f"  {group:<18} {st['requests']:>5} requests, {st['throttles']:>3} throttled, "
            f"peak {st['max_in_flight']} in flight, limit now {st['limit']:.1f}",
            file=sys.stderr,
        )


# --------------------------------------------------------------------------- commands


# A board found gone (see _board_gone) this many fetches in a row is removed from companies.yaml.
MAX_CONSECUTIVE_404S = 3


@dataclass(frozen=True)
class BoardOutcome:
    """What fetching one board produced: its jobs, or None plus the HTTP status if it failed."""

    company: Company
    jobs: list[Job] | None = None
    status: int | None = None
    skipped: bool = False  # not fetched: its group's circuit breaker had tripped
    refused: bool = False  # the host pushed back (429, or a 403 that isn't about this board)
    gone: bool = False  # the board itself no longer exists (see _board_gone)


def _host_refused(response: httpx.Response) -> bool:
    """429 always; 403 unless it is a board-level error, as Workday sends for a closed site."""
    if response.status_code == 429:
        return True
    if response.status_code != 403:
        return False
    try:
        body = response.json()
    except ValueError:  # an HTML block page, say
        return True
    return not (isinstance(body, dict) and "errorCode" in body)


def _board_gone(company: Company, response: httpx.Response) -> bool:
    """A 404; for Workday also a 422 (the site was removed) or a 403 "S22" (the site is closed)."""
    if response.status_code == 404:
        return True
    if company.ats != "workday" or response.status_code not in (403, 422):
        return False
    try:
        body = response.json()
    except ValueError:
        return False
    code = body.get("errorCode") if isinstance(body, dict) else None
    return code == "HTTP_422" if response.status_code == 422 else code == "S22"


def _label(company: Company) -> str:
    """The company's name, plus its slug if that differs, so a tenant's sites can be told apart."""
    if company.slug.lower() == company.name.lower():
        return company.name
    return f"{company.name} ({company.slug})"


def _one_line(error: BaseException) -> str:
    return " ".join(str(error).split())


class _GroupPools:
    """One thread pool per rate group, for boards' later pages and descriptions.

    A board's pooled work only queues behind its own host's, as with the runner's board queues.
    Its threads only send requests, never wait on a pool, so they can't deadlock.
    """

    def __init__(self, size: int):
        self._size = size
        self._pools: dict[str, ThreadPoolExecutor] = {}
        self._lock = threading.Lock()
        self._closed = False

    def get(self, group: str) -> ThreadPoolExecutor:
        with self._lock:
            if group not in self._pools:
                pool = ThreadPoolExecutor(self._size, thread_name_prefix=f"fetch-{group}")
                if self._closed:  # a board starting after the end gets a pool that refuses work
                    pool.shutdown()
                self._pools[group] = pool
            return self._pools[group]

    def shutdown(self) -> None:
        with self._lock:
            self._closed = True
            pools = list(self._pools.values())
        for pool in pools:
            pool.shutdown(wait=False, cancel_futures=True)


@contextmanager
def _stop_on_exit(transport: throttle.ThrottledTransport, pools: _GroupPools) -> Iterator[None]:
    """On the way out (Ctrl-C included), send nothing more and cancel queued page requests.

    Stopping the transport first wakes pool threads waiting for a slot or out a 429 pause, so
    the process can exit once the requests already sent are done.
    """
    try:
        yield
    finally:
        transport.stop()
        pools.shutdown()


def _refused(outcome: BoardOutcome) -> bool:
    """A host pushing back (rate limit or block), which feeds the runner's circuit breaker."""
    return outcome.refused


def _fetch_board(
    company: Company,
    client: httpx.Client,
    wants_body: Callable[[Job], bool],
    verbose: bool,
    stopping: Callable[[], bool] = lambda: False,
    pool: Executor | None = None,
) -> BoardOutcome:
    """Fetch one board. Touches no shared state, and a failing board never stops the run.

    Once the run is ``stopping`` (interrupted), errors go unreported: the client is being closed
    under boards still in flight, and the runner discards their outcomes anyway.
    """
    try:
        jobs = fetch_company(company, client, wants_body=wants_body, pool=pool)
        return BoardOutcome(company, jobs=jobs)
    except httpx.HTTPStatusError as e:
        status = e.response.status_code
        if not stopping():
            log.warning("%s: HTTP %s — check slug/ATS", _label(company), status)
        return BoardOutcome(
            company,
            status=status,
            refused=_host_refused(e.response),
            gone=_board_gone(company, e.response),
        )
    except httpx.HTTPError as e:
        if not stopping():
            log.warning("%s: %s", _label(company), _one_line(e))
    except Exception as e:  # malformed data from one board; the rest of the run still counts
        if not stopping():
            kind, message = type(e).__name__, _one_line(e)
            log.warning("%s: skipped, %s: %s", _label(company), kind, message, exc_info=verbose)
    return BoardOutcome(company)


def _record(
    outcome: BoardOutcome,
    prefs: config.Preferences,
    seen: storage.SeenSet,
    misses: storage.MissLedger,
    dead: set[str],
    new_jobs: list[Job],
    verbose: bool,
    prune_after: int = MAX_CONSECUTIVE_404S,
) -> None:
    """Fold one board's outcome into the run's books and print its line. Main thread only."""
    company = outcome.company
    if outcome.skipped:
        return
    if outcome.jobs is None:
        if outcome.gone and misses.miss(company.key) >= prune_after:
            dead.add(company.key)
        return
    misses.clear(company.key)
    passed, rejected = jfilter.apply(outcome.jobs, prefs)
    fresh = [j for j in passed if j.key not in seen]
    counts = f"total={len(outcome.jobs):<4} passed={len(passed):<3} new={len(fresh)}"
    print(f"{company.name:<16} {counts}")
    if verbose:
        for r in rejected:
            print(f"    - {r.job.title[:60]:<60} {r.reason}")
    new_jobs.extend(fresh)


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
    fetch = args.settings.fetch
    prune = fetch.prune_after_404s
    transport = _transport(fetch, workers=args.workers, per_host=args.per_host)
    # Later pages and descriptions (Workday, SmartRecruiters) go to their group's pool; the
    # transport's per-host and global limits still decide how many are in flight.
    pools = _GroupPools(args.per_host)
    with _client(transport, fetch) as client, _stop_on_exit(transport, pools):
        boards = BoardRunner(
            companies,
            lambda company: _fetch_board(
                company,
                client,
                title_passes,
                args.verbose,
                lambda: boards.stopping,
                pools.get(rate_group(company)),
            ),
            group_of=rate_group,
            per_group=args.per_host,
            refused=_refused,
            skip=lambda company: BoardOutcome(company, skipped=True),
            breaker=fetch.breaker,
        )
        try:
            for outcome in boards:  # in companies.yaml order, whatever order they finish in
                _record(outcome, prefs, seen, misses, dead, new_jobs, args.verbose, prune)
        except KeyboardInterrupt:
            # Keep what the finished boards found; the next run picks up the rest.
            interrupted = True
            print("\ninterrupted: keeping the boards fetched so far", file=sys.stderr)
            late = boards.finished_out_of_order()
            if any(o.jobs is not None and not o.skipped for o in late):  # only if a line follows
                print("finished out of order:")
            for outcome in late:
                _record(outcome, prefs, seen, misses, dead, new_jobs, args.verbose, prune)
    if args.verbose:
        _print_stats(transport)

    if args.dry_run:
        for j in new_jobs:
            print(f"  + {j.company}: {j.title} ({j.location}) {j.url}")
        return 130 if interrupted else 0

    for company in config.remove_companies(args.companies, dead) if dead else []:
        where = args.companies.name
        print(f"removed {_label(company)} from {where}: gone {prune} fetches in a row")
    for key in dead:
        misses.clear(key)
    misses.save()

    for j in new_jobs:
        storage.append_jsonl(data_dir / "jobs.jsonl", j.model_dump())
        seen.add(j.key)
    seen.save()
    print(f"\n{len(new_jobs)} new job(s) recorded.")
    return 130 if interrupted else 0


def _completer(llm_settings: settings.LLMSettings | None = None) -> Completer:
    return make_completer(llm_settings)


def _model_label(llm_settings: settings.LLMSettings) -> str:
    """backend:model, worked out once per run and recorded with every score and letter."""
    return f"{backend_name(llm_settings)}:{model_name(llm_settings)}"


def cmd_score(args: argparse.Namespace, data_dir: Path, complete: Completer | None = None) -> int:
    jobs = storage.load_jobs(data_dir)
    scored_keys = {s.job.key for s in storage.load_scores(data_dir)}
    todo = [j for j in jobs if args.rescore or j.key not in scored_keys]
    if args.limit:
        todo = todo[: args.limit]
    if not todo:
        print("nothing to score.")
        return 0

    llm = args.settings.llm
    complete = complete or _completer(llm)
    label = _model_label(llm)
    profile = config.load_profile()
    kit = config.load_kit()
    for j in todo:
        try:
            sj = score_job(j, profile, kit, complete, llm.score_max_tokens, llm.body_chars, label)
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

    llm = args.settings.llm
    complete = complete or _completer(llm)
    label = _model_label(llm)
    profile = config.load_profile()
    kit = config.load_kit()
    skipped = 0
    for s in targets:
        try:
            letter = generate_letter(
                s, profile, kit, complete, llm.letter_max_tokens, llm.body_chars, label
            )
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


def _at_least_one(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a whole number: {value!r}") from None
    if number < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, not {number}")
    return number


def _concurrency_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--workers", type=_at_least_one, metavar="N",
                   help="most requests in flight across all boards (default: fetch.workers in "
                        "settings.yaml, 32)")
    p.add_argument("--per-host", type=_at_least_one, metavar="N",
                   help="most in flight per Workday datacenter or API host; it adapts below this "
                        "when a host answers 429 (default: fetch.per_host in settings.yaml, 6). "
                        "--workers 1 --per-host 1 is the "
                        "gentlest setting")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="jobhunt", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-v", "--verbose", action="store_true", help="log progress and filter reasons")
    p.add_argument("--data-dir", type=Path,
                   help="runtime state: seen.json, jobs.jsonl, scores.jsonl (default: "
                        "paths.data_dir in settings.yaml, else data/)")
    p.add_argument("--output-dir", type=Path,
                   help="where letters are written (default: paths.output_dir in settings.yaml, "
                        "else output/)")
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
    _concurrency_args(f)

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
    _concurrency_args(r)
    r.add_argument("--limit", type=int, metavar="N", help=limit_help)
    r.add_argument("--rescore", action="store_true", help=rescore_help)
    r.add_argument("--min-score", type=int, metavar="N", help=min_score_help)
    r.add_argument("--job", metavar="KEY", help=job_help)
    r.add_argument("--force", action="store_true", help=force_help)
    return p


def _resolve(args: argparse.Namespace, s: settings.Settings) -> argparse.Namespace:
    """Fill what no flag set from settings (file, then env), else the built-in defaults."""
    args.settings = s
    args.data_dir = args.data_dir or s.paths.data_dir or storage.DEFAULT_DATA_DIR
    args.output_dir = args.output_dir or s.paths.output_dir or storage.DEFAULT_OUTPUT_DIR
    if hasattr(args, "workers"):  # fetch and run
        args.workers = args.workers or s.fetch.workers
        args.per_host = args.per_host or s.fetch.per_host
    return args


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        args = _resolve(args, settings.load())
    except settings.SettingsError as e:
        print(e, file=sys.stderr)
        return 2
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
