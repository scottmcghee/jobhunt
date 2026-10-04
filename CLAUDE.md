# CLAUDE.md — jobhunt

This file is the constitution for AI-assisted work in this repo. Read it before touching code.

## What this is

A small, well-tested Python CLI that:

1. **Ingests** open roles from company career pages via public ATS JSON APIs (Greenhouse, Lever, Ashby).
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
  kit/                # Cover Letter Kit: opening.md, closing.md, modules/*.md
src/jobhunt/
  schema.py           # Job, ScoredJob, Letter models
  sources/            # one module per ATS; each exposes fetch(company) -> list[Job]
  filter.py           # pure: list[Job] x Preferences -> list[Job]
  score.py            # Claude call: Job x profile -> ScoredJob
  generate.py         # Claude call: ScoredJob x Kit -> Letter
  storage.py          # seen-set and 404 ledgers, JSONL append
  llm.py              # the ONLY module that talks to a model: Anthropic SDK or `claude -p` backend
  cli.py              # `jobhunt fetch | score | letter | run`
tests/
  fixtures/           # real-shaped ATS responses, anonymized
data/                 # runtime state (gitignored)
output/               # generated letters (gitignored)
```

## Workflow for any change

1. State the plan in one or two sentences before editing.
2. Add or update the test.
3. Implement the smallest change that passes.
4. `pytest -q`. Then `ruff check .` if available.
5. Summarize what changed and what was *not* changed.

## Conventions

- Python 3.11+. `httpx` for HTTP, `pydantic` v2 for models, `pyyaml` for config, `anthropic` SDK for Claude.
- Model and backend selection live in one place: `llm.py`. `JOBHUNT_BACKEND` picks `anthropic` or `claude-code`; `JOBHUNT_MODEL` overrides the model. Tests fake the `Completer`; never call a real backend in tests.
- Logging via `logging`, not `print`, except in `cli.py` output.
- Dates are ISO 8601 strings in UTC.

## Roadmap

Agreed future work, in rough priority order. Each item still follows the workflow above (fixture + test first).

1. **Prune dead boards automatically.** *(done)* `jobhunt fetch` counts consecutive HTTP 404s per board in `data/misses.json`. Any successful fetch resets the count; the third 404 in a row removes the entry from `config/companies.yaml`. `--dry-run` changes neither.
2. **Workday and SmartRecruiters sources.** Two new modules under `sources/`, each with a recorded fixture and `respx` tests, plus `ATSName` and `FETCHERS` entries.
   - SmartRecruiters has a public postings API keyed by company identifier.
   - Workday has no single public API. Each tenant's careers site serves JSON from its own host, so a Workday entry needs more than one slug (tenant, site, and data-center host). That means `Company` grows an optional field rather than overloading `slug`.
   - `jobhunt.slugs` should learn their URL shapes so Common Crawl harvesting covers them too.
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
