# CLAUDE.md — jobhunt

This file is the constitution for AI-assisted work in this repo. Read it before touching code.

## What this is

A small, well-tested Python CLI that:

1. **Ingests** open roles from company career pages via ATS JSON APIs (Greenhouse, Lever, Ashby, Workday, SmartRecruiters, Workable, BambooHR).
2. **Filters** them against hard constraints (title level, location, remote policy, keywords).
3. **Scores** each surviving role 1–10 against a fixed candidate profile using Claude, with a written rationale.
4. **Generates** a tailored cover letter for high-scoring roles by assembling pre-written proof modules from a Cover Letter Kit — never by inventing claims.

It is also a portfolio piece. Code quality, tests, and the README matter as much as the output.

## Non-negotiables

- **Resume facts are fixed.** The candidate profile (`config/profile.md`) and Kit modules (`config/kit/`) are the only source of claims about the candidate. The generator *selects and arranges*; it does not fabricate metrics, titles, employers, or technologies.
- **Tests first.** Every module under `src/jobhunt/` has a matching `tests/test_<module>.py`. Write or extend the test before the implementation. Run `pytest` before declaring anything done.
- **No live network in tests.** ATS clients are tested against fixtures in `tests/fixtures/` using `respx`. Claude calls are mocked. `pytest` must pass offline.
- **Idempotent ingestion.** Re-running `jobhunt fetch` never duplicates a job. Dedupe key is `(source, company_slug, external_id)`; `data/seen.json` is the ledger.
- **Secrets and personal details stay out.** `ANTHROPIC_API_KEY` (if used) comes from the environment. Nothing in `config/`, `data/`, or `output/` is committed (see `.gitignore`): `config/` holds a real person's profile and contact details. Committed templates live in `config.example/` and describe a fictional candidate; never copy real details into them.
- **Tests use the templates.** Tests read `config.example/`, never `config/`, so they pass on a fresh clone. The one exception, `test_local_config_is_valid`, only validates a personal `config/` if one exists.
- **Small functions, typed.** Pydantic models for all data crossing a boundary. Type hints everywhere. Prefer pure functions; isolate I/O at the edges (`sources/`, `storage.py`, `llm.py`).

## Layout

```
config.example/       # committed templates (fictional candidate); copy to config/ to start
config/               # personal, gitignored; same files as below
  companies.yaml      # company -> ATS type + board slug
  preferences.yaml    # filter rules: titles, locations, remote, keywords, min score
  profile.md          # the candidate narrative the scorer reads (facts are FIXED)
  settings.yaml       # optional tunables (concurrency, timeouts, model, token budgets, paths)
  kit/                # Cover Letter Kit: opening.md, closing.md, modules/*.md
src/jobhunt/
  schema.py           # Job, ScoredJob, Letter models
  sources/            # one module per ATS; each exposes fetch(company) -> list[Job]
  filter.py           # pure: list[Job] x Preferences -> list[Job]
  score.py            # Claude call: Job x profile -> ScoredJob
  generate.py         # Claude call: ScoredJob x Kit -> Letter
  storage.py          # seen-set and gone-board ledgers (see cli._board_gone), JSONL append
  throttle.py         # polite HTTP for fetch: per-group concurrency limits, 429/Retry-After and transient retries
  runner.py           # concurrent fetch: a worker pool per rate-limit group, results in config order
  llm.py              # the ONLY module that talks to a model: Anthropic SDK, Bedrock, or `claude -p` backend
  settings.py         # tunables: defaults < config/settings.yaml < JOBHUNT_<SECTION>_<KEY> env < CLI flags
  cli.py              # `jobhunt fetch | score | letter | run`
  slugs.py            # `python -m jobhunt.slugs`: board URLs in any text -> companies.yaml entries;
                      # offline, except --check, which fetches the first page of each new board via sources/
tests/
  fixtures/           # real-shaped ATS responses, anonymized
scripts/
  bench_fetch.py      # times fetch on a stratified sample of boards, to tune --workers/--per-host
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
6. Open a PR, then run `/pr-review-loop <PR#>` on it before the owner merges (the owner can also run it). A read-only `pr-reviewer` agent reports only verified findings, a `pr-fixer` agent fixes the confirmed ones test-first and pushes, and the loop repeats until a round is clean (at most 3). The owner has given standing approval for the loop on every PR: the fixer fixes and pushes confirmed findings without a separate plan, and steps 2-4 still apply. Both agents and the skill live in `.claude/`.

## Conventions

- Python 3.11+. `httpx` for HTTP, `pydantic` v2 for models, `pyyaml` for config, `anthropic` SDK for Claude.
- Model and backend selection live in one place: `llm.py`, which reads `llm.backend` and `llm.model` from settings (`JOBHUNT_BACKEND`/`JOBHUNT_MODEL` still work; Bedrock is never chosen automatically). Tests fake the `Completer`; never call a real backend in tests.
- Tunables live in `settings.py`, not as module constants: add the field there with today's value as its default, document it in `config.example/settings.yaml` (a test checks the template lists every field at its default and names its env var), and pass the value down from `cli.py`. API constraints (page sizes, endpoints) stay constants.
- Logging via `logging`, not `print`, except in `cli.py` output.
- Dates are ISO 8601 strings in UTC.

## Roadmap

Agreed future work, in rough priority order. Each item still follows the workflow above (fixture + test first).

1. **Prune dead boards automatically.** *(done)* `jobhunt fetch` counts consecutive fetches that find a board gone (an HTTP 404; for Workday also a 422, or a 403 `S22` closed site) in `data/misses.json`. Any successful fetch resets the count; the third in a row removes the entry from `config/companies.yaml`. `--dry-run` changes neither.
2. **Workday and SmartRecruiters sources.** A module under `sources/` for each, with a recorded fixture and `respx` tests, plus an `ATSName` entry.
   - **Workday** *(done)*. There is no documented public API; `sources/workday.py` calls the JSON endpoints each tenant's careers site uses.
     - A board is `slug: tenant/site` plus `datacenter: wdN`, the only optional `Company` field. Tenant and site together are the board's identity (one tenant often has several sites), so keys, 404 pruning, and duplicate checks work unchanged.
     - The listing has no descriptions, and each description costs one request. `fetch` takes a `wants_body` check; the CLI passes "title passes the title filter", so only those postings pay.
     - `jobhunt.slugs` harvests `<tenant>.<wdN>.myworkdayjobs.com/[<lang>/]<site>` URLs.
   - **SmartRecruiters** *(done)*. `sources/smartrecruiters.py` uses the public Posting API; a board is `slug: <company identifier>`.
     - Like Workday, the listing has no descriptions, so `fetch` takes the same `wants_body` check.
     - An unknown identifier returns 200 with no postings, not a 404, so 404 pruning never fires. `fetch` warns on an empty board instead of guessing that it is dead.
     - `jobhunt.slugs` harvests `jobs.smartrecruiters.com/<identifier>` and `careers.smartrecruiters.com/<identifier>` URLs.
   - **Workable and BambooHR** *(done)*. Neither documents these endpoints; both are what each account's own careers page calls.
     - Workable: `sources/workable.py`, one request per board with descriptions included. A board is `slug: <account>`; an unknown account is a 404. `jobhunt.slugs` harvests `apply.workable.com/<account>` and the older `<account>.workable.com/jobs|j/...`.
     - BambooHR: `sources/bamboohr.py`, a listing plus one detail request per wanted posting (the same `wants_body` check). A board is `slug: <tenant>`. An unknown tenant redirects to bamboohr.com, which `cli._board_gone` counts like a 404; the adapter never follows redirects. Every tenant has its own subdomain, but they share one rate group. `jobhunt.slugs` harvests `<tenant>.bamboohr.com/careers|jobs/...`.
3. **Companies with no ATS (e.g., Apple).** These are case-by-case and may need crawling HTML rather than calling an API, so treat this as a separate flow, not another `sources/` adapter.
   - **Investigate first.** For each company, check whether its careers site is backed by a JSON endpoint before writing a crawler.
   - **Crawling rules.** Respect `robots.txt` and rate limits, and test crawlers against saved HTML fixtures.
   - **Fit the existing pipeline.** Output must still be `Job` models so filter, score, and letter stay unchanged.
   - **Stay a CLI.** No scheduler or database (see below).

## Things Claude (the assistant) should not do here

- Do not add new ATS sources without a fixture and a test.
- Do not change scoring rubric wording in `score.py` without updating `tests/test_score.py` golden assertions.
- Do not "improve" the candidate's claims in `config/profile.md` or `config/kit/`. Ask.
- Do not add a web UI, database, or scheduler. This is a CLI. Scope creep is the enemy of a finished portfolio piece.
