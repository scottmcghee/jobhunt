"""Tag the boards in companies.yaml by size and industry, so filter rules can target groups.

    python scripts/tag_companies.py [--companies PATH] [--data-dir DIR] [--survey PATH]
                                    [--no-llm] [--batch-size N] [--max-batches N] [--dry-run]

Tags come from, cheapest first:

1. ``big-tech``: a short list of FAANG-scale employers (``BIG_TECH``), matched by board.
2. ``sp500`` and an industry for its sector: boards the S&P 500 survey
   (scripts/survey_careers.py, data/sp500/results.json) tied to a constituent.
3. Industry for the rest: the configured model (llm.py) classifies boards about 100 at a time,
   from each board's name, slug and a few of its job titles (data/jobs.jsonl), choosing only from
   ``INDUSTRIES``. Answers are cached by board in data/company_tags.json, so a rerun only asks
   about boards it hasn't seen. ``--no-llm`` uses the cache and asks nothing new.

The script only manages the tags in ``VOCABULARY``: it recomputes those on every run and leaves
any other tag (``seattle``, ``remote``, ...) as written. It edits companies.yaml as text, so
comments and layout stay put; a block-style ``tags:`` list becomes a flow list. Rerun it whenever
the board list changes. ``--dry-run`` reports what would change without writing.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import textwrap
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import yaml

from jobhunt import config, settings, storage
from jobhunt.llm import extract_json, make_completer
from jobhunt.schema import Company

log = logging.getLogger("tag_companies")

INDUSTRIES = (
    "edtech", "healthcare", "public-sector", "nonprofit", "public-safety",
    "devtools", "security", "ai", "saas",
    "fintech", "insurtech",
    "retail", "manufacturing", "energy", "media", "gaming", "crypto", "consulting",
    "staffing", "defense",
)  # fmt: skip
VOCABULARY = ("big-tech", "sp500", *INDUSTRIES)
MAX_INDUSTRIES = 2

# FAANG-scale employers, by the word their boards are named or hosted under.
BIG_TECH = ("amazon", "apple", "microsoft", "google", "alphabet", "meta", "netflix", "nvidia",
            "tesla", "salesforce", "oracle")  # fmt: skip
# Boards whose name or slug doesn't say who they are.
BIG_TECH_HOSTS = ("eeho.fa.us2.oraclecloud.com",)  # Oracle's own careers site

# The S&P 500's GICS sectors, as industry tags. Broad sectors (IT, Financials, Real Estate) are
# left to the model, since they span several of the tags.
SECTORS = {
    "Health Care": ["healthcare"],
    "Energy": ["energy"],
    "Utilities": ["energy"],
    "Industrials": ["manufacturing"],
    "Materials": ["manufacturing"],
    "Consumer Discretionary": ["retail"],
    "Consumer Staples": ["retail"],
    "Communication Services": ["media"],
}

PROMPT = f"""You tag companies by industry for a job search tool. For each company line
(key | name | board slug | some job titles), choose up to {MAX_INDUSTRIES} tags from this list
only, or none if none clearly fits:

{", ".join(INDUSTRIES)}

Answer with JSON only: an object mapping each key, exactly as given, to a list of tags."""

Completer = Callable[[str, str, int], str]


def is_big_tech(board: Company) -> bool:
    """A board of a BIG_TECH employer: by source, by host, or by the first word of its name."""
    if board.ats in ("amazon", "apple"):  # their own careers sites
        return True
    host = board.slug.split("/")[0].lower()  # a Workday tenant, or a careers site's host
    if host in BIG_TECH_HOSTS:
        return True
    labels = set(re.split(r"[^a-z0-9]+", host))  # "apply.careers.microsoft.com" -> microsoft
    first = (re.findall(r"[a-z0-9]+", board.name.lower()) or [""])[0]
    return bool(labels & set(BIG_TECH)) or first in BIG_TECH


def sector_tags(sector: str) -> list[str]:
    return list(SECTORS.get(sector, []))


def _key(board: Company) -> str:
    """A board's identity, case-insensitively (the survey and the config may differ in case).

    An Oracle board is its host: the search returns the host's postings whatever the site, and the
    config keeps one board per host, often not the site the survey recorded. Workday keeps the
    site, since one tenant can host several companies' or divisions' sites.
    """
    if board.ats == "oracle":
        return f"oracle:{board.slug.split('/')[0].lower()}"
    return board.key.lower()


def sp500_boards(results: Path) -> dict[str, tuple[str, str]]:
    """Board key (lowercased) -> (ticker, GICS sector), from the survey's results.json."""
    if not results.exists():
        return {}
    found = {}
    for row in json.loads(results.read_text()):
        for b in row.get("boards") or []:
            found[_key(Company.model_validate(b))] = (row["ticker"], row.get("sector", ""))
    return found


def titles_by_board(data_dir: Path, per_board: int = 5) -> dict[str, list[str]]:
    """Up to ``per_board`` job titles for each board, from data/jobs.jsonl."""
    titles: dict[str, list[str]] = {}
    if not (data_dir / "jobs.jsonl").exists():
        return titles
    for job in storage.load_jobs(data_dir):
        some = titles.setdefault(f"{job.source}:{job.company_slug}", [])
        if len(some) < per_board and job.title not in some:
            some.append(job.title)
    return titles


def _line(board: Company, titles: Mapping[str, list[str]]) -> str:
    sample = "; ".join(titles.get(board.key, [])[:5])
    return f"{board.key} | {board.name} | {board.slug} | {sample}"


def classify(
    boards: Sequence[Company],
    complete: Completer,
    cache_path: Path,
    titles: Mapping[str, list[str]],
    batch_size: int = 100,
    max_batches: int | None = None,
) -> dict[str, list[str]]:
    """Industry tags per board key, from the cache, asking the model about boards not in it."""
    cache: dict[str, list[str]] = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    todo = [b for b in boards if b.key not in cache]
    batches = [todo[i : i + batch_size] for i in range(0, len(todo), batch_size)]
    for n, batch in enumerate(batches[:max_batches]):
        user = "\n".join(_line(b, titles) for b in batch)
        try:
            answer = extract_json(complete(PROMPT, user, 60 * len(batch) + 200))
        except Exception as e:  # a bad answer costs this batch, not the run
            log.warning("batch %d: couldn't read the model's answer (%s)", n + 1, e)
            continue
        for b in batch:
            got = answer.get(b.key)
            if isinstance(got, list):  # "Public sector" -> public-sector
                said = [re.sub(r"[ _]", "-", t.strip().lower()) for t in got if isinstance(t, str)]
                cache[b.key] = [t for t in said if t in INDUSTRIES][:MAX_INDUSTRIES]
        cache_path.write_text(json.dumps(cache, indent=1, sort_keys=True) + "\n")
        log.info("classified batch %d of %d", n + 1, len(batches))
    return {b.key: cache[b.key] for b in boards if b.key in cache}


def managed_tags(
    board: Company, industry: Mapping[str, list[str]], sp500: Mapping[str, tuple[str, str]] | None
) -> list[str]:
    """``sp500`` None means no survey to go by: a board keeps the ``sp500`` tag it has."""
    tags = []
    if is_big_tech(board):
        tags.append("big-tech")
    if sp500 is None:
        if "sp500" in board.tags:
            tags.append("sp500")
    elif (hit := sp500.get(_key(board))) is not None:
        tags.append("sp500")
        tags += sector_tags(hit[1])
    if board.key in industry:
        tags += industry[board.key]
    else:  # no answer (not asked, or a skipped batch) isn't "no industries": keep what's there
        tags += [t for t in board.tags if t in INDUSTRIES]
    return list(dict.fromkeys(tags))


_TAGS_LINE = re.compile(r"^(\s+(?:- )?)tags:(.*?)\n?$")


def _quote(tag: str) -> str:
    """A tag as YAML that reads back as the same string ("2024", "yes", "a, b" need quotes)."""
    if re.fullmatch(r"[A-Za-z0-9_.-]+", tag) and yaml.safe_load(tag) == tag:
        return tag
    return json.dumps(tag)


def _split_comment(value: str) -> tuple[str, str]:
    """A tags line's value and its trailing comment, if any (a "#" inside quotes isn't one)."""
    whole = yaml.safe_load(f"x:{value}")
    for m in re.finditer(r"\s+#", value):
        try:
            if yaml.safe_load(f"x:{value[: m.start()]}") == whole:
                return value[: m.start()], value[m.start() :]
        except yaml.YAMLError:
            continue
    return value, ""


def _rewritable(entry: list[str]) -> bool:
    """Whether ``_rewrite`` handles this entry's tags: a one-line value, or a block list."""
    for i, line in enumerate(entry):
        if m := _TAGS_LINE.match(line):
            try:  # a flow list wrapped across lines doesn't parse on its own line
                value, _ = _split_comment(m.group(2))
            except yaml.YAMLError:
                return False
            rest = [x for x in entry[i + 1 :] if x.strip() and not x.lstrip().startswith("#")]
            if not value.strip() and rest and not re.match(rf"^ {{{len(m.group(1))},}}- ", rest[0]):
                return False  # e.g. a flow list on the next line
    return True


def _rewrite(entry: list[str], tags: list[str]) -> list[str]:
    """An entry's lines with its tags set to ``tags``, as one flow-list line."""
    first_key = next(i for i, line in enumerate(entry) if re.match(r"^\s*- \w", line))
    indent = " " * (len(entry[first_key]) - len(entry[first_key].lstrip(" -")))
    flow = f"tags: [{', '.join(_quote(t) for t in tags)}]"
    out, i = [], 0
    while i < len(entry):
        if m := _TAGS_LINE.match(entry[i]):
            value, comment = _split_comment(m.group(2))
            out.append(f"{m.group(1)}{flow}{comment}\n")
            i += 1
            if not value.strip():  # a block list: drop its "- item" lines, and comments among them
                column = len(m.group(1))
                j, end = i, i
                while j < len(entry) and (not entry[j].strip() or entry[j].lstrip().startswith("#")
                                          or re.match(rf"^ {{{column},}}- ", entry[j])):
                    j += 1
                    if entry[j - 1].strip().startswith("- "):
                        end = j  # up to the last item: a blank line after it ends the entry
                i = end
            continue
        out.append(entry[i])
        i += 1
    if not any(_TAGS_LINE.match(x) for x in entry):  # no tags line: add one after the last key
        last = max(i for i, x in enumerate(out) if x.strip() and not x.lstrip().startswith("#"))
        if not out[last].endswith("\n"):
            out[last] += "\n"
        out.insert(last + 1, f"{indent}{flow}\n")
    return out


def apply_tags(
    path: Path,
    industry: Mapping[str, list[str]],
    sp500: Mapping[str, tuple[str, str]] | None,
    dry_run: bool = False,
) -> int:
    """Recompute the managed tags of every entry; returns how many entries changed."""
    lines = path.read_text().splitlines(keepends=True)
    out: list[str] = []
    changed = 0
    spans = config._entry_spans(lines)
    out += lines[: spans[0][0]] if spans else lines
    expected = []
    for lo, hi in spans:
        entry = lines[lo:hi]
        board = Company.model_validate(yaml.safe_load(textwrap.dedent("".join(entry)))[0])
        keep = [t for t in board.tags if t not in VOCABULARY]
        tags = keep + managed_tags(board, industry, sp500)
        if tags != board.tags and not _rewritable(entry):
            log.warning("%s: can't rewrite its tags as written; left it unchanged", board.key)
            tags = board.tags
        expected.append(board.model_copy(update={"tags": tags}))
        if tags != board.tags:
            changed += 1
            entry = _rewrite(entry, tags)
        out += entry
    out += lines[spans[-1][1] :] if spans else []
    if changed:
        _check("".join(lines), "".join(out), expected)
    if changed and not dry_run:
        path.write_text("".join(out))
    return changed


def _check(before: str, after: str, expected: list[Company]) -> None:
    """Raise unless ``after`` reads back as ``expected`` with the rest of ``before`` unchanged."""
    try:
        old, new = yaml.safe_load(before), yaml.safe_load(after)
        got = [Company.model_validate(e) for e in new.pop("companies")]
    except Exception as e:
        raise ValueError(f"rewriting companies.yaml broke it ({e}); left it unchanged") from e
    old.pop("companies")
    if got != expected or new != old:
        raise ValueError("rewriting companies.yaml changed more than tags; left it unchanged")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tag_companies", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    default_companies = config.DEFAULT_CONFIG_DIR / "companies.yaml"
    parser.add_argument("--companies", type=Path, default=default_companies)
    parser.add_argument("--data-dir", type=Path, default=storage.DEFAULT_DATA_DIR)
    survey_help = "survey results (default: DATA_DIR/sp500/results.json)"
    parser.add_argument("--survey", type=Path, help=survey_help)
    parser.add_argument("--no-llm", action="store_true", help="use cached industries only")
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--max-batches", type=int, help="ask the model at most N times this run")
    parser.add_argument("--dry-run", action="store_true", help="report changes without writing")
    args = parser.parse_args(argv)

    boards = config.load_companies(args.companies)
    survey = args.survey or args.data_dir / "sp500" / "results.json"
    sp500 = sp500_boards(survey) if survey.exists() else None
    if sp500 is None:
        log.warning("no survey at %s; keeping existing sp500 tags", survey)
    cache = args.data_dir / "company_tags.json"
    if args.no_llm:
        cached = json.loads(cache.read_text()) if cache.exists() else {}
        industry = {b.key: cached[b.key] for b in boards if b.key in cached}
    else:
        try:
            llm = settings.load().llm
        except settings.SettingsError as e:
            print(e, file=sys.stderr)
            return 2
        industry = classify(boards, make_completer(llm), cache, titles_by_board(args.data_dir),
                            args.batch_size, args.max_batches)
    changed = apply_tags(args.companies, industry, sp500, dry_run=args.dry_run)
    counts = Counter(t for b in boards for t in managed_tags(b, industry, sp500))
    verb = "would change" if args.dry_run else "changed"
    print(f"{len(boards)} boards; {changed} entries {verb}; {len(industry)} classified")
    for tag, n in counts.most_common():
        print(f"  {tag:<15} {n}")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    raise SystemExit(main())
