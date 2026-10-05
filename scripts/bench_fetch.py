"""Benchmark ``jobhunt fetch`` on a sample of your boards, to pick --workers and --per-host.

    python scripts/bench_fetch.py sample [-n 150] [--seed 1] [--companies PATH] [-o data/bench.yaml]
    python scripts/bench_fetch.py run [--companies data/bench.yaml] [--workers N] [--per-host N]

``sample`` draws boards so every rate-limit group (a Workday datacenter or an API host) gets one,
then shares the rest by each group's remaining room, keeping companies.yaml order. ``run`` times a
dry-run fetch of that file with -v, so the per-host request, throttle and peak-concurrency stats
print at the end. A dry run records nothing, but it does make real requests: every run hits each
sampled board once, so keep samples small and don't loop it.
"""

from __future__ import annotations

import argparse
import random
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from jobhunt import cli, config, settings, slugs
from jobhunt.schema import Company
from jobhunt.sources import rate_group

DEFAULT_SAMPLE = Path("data/bench.yaml")


def stratified_sample(companies: Sequence[Company], n: int, seed: int = 1) -> list[Company]:
    """n boards: one per group while there's room, the rest by remaining room; in config order."""
    if n < 1:
        raise ValueError(f"n must be at least 1, not {n}")
    if n >= len(companies):
        return list(companies)
    groups: dict[str, list[Company]] = {}
    for c in companies:
        groups.setdefault(rate_group(c), []).append(c)
    by_size = sorted(groups, key=lambda g: -len(groups[g]))
    take = dict.fromkeys(by_size, 0)
    for g in by_size[:n]:  # everyone gets one, biggest first if there aren't enough
        take[g] = 1
    left = n - sum(take.values())
    room = {g: len(groups[g]) - take[g] for g in by_size}
    total = sum(room.values())
    if left and total:
        shares = {g: left * room[g] / total for g in by_size}
        for g in by_size:
            take[g] += int(shares[g])
        by_remainder = sorted(by_size, key=lambda g: -(shares[g] - int(shares[g])))
        for g in by_remainder[: n - sum(take.values())]:
            take[g] += 1
    rng = random.Random(seed)
    chosen = {id(c) for g in by_size for c in rng.sample(groups[g], take[g])}
    return [c for c in companies if id(c) in chosen]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bench_fetch", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sample", help="write a stratified sample of companies.yaml")
    s.add_argument("-n", type=cli._at_least_one, default=150)
    s.add_argument("--seed", type=int, default=1)
    s.add_argument("--companies", type=Path, help="default: config/companies.yaml")
    s.add_argument("-o", "--out", type=Path, default=DEFAULT_SAMPLE)
    r = sub.add_parser("run", help="time a dry-run fetch of the sample")
    r.add_argument("--companies", type=Path, default=DEFAULT_SAMPLE)
    r.add_argument("--workers", help="default: fetch.workers from settings")
    r.add_argument("--per-host", help="default: fetch.per_host from settings")
    args = parser.parse_args(argv)

    if args.cmd == "sample":
        try:
            companies = config.load_companies(args.companies)
        except config.ConfigMissing as e:
            print(e, file=sys.stderr)
            return 2
        if not companies:
            print("no boards to sample; add some to companies.yaml first", file=sys.stderr)
            return 2
        boards = stratified_sample(companies, args.n, args.seed)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text("companies:\n" + slugs.render(boards))
        print(f"{len(boards)} boards -> {args.out}")
        return 0

    if not args.companies.exists():
        print(f"no sample at {args.companies}; run `bench_fetch.py sample` first", file=sys.stderr)
        return 2
    fetch = settings.load().fetch  # what fetch itself will use for any flag not given here
    flags = []
    if args.workers:
        flags += ["--workers", args.workers]
    if args.per_host:
        flags += ["--per-host", args.per_host]
    start = time.monotonic()
    rc = cli.main(["-v", "--companies", str(args.companies), "fetch", "--dry-run", *flags])
    elapsed = time.monotonic() - start
    if rc in (0, 130):  # finished, or stopped with Ctrl-C: the time still means something
        workers, per_host = args.workers or fetch.workers, args.per_host or fetch.per_host
        flags = f"--workers {workers} --per-host {per_host}"
        print(f"\n{flags}: {elapsed:.0f} s", file=sys.stderr)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
