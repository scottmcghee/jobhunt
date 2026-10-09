# CLAUDE.md — jobhunt

This file is the constitution for AI-assisted work in this repo. Read it before touching code.

## What this is

A small, well-tested Python CLI that:

1. **Ingests** open roles from company career sites (see [Sources](#sources)).
2. **Filters** them against hard constraints (title level, location, remote policy, keywords).
3. **Scores** each surviving role 1–10 against a fixed candidate profile using Claude, with a written rationale.
4. **Generates** a tailored cover letter for high-scoring roles by assembling pre-written proof modules from a Cover Letter Kit, never by inventing claims.
5. **Tracks** applications to the roles it found and their outcomes, with reply rates by resume version, warm or cold contact, and score.
6. **Evaluates** the scorer against the candidate's own labels for a sample of jobs.

It is also a portfolio piece. Code quality, tests, and the README matter as much as the output.

## Non-negotiables

- **Resume facts are fixed.** The candidate profile (`config/profile.md`) and Kit modules (`config/kit/`) are the only source of claims about the candidate. The generator *selects and arranges*; it does not fabricate metrics, titles, employers, or technologies.
- **Tests first.** Every module under `src/jobhunt/` has a matching `tests/test_<module>.py`. Write or extend the test before the implementation. Run `pytest` before declaring anything done.
- **No live network in tests.** Sources are tested against fixtures in `tests/fixtures/` using `respx`. Model calls use a fake `Completer`, never a real backend. `pytest` must pass offline.
- **Idempotent ingestion.** Re-running `jobhunt fetch` never duplicates a job. The dedupe key is `(source, company_slug, external_id)` (`Job.key`); `data/seen.json` is the ledger.
- **Secrets and personal details stay out.** `ANTHROPIC_API_KEY` (if used) comes from the environment; USAJOBS's key and email come from the environment or the gitignored `config/settings.yaml`, and go to data.usajobs.gov only. Nothing in `config/`, `data/`, or `output/` is committed (see `.gitignore`): `config/` holds a real person's profile and contact details. Committed templates live in `config.example/` and describe a fictional candidate; never copy real details into them.
- **Tests use the templates.** Tests read `config.example/`, never `config/`, so they pass on a fresh clone. The one exception, `test_local_config_is_valid`, only validates a personal `config/` if one exists.
- **Small functions, typed.** Pydantic models for all data crossing a boundary. Type hints everywhere. Prefer pure functions; isolate I/O at the edges (`sources/`, `storage.py`, `llm.py`).

## Layout

```
config.example/       # committed templates (fictional candidate); copy to config/ to start
config/               # personal, gitignored; same files as below
  companies.yaml      # boards: name, ats, slug (+ datacenter, location, tags where a source needs them)
  preferences.yaml    # filter rules: titles, locations, remote, keywords, min score
  profile.md          # the candidate narrative the scorer reads
  settings.yaml       # optional tunables (concurrency, timeouts, model, token budgets, paths)
  kit/                # Cover Letter Kit: opening.md, closing.md, modules/*.md
docs/sources.md       # per-source details: endpoints, slug formats, limits
src/jobhunt/
  schema.py           # Company, Job, Score, ScoredJob, Letter models; ATSName
  config.py           # load and validate companies.yaml, preferences.yaml, profile and Kit
  settings.py         # tunables: defaults < config/settings.yaml < JOBHUNT_<SECTION>_<KEY> env < CLI flags
  sources/            # one module per source; __init__.py dispatches (fetch_company) and assigns rate groups
                      # _html.py, _postings.py, _rate.py, _search.py, _sitemap.py are shared helpers
  filter.py           # pure: list[Job] x Preferences -> list[Job]
  score.py            # Claude call: Job x profile -> ScoredJob
  generate.py         # Claude call: ScoredJob x Kit -> Letter
  storage.py          # data/ and output/: seen-set, gone-board and fetch-progress ledgers, JSONL records
                      # (jobs, scores, applications, labels, evals), letters
  throttle.py         # polite HTTP for fetch: adaptive per-group concurrency, rate caps, 429/Retry-After and transient retries
  runner.py           # concurrent fetch: a worker pool per rate-limit group, results in config order
  llm.py              # the only module that talks to a model: Anthropic SDK, Bedrock, or `claude -p` backend
  applications.py     # pure: applied/outcome events -> applications, and reply stats by group
  evaluate.py         # pure: a sample of jobs to label; labels vs. model scores -> agreement metrics
  cli.py              # `jobhunt fetch | score | list | letter | run | applied | outcome | applications
                      #  | label | eval`
  commoncrawl.py      # Common Crawl's URL index read by byte range: every URL a crawl saw under some hosts;
                      # and its web graph's host list: every careers.* / jobs.* host it saw
  discover.py         # `python -m jobhunt.discover`: new boards from Common Crawl and careers hosts
  fingerprint.py      # which hiring platform a careers site runs, and its board (survey and discovery)
  slugs.py            # `python -m jobhunt.slugs`: board URLs in any text -> companies.yaml entries;
                      # offline, except --check, which fetches the first page of each new board via sources/
tests/
  fixtures/           # real-shaped source responses (JSON and HTML), anonymized
scripts/
  bench_fetch.py      # times fetch on a stratified sample of boards, to tune --workers/--per-host
  survey_careers.py   # S&P 500 careers sites: which hiring platform each uses, and new boards
  tag_companies.py    # managed tags in companies.yaml: big-tech, sp500, industry (model, cached)
data/                 # runtime state (gitignored)
output/               # generated letters (gitignored)
```

## Workflow for any change

1. **Propose a plan and wait for approval before changing anything.** This applies to every task, large or small. Read-only investigation (reading code, inspecting data, probing a public API) is fine first, to ground the plan. The plan walks through the reasoning, not just the steps:
   - **Goal:** what the task is, in your own words, and how we'll know it's done.
   - **Findings:** what the investigation showed that shapes the approach.
   - **Approach:** the steps, and why this approach.
   - **Alternatives:** what else was considered, and why it was rejected.
   - **Risks and open questions:** anything that could go wrong, or that the owner should decide.
   - **Scope:** the files expected to change, and what will deliberately *not* change.
2. Add or update the test.
3. Implement the smallest change that passes.
4. `pytest`. Then `ruff check .` if available.
5. Summarize what changed and what was *not* changed.
6. Open a PR, then run `/pr-review-loop <PR#>` on it before the owner merges (the owner can also run it). Each round, a read-only `pr-reviewer` agent reports only verified findings, the loop checks them, and a `pr-fixer` agent fixes the confirmed ones test-first and pushes; it stops after a clean round, or after 3. The owner has given standing approval for the loop on every PR: the fixer fixes and pushes confirmed findings without a separate plan, and steps 2-4 still apply. The skill and both agents live in `.claude/`.

## Conventions

- Python 3.11+. `httpx` for HTTP, `pydantic` v2 for models, `pyyaml` for config, `anthropic` SDK for Claude.
- Model and backend selection live in one place: `llm.py`, which reads `llm.backend` and `llm.model` from settings (`JOBHUNT_BACKEND`/`JOBHUNT_MODEL` still work; Bedrock is never chosen automatically).
- Tunables live in `settings.py`, not as module constants: add the field there with today's value as its default, document it in `config.example/settings.yaml` (a test checks the template lists every field at its default and names its env var), and pass the value down from `cli.py`. API constraints (page sizes, endpoints) stay constants.
- Logging via `logging`, not `print`, except in `cli.py` output.
- Dates are ISO 8601 strings in UTC.

## Sources

Every source is a module under `sources/`, an `ATSName` in `schema.py`, and an entry in one of three tables in `sources/__init__.py`:

- `FETCHERS`: one listing with descriptions (Greenhouse, Lever, Ashby, Workable, Gem; iCIMS Career Sites, paged).
- `ON_DEMAND_FETCHERS`: the listing lacks descriptions, so each costs a request. `fetch` passes a `wants_body` check ("title passes the title filter"), so only those postings pay (Workday, SmartRecruiters, BambooHR, SuccessFactors, Radancy, Paradox, Rippling).
- `SEARCH_FETCHERS`: employers too big to list run one search per term: the title filter's target words, plus the board's `include_for_tags` extras (Amazon, Eightfold, Oracle Recruiting Cloud, Apple, Phenom, USAJOBS). The normal filter still runs afterwards.

Endpoints, slug formats, gone-board signals and rate caps for each source are in [docs/sources.md](docs/sources.md).

**Dead boards are pruned.** `jobhunt fetch` counts consecutive fetches that find a board gone (`cli._board_gone`: a 404, or a source's own signal) in `data/misses.json`. A successful fetch resets the count; after `fetch.prune_after_404s` in a row (default 3) the entry is removed from `config/companies.yaml`. `--dry-run` changes neither.

**Companies with no supported ATS** (e.g., Apple) are case-by-case and may need crawling HTML rather than calling an API.

- **Investigate first.** Check whether the careers site is backed by a JSON endpoint before writing a crawler.
- **Crawling rules.** Respect `robots.txt` and rate limits, and test crawlers against saved HTML fixtures. One exception, the owner's decision: Common Crawl's published index files are downloaded from data.commoncrawl.org although its robots.txt disallows crawlers, because its own documentation directs people to download them there; this is personal, non-commercial use of a public dataset (`commoncrawl.py`).
- **Fit the existing pipeline.** Output must still be `Job` models so filter, score, and letter stay unchanged.
- **Off-limits:** Google (robots.txt disallows its job pages), Meta (its terms forbid automated collection without written permission), classic iCIMS portals (`Disallow: /`), NEOGOV / governmentjobs.com and schooljobs.com (`Disallow: /` for all but named search engines, and its terms ban scraping even public pages), ctcLink (`Disallow: /`), SCALIS (its `/api/` is disallowed and its pages carry no job data), UKG / UltiPro job boards (`Disallow: */JobBoardView`), Pinpoint (`Disallow: /`), Dover (`Disallow: /api/`), PageUp (a bot wall), and any board whose robots.txt disallows its own path (some Workday sites disallow their site path). [docs/sources.md](docs/sources.md#not-covered-watch-these-by-hand) says what sits behind them.

## Roadmap

Done: dead-board pruning; Workday, SmartRecruiters, Workable, BambooHR, SuccessFactors, Radancy, Paradox, iCIMS Career Sites, Gem and Rippling; search sources for Amazon, Eightfold, Oracle, Apple, Phenom and USAJOBS; the S&P 500 survey (`scripts/survey_careers.py`) to find more boards.

Open: more companies with no supported ATS, case by case, under the rules above. Each item follows the workflow (fixture and test first).

## Things Claude (the assistant) should not do here

- Do not add a new source (ATS or not) without a fixture and a test.
- Do not change scoring rubric wording in `score.py` without updating `tests/test_score.py` golden assertions.
- Do not "improve" the candidate's claims in `config/profile.md` or `config/kit/`. Ask.
- Do not add a web UI, database, or scheduler. This is a CLI. Scope creep is the enemy of a finished portfolio piece.
