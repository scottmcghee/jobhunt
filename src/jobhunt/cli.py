"""Find job postings that fit a candidate profile, and draft cover letters for the best ones.

Pulls open roles from the company job boards in config/companies.yaml (Greenhouse, Lever,
Ashby, Workday, SmartRecruiters, Workable, BambooHR), drops those that fail the hard filters in
config/preferences.yaml, has Claude score the rest 1-10 against config/profile.md, and assembles
cover letters for the top scorers from the pre-written modules in config/kit/.

    jobhunt fetch   [--company NAME] [--dry-run] [--resume] [--workers N] [--per-host N]
                                                     pull postings, filter, record new ones
    jobhunt score   [--limit N] [--rescore]          score unscored jobs with Claude
    jobhunt list    [--min-score N] [--hide-applied] show scored jobs and their keys
    jobhunt letter  [--min-score N] [--job KEY] [--force]
                                                     letters for high scorers that have none yet
    jobhunt run     [--workers N] [--per-host N]     fetch -> score -> letter
    jobhunt applied KEY --resume V [--warm] [--date D] [--force]
                                                     record an application to a found job
    jobhunt outcome KEY STATUS [--date D]            record what came of it
    jobhunt applications                             each application, and response rates
    jobhunt label   [--sample N]                     score a sample of jobs yourself
    jobhunt eval    [--rescore]                      how well the scorer agrees with you

A job key is source:company_slug:external_id, e.g. greenhouse:huntress:7777533003.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import Executor, ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

import httpx

from jobhunt import applications as apps
from jobhunt import config, evaluate, settings, storage, throttle
from jobhunt import filter as jfilter
from jobhunt.generate import generate_letter
from jobhunt.llm import Completer, backend_name, make_completer, model_name
from jobhunt.runner import BoardRunner
from jobhunt.schema import Company, Job, ScoredJob
from jobhunt.score import SYSTEM as SCORE_SYSTEM
from jobhunt.score import build_user_prompt, score_job
from jobhunt.sources import fetch_company, rate_group

log = logging.getLogger("jobhunt")


def _transport(
    fetch: settings.FetchSettings, workers: int, per_host: int
) -> throttle.ThrottledTransport:
    # Polite by construction: per-group concurrency limits and rate caps, and retries on 429.
    return throttle.from_settings(fetch, workers=workers, per_host=per_host)


def _client(transport: httpx.BaseTransport, fetch: settings.FetchSettings) -> httpx.Client:
    return httpx.Client(
        transport=transport,
        timeout=fetch.timeout,
        headers={"User-Agent": fetch.user_agent},
        follow_redirects=True,
        cookies=throttle.no_cookies(),
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
    """A 404; for Workday also a 422 (the site was removed) or a 403 "S22" (the site is closed);
    for BambooHR a redirect to bamboohr.com itself, its answer for an unknown tenant."""
    if response.status_code == 404:
        return True
    if company.ats == "bamboohr":
        if not response.is_redirect:
            return False
        try:
            target = httpx.URL(response.headers.get("location", ""))
        except httpx.InvalidURL:
            return False
        return target.host in ("bamboohr.com", "www.bamboohr.com")
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


@contextmanager
def _interrupt_on_signals() -> Iterator[None]:
    """Treat SIGHUP and SIGTERM like Ctrl-C, so closing the terminal still saves the run.

    After a SIGHUP the terminal is gone, so output goes to /dev/null rather than failing
    halfway through the save. Once the run is interrupted (Ctrl-C included), SIGHUP and
    SIGTERM no longer interrupt it until the end. A signal already ignored (``nohup``) stays so.
    """
    names = ("SIGINT", "SIGHUP", "SIGTERM")
    signums = [getattr(signal, name) for name in names if hasattr(signal, name)]
    handlers = {signum: signal.getsignal(signum) for signum in signums}
    previous = {signum: h for signum, h in handlers.items() if h is not signal.SIG_IGN}
    stdout, stderr = sys.stdout, sys.stderr
    devnull = None
    interrupted = False

    def interrupt(signum: int, frame: object) -> None:
        nonlocal devnull, interrupted
        if signum == getattr(signal, "SIGHUP", None) and devnull is None:
            devnull = open(os.devnull, "w")  # noqa: SIM115 - closed on the way out
            sys.stdout = sys.stderr = devnull
        if signum == signal.SIGINT or not interrupted:  # Ctrl-C always interrupts, as before
            interrupted = True
            raise KeyboardInterrupt

    for signum in previous:
        signal.signal(signum, interrupt)
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        sys.stdout, sys.stderr = stdout, stderr
        if devnull is not None:
            devnull.close()


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
    search: Sequence[str] = (),
    max_per_term: int | None = None,
    usajobs_auth: tuple[str, str] | None = None,
) -> BoardOutcome:
    """Fetch one board. Touches no shared state, and a failing board never stops the run.

    Once the run is ``stopping`` (interrupted), errors go unreported: the client is being closed
    under boards still in flight, and the runner discards their outcomes anyway.
    """
    try:
        jobs = fetch_company(
            company,
            client,
            wants_body=wants_body,
            pool=pool,
            search=search,
            max_per_term=max_per_term,
            usajobs_auth=usajobs_auth,
        )
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
) -> list[Job]:
    """Fold one board's outcome into the run's books and print its line. Main thread only.

    Returns the board's new jobs (also added to ``new_jobs``).
    """
    company = outcome.company
    if outcome.skipped:
        return []
    if outcome.jobs is None:
        if outcome.gone and misses.miss(company.key) >= prune_after:
            dead.add(company.key)
        return []
    misses.clear(company.key)
    passed, rejected = jfilter.apply(outcome.jobs, prefs, company.tags)
    unique: dict[str, Job] = {}  # Workable lists a posting once per location, under one shortcode
    for j in passed:
        unique.setdefault(j.key, j)
    fresh = [j for j in unique.values() if j.key not in seen]
    counts = f"total={len(outcome.jobs):<4} passed={len(passed):<3} new={len(fresh)}"
    print(f"{company.name:<16} {counts}")
    if verbose:
        for r in rejected:
            print(f"    - {r.job.title[:60]:<60} {r.reason}")
    new_jobs.extend(fresh)
    return fresh


def cmd_fetch(args: argparse.Namespace, data_dir: Path) -> int:
    with _interrupt_on_signals():
        return _cmd_fetch(args, data_dir)


def _cmd_fetch(args: argparse.Namespace, data_dir: Path) -> int:
    """Each board's new jobs are saved as it finishes, so an interrupted run loses nothing."""
    if args.resume and args.company:
        print("--resume continues a full fetch; it can't be used with --company", file=sys.stderr)
        return 2
    companies = config.load_companies(args.companies)
    if args.company:
        companies = [c for c in companies if c.name.lower() == args.company.lower()]
        if not companies:
            print(f"no company named {args.company!r} in companies.yaml", file=sys.stderr)
            return 2
    prefs = config.load_preferences()
    jobs_path = data_dir / "jobs.jsonl"
    seen = storage.SeenSet(data_dir / "seen.json", jobs=jobs_path)
    misses = storage.MissLedger(data_dir / "misses.json")
    # A full fetch notes each finished board for --resume; a one-company fetch leaves that alone.
    progress = None if args.company else storage.FetchProgress(data_dir / "fetch_progress.txt")
    resuming = args.resume and progress is not None and progress.exists()
    if resuming:
        companies = [c for c in companies if c.key not in progress.done]
        done = len(progress.done)
        print(f"resuming: {done} board(s) already fetched, {len(companies)} to go", file=sys.stderr)
    elif args.resume:
        print("nothing to resume: fetching every board", file=sys.stderr)
    if progress is not None and not resuming and not args.dry_run:
        progress.start()

    def title_passes(company: Company) -> Callable[[Job], bool]:
        return lambda job: jfilter.title_passes(job, prefs, company.tags)

    new_jobs: list[Job] = []
    dead: set[str] = set()
    interrupted = False
    fetch = args.settings.fetch
    prune = fetch.prune_after_404s
    federal = args.settings.usajobs  # the key and email go to USAJOBS boards only
    usajobs_auth = (federal.api_key, federal.email) if federal.api_key and federal.email else None

    def book(outcome: BoardOutcome) -> None:
        """Record one board, then save it: its new jobs, the seen set and misses, then progress."""
        before = misses.counts.get(outcome.company.key)
        fresh = _record(outcome, prefs, seen, misses, dead, new_jobs, args.verbose, prune)
        if args.dry_run or outcome.skipped:  # a skipped board is retried by --resume
            return
        if fresh:
            for j in fresh:
                storage.append_jsonl(jobs_path, j.model_dump())
                seen.add(j.key)
            seen.save()
        if misses.counts.get(outcome.company.key) != before:  # --resume won't fetch it again
            misses.save()
        if progress is not None:
            progress.mark(outcome.company.key)

    transport = _transport(fetch, workers=args.workers, per_host=args.per_host)
    # Later pages and descriptions (Workday, SmartRecruiters, BambooHR, Eightfold, Oracle, Apple,
    # Phenom, SuccessFactors) go to their group's pool; the transport's per-host and global limits
    # still decide how many are in flight.
    pools = _GroupPools(args.per_host)
    with _client(transport, fetch) as client, _stop_on_exit(transport, pools):
        boards = BoardRunner(
            companies,
            lambda company: _fetch_board(
                company,
                client,
                title_passes(company),
                args.verbose,
                lambda: boards.stopping,
                pools.get(rate_group(company)),
                # search terms for sites too big to list, with the board's tag extras
                prefs.title.targets(company.tags),
                fetch.max_per_term.get(company.ats),
                usajobs_auth,
            ),
            group_of=rate_group,
            per_group=args.per_host,
            refused=_refused,
            skip=lambda company: BoardOutcome(company, skipped=True),
            breaker=fetch.breaker,
        )
        try:
            for outcome in boards:  # in companies.yaml order, whatever order they finish in
                book(outcome)
        except KeyboardInterrupt:
            # Keep what the finished boards found; the next run picks up the rest.
            interrupted = True
            print("\ninterrupted: keeping the boards fetched so far", file=sys.stderr)
            late = boards.finished_out_of_order()
            if any(o.jobs is not None and not o.skipped for o in late):  # only if a line follows
                print("finished out of order:")
            for outcome in late:
                book(outcome)
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

    if progress is not None and not interrupted:
        progress.finish()
    print(f"\n{len(new_jobs)} new job(s) recorded.")
    if progress is not None and interrupted:
        print(f"run `jobhunt {args.cmd} --resume` to fetch the rest", file=sys.stderr)
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
    if args.hide_applied:
        applied = {a.job_key for a in apps.fold(storage.load_application_events(data_dir))}
        rows = [s for s in rows if s.job.key not in applied]
    for s in rows:
        print(f"{s.score.score:>2}/10  {s.job.company:<14} {s.job.title[:55]:<55} {s.job.key}")
        print(f"        {s.job.url}")
    print(f"\n{len(rows)} scored job(s).")
    return 0


def cmd_applied(args: argparse.Namespace, data_dir: Path) -> int:
    job = next((j for j in storage.load_jobs(data_dir) if j.key == args.key), None)
    if job is None:
        hint = "`jobhunt list` shows keys"
        print(f"{args.key} is not a job jobhunt has found ({hint})", file=sys.stderr)
        return 2
    events = storage.load_application_events(data_dir)
    earlier = next((a for a in apps.fold(events) if a.job_key == job.key), None)
    if earlier and not args.force:
        print(f"already applied on {earlier.applied}; --force records it again", file=sys.stderr)
        return 2
    outcomes = [e.date for e in events if e.job_key == job.key and e.kind == "outcome"]
    if args.date and outcomes and args.date > min(outcomes):
        print(f"{args.date} is after an outcome ({min(outcomes)})", file=sys.stderr)
        return 2
    scores = [s.score.score for s in storage.load_scores(data_dir) if s.job.key == job.key]
    event = apps.Event(
        job_key=job.key,
        kind="applied",
        date=args.date or (earlier.applied if earlier else _today()),
        company=job.company,
        title=job.title,
        # A correction keeps the score applied with; a fresh application takes the latest.
        score=earlier.score if earlier else scores[-1] if scores else None,
        resume=args.resume,
        warm=args.warm,
    )
    storage.append_jsonl(data_dir / "applications.jsonl", event.model_dump())
    how = f"resume {args.resume}, {apps.warm_label(args.warm)}"
    print(f"applied {event.date}: {job.company}: {job.title} ({how})")
    return 0


def cmd_outcome(args: argparse.Namespace, data_dir: Path) -> int:
    applied = {a.job_key: a for a in apps.fold(storage.load_application_events(data_dir))}
    if args.key not in applied:
        hint = "record it first with `jobhunt applied`"
        print(f"no application to {args.key}; {hint}", file=sys.stderr)
        return 2
    app = applied[args.key]
    if args.date < app.applied:
        print(f"{args.date} is before the application ({app.applied})", file=sys.stderr)
        return 2
    event = apps.Event(job_key=args.key, kind="outcome", date=args.date, status=args.status)
    storage.append_jsonl(data_dir / "applications.jsonl", event.model_dump())
    print(f"{event.date}: {app.company}: {app.title} -> {args.status}")
    return 0


def _print_groups(title: str, groups: list[apps.GroupStats]) -> None:
    print(f"\nby {title}:")
    print(
        f"  {'':<10} {'applied':>7} {'replied':>8} {'screen+':>8} {'interview+':>10}"
        f" {'offer':>5} {'days to reply':>13}"
    )
    for g in groups:
        days = "-" if g.median_days is None else f"{g.median_days:g}"
        print(
            f"  {g.label[:10]:<10} {g.applied:>7} {g.replied:>8} {g.screened:>8}"
            f" {g.interviewed:>10} {g.offers:>5} {days:>13}"
        )


def cmd_applications(args: argparse.Namespace, data_dir: Path) -> int:
    rows = apps.fold(storage.load_application_events(data_dir))
    if not rows:
        print("no applications yet (`jobhunt applied KEY --resume V` records one).")
        return 0
    for a in rows:
        score = "-" if a.score is None else f"{a.score}/10"
        print(
            f"{a.applied}  {a.status or 'pending':<11} {score:>5}  {a.resume or '-':<8} "
            f"{apps.warm_label(a.warm):<4}  {a.company[:16]:<16} {a.title[:50]}"
        )
    _print_groups("resume", apps.stats(rows, lambda a: a.resume or "-"))
    _print_groups("contact", apps.stats(rows, lambda a: apps.warm_label(a.warm)))
    _print_groups("score", apps.stats(rows, lambda a: apps.score_band(a.score)))
    print(f"\n{len(rows)} application(s).")
    return 0


LABEL_BODY_CHARS = 1500  # of a posting's description shown while labeling; the URL has the rest


def _ask_score() -> int | str | None:
    """The candidate's score, 1-10; None to skip the job, "q" to stop."""
    while True:
        reply = input("Your score, 1 to 10 (Enter skips, q quits): ").strip().lower()
        if reply in ("", "q"):
            return reply or None
        if reply.isdecimal() and 1 <= int(reply) <= 10:
            return int(reply)
        print("  a score is a whole number from 1 to 10")


def cmd_label(args: argparse.Namespace, data_dir: Path) -> int:
    labels = storage.load_labels(data_dir)
    need = args.sample - len(labels)
    if need <= 0:
        print(f"already {len(labels)} labeled; `jobhunt eval` compares them with the scorer.")
        return 0
    latest = {s.job.key: s for s in storage.load_scores(data_dir)}
    todo = evaluate.sample(latest.values(), set(labels), need)
    if not todo:
        print("no scored jobs left to label.")
        return 0
    print(f"{len(todo)} job(s) to label with your own score, 1 to 10, as the scorer would.")
    print("The scorer's score stays hidden until `jobhunt eval`. q or Ctrl-D stops; labels keep.")
    done = 0
    try:
        for i, s in enumerate(todo, start=1):
            job = s.job
            print(f"\n[{i}/{len(todo)}] {job.company}: {job.title}")
            print(f"  {job.location or 'location not given'}")
            print(f"  {job.url}\n")
            body = job.body[:LABEL_BODY_CHARS]
            more = " ..." if len(job.body) > LABEL_BODY_CHARS else ""
            print("  " + body.replace("\n", "\n  ") + more + "\n")
            score = _ask_score()
            if score == "q":
                break
            if score is None:
                continue
            try:
                note, stop = input("Note (optional): ").strip(), False
            except (EOFError, KeyboardInterrupt):  # the score just typed still counts
                note, stop = "", True
            label = evaluate.Label(job_key=job.key, score=score, note=note)
            storage.append_jsonl(data_dir / "labels.jsonl", label.model_dump())
            done += 1
            if stop:
                print()
                break
    except (EOFError, KeyboardInterrupt):
        print()
    print(f"\n{done} labeled now, {len(labels) + done} in all.")
    return 0


def _score_prompt_fingerprint(profile: str, kit: config.Kit, body_chars: int) -> str:
    """A fingerprint of everything that shapes the scoring prompt: the system prompt, the
    user-prompt template with the candidate profile and Kit modules (rendered with a placeholder
    job) and how much of a description it sends. Only the hash is stored, never the profile."""
    job = Job(
        source="greenhouse",
        company="{company}",
        company_slug="{slug}",
        external_id="{id}",
        title="{title}",
        location="{location}",
        url="{url}",
        body="{body}",
    )
    user = build_user_prompt(job, profile, kit, body_chars)
    return evaluate.fingerprint(f"{SCORE_SYSTEM}\n{user}\nbody_chars={body_chars}")


def _rescore_labeled(
    args: argparse.Namespace,
    keys: list[str],
    latest: dict[str, ScoredJob],
    complete: Completer | None,
) -> dict[str, ScoredJob]:
    """The labeled jobs scored afresh with the current prompt and model; scores.jsonl untouched."""
    llm = args.settings.llm
    complete = complete or _completer(llm)
    label = _model_label(llm)
    profile, kit = config.load_profile(), config.load_kit()
    fresh = {}
    for key in keys:
        job = latest[key].job
        try:
            fresh[key] = score_job(
                job, profile, kit, complete, llm.score_max_tokens, llm.body_chars, label
            )
        except (ValueError, KeyError) as e:  # unusable model reply: left out of this run
            log.warning("%s: %s — skipped (%s: %s)", key, job.title, type(e).__name__, e)
    return fresh


def cmd_eval(args: argparse.Namespace, data_dir: Path, complete: Completer | None = None) -> int:
    labels = storage.load_labels(data_dir)
    if not labels:
        print("no labels yet: `jobhunt label` asks for your own scores first.", file=sys.stderr)
        return 2
    latest = {s.job.key: s for s in storage.load_scores(data_dir)}
    keys = [k for k in labels if k in latest]
    if args.rescore:
        scored = _rescore_labeled(args, keys, latest, complete)
        source = "rescored"
        prompt = _score_prompt_fingerprint(
            config.load_profile(), config.load_kit(), args.settings.llm.body_chars
        )
    else:
        scored = {k: latest[k] for k in keys}
        source, prompt = "stored", None
    pairs = [
        evaluate.Pair(
            job_key=k,
            company=s.job.company,
            title=s.job.title,
            human=labels[k].score,
            model=s.score.score,
            rationale=s.score.rationale,
        )
        for k, s in scored.items()
    ]
    if not pairs:
        print("none of the labeled jobs has a score to compare.", file=sys.stderr)
        return 2
    threshold = config.load_preferences().scoring.min_score_for_letter
    m = evaluate.metrics(pairs, threshold)
    models = sorted({s.score.model for s in scored.values()})
    _print_eval(m, pairs, source, models)
    run = {
        "run_at": datetime.now(UTC).isoformat(),
        "source": source,
        "models": models,
        "prompt": prompt,
        "metrics": m.model_dump(),
    }
    storage.append_jsonl(data_dir / "evals.jsonl", run)
    return 0


def _share(x: float | None) -> str:
    return "-" if x is None else f"{x:.0%}"


def _print_eval(
    m: evaluate.Metrics, pairs: list[evaluate.Pair], source: str, models: list[str]
) -> None:
    how = "scored just now" if source == "rescored" else "their stored scores"
    print(f"{m.n} labeled job(s), {how} ({', '.join(models)})\n")
    rank = "-" if m.spearman is None else f"{m.spearman:.2f}"
    lean = "higher" if m.bias > 0 else "lower" if m.bias < 0 else "neither higher nor lower"
    print(f"  rank agreement (Spearman, -1 to 1)  {rank}")
    lean = f"the scorer scores {lean} than you"
    print(f"  mean error {m.mae:.2f} points; bias {m.bias:+.2f} ({lean})")
    print(f"  within one point: {_share(m.within_1)}")
    print(f"\n  at the letter line ({m.threshold}+):")
    print(f"    you said yes to {m.tp + m.fn}; the scorer found {m.tp} (recall {_share(m.recall)})")
    print(
        f"    the scorer said yes to {m.tp + m.fp}; you agreed on {m.tp}"
        f" (precision {_share(m.precision)})"
    )
    print("\nbiggest disagreements:")
    for p in evaluate.disagreements(pairs):
        if p.model == p.human:
            break
        print(f"  you {p.human:>2}, scorer {p.model:>2}  {p.company}: {p.title}")
        print(f"      {p.rationale}")


def cmd_run(args: argparse.Namespace, data_dir: Path, output_dir: Path) -> int:
    rc = cmd_fetch(args, data_dir)
    if rc:
        return rc
    rc = cmd_score(args, data_dir)
    if rc:
        return rc
    return cmd_letter(args, data_dir, output_dir)


# --------------------------------------------------------------------------- parser


def _today() -> str:
    return datetime.now(UTC).date().isoformat()


def _iso_date(value: str) -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a date like YYYY-MM-DD: {value!r}") from None


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
    resume_help = "fetch only the boards an interrupted fetch didn't get to"
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
    f.add_argument("--resume", action="store_true", help=resume_help)
    _concurrency_args(f)

    s = sub.add_parser("score", help="score unscored jobs with Claude")
    s.add_argument("--limit", type=int, metavar="N", help=limit_help)
    s.add_argument("--rescore", action="store_true", help=rescore_help)

    li = sub.add_parser("list", help="show scored jobs and their keys")
    li.add_argument("--min-score", type=int, metavar="N", help="hide jobs scoring below N")
    li.add_argument("--hide-applied", action="store_true", help="hide jobs already applied to")

    date_help = "the day it happened, YYYY-MM-DD (default: today, UTC)"
    ad = sub.add_parser("applied", help="record an application to a job jobhunt found")
    ad.add_argument("key", help="the job's key, as `jobhunt list` shows it")
    ad.add_argument("--resume", required=True, metavar="VERSION", help="the resume version sent")
    ad.add_argument("--warm", action="store_true", help="a warm contact (referral, intro)")
    applied_help = f"{date_help[:-1]}; a --force correction keeps the earlier date)"
    ad.add_argument("--date", type=_iso_date, help=applied_help)
    ad.add_argument("--force", action="store_true", help="record it again (a correction)")

    oc = sub.add_parser("outcome", help="record what came of an application")
    oc.add_argument("key", help="the job's key")
    oc.add_argument("status", choices=apps.STATUSES)
    oc.add_argument("--date", type=_iso_date, default=_today(), help=date_help)

    sub.add_parser("applications", help="each application, and response rates by group")

    lb = sub.add_parser("label", help="score a sample of jobs yourself, for `jobhunt eval`")
    lb.add_argument(
        "--sample", type=_at_least_one, default=50, metavar="N",
        help="how many labeled jobs to have in all (default 50)",
    )
    ev = sub.add_parser("eval", help="how well the scorer agrees with your labels")
    ev.add_argument(
        "--rescore", action="store_true",
        help="score the labeled jobs again with the current prompt and model (one call each)",
    )

    le = sub.add_parser("letter", help="generate letters for high scorers")
    le.add_argument("--min-score", type=int, metavar="N", help=min_score_help)
    le.add_argument("--job", metavar="KEY", help=job_help)
    le.add_argument("--force", action="store_true", help=force_help)

    r = sub.add_parser("run", help="fetch -> score -> letter")
    r.add_argument("--company", metavar="NAME", help=company_help)
    r.add_argument("--dry-run", action="store_true", help=dry_run_help)
    r.add_argument("--resume", action="store_true", help=resume_help)
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
        if args.cmd == "applied":
            return cmd_applied(args, args.data_dir)
        if args.cmd == "outcome":
            return cmd_outcome(args, args.data_dir)
        if args.cmd == "applications":
            return cmd_applications(args, args.data_dir)
        if args.cmd == "label":
            return cmd_label(args, args.data_dir)
        if args.cmd == "eval":
            return cmd_eval(args, args.data_dir)
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
