# jobhunt

[![CI](https://github.com/scottmcghee/jobhunt/actions/workflows/ci.yml/badge.svg)](https://github.com/scottmcghee/jobhunt/actions/workflows/ci.yml)

A small, tested Python CLI that watches company career pages, filters postings against hard constraints, scores the survivors against a fixed candidate profile with Claude, and assembles a tailored cover letter for the ones worth pursuing.

I built this during my own search for Director/VP infrastructure and platform roles. It is also a worked example of how I think engineering should be done with AI assistance in 2026: a written constitution (`CLAUDE.md`), tests before implementation, no network in the test suite, and a hard line between what the model is allowed to decide and what it isn't.

## The pipeline

```
companies.yaml ──► fetch ──► filter ──► score ──► letter
                   (ATS)     (rules)    (Claude)   (Kit + Claude)
                     │          │          │          │
                 seen.json  reasons    scores.jsonl  output/*.md
```

1. **Fetch.** Pulls open roles from the JSON APIs of five applicant-tracking systems — Greenhouse, Lever, Ashby, SmartRecruiters, and Workday — and normalizes them into one `Job` model. No scraping, no auth, no rate-limit games: Greenhouse, Lever, Ashby, and SmartRecruiters publish these endpoints for exactly this, and Workday's are the ones its own careers pages call. Workday and SmartRecruiters list postings without descriptions, so a description is fetched only for postings whose title passes the filter.
2. **Filter.** Deterministic rules from `config/preferences.yaml`: the title must contain one of your target words and none of your excluded ones, the posting must mention one of your domain keywords, and the location must be one you accept (or remote, if you allow it) and not one you reject. Every rejection carries a reason. This stage is pure and fully unit-tested, and it keeps the expensive stage cheap.
3. **Score.** Claude reads the posting and `config/profile.md` and returns 1–10 with a rationale, strengths, gaps, and two suggested proof modules. The rubric is explicit, treats the candidate's *known gaps* as facts, and caps the score at 5 when a posting's core requirement is one of them. The rubric is pinned by a golden test.
4. **Letter.** For roles at or above the threshold, the tool assembles a letter from a **Cover Letter Kit**: a fixed opening, two pre-written proof paragraphs chosen to match the posting, and a fixed closing. The model writes exactly two sentences — one proving the candidate read something specific about the company, one on fit — and nothing else. Structure is enforced in code, not in the prompt.

### Why the model only writes two sentences

Letting an LLM write a whole cover letter produces fluent, forgettable prose and, worse, drift: a "$4M cloud budget" quietly becomes "about $4 million," a "28-person org" becomes "nearly 30." Every factual claim in a letter from this tool was written by a human, once, in `config/kit/`. The model's job is selection and the two sentences that require reading the posting. That is the part a template can't do and the part that gets letters read.

Really, this should just be a starting point for you to get started on a letter. Tune the text to your voice!

## Install

```bash
git clone https://github.com/scottmcghee/jobhunt.git && cd jobhunt
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp -R config.example config      # your own copy; config/ is gitignored
```

## Pick a model backend

`llm.py` is the only module that talks to a model, and it supports three interchangeable backends:

| Backend | How it runs | Auth | Select with |
|---|---|---|---|
| `claude-code` | Shells out to the Claude Code CLI: `claude -p --tools "" --max-turns 1` | Your Claude subscription login (`claude auth login`) | Automatic when `claude` is on PATH and no API key is set |
| `anthropic` | Anthropic Python SDK | `ANTHROPIC_API_KEY` from the [Claude Console](https://platform.claude.com) (pay-as-you-go) | Automatic when `ANTHROPIC_API_KEY` is set |
| `bedrock` | Anthropic Python SDK's [Amazon Bedrock](https://platform.claude.com/docs/en/build-with-claude/claude-in-amazon-bedrock) client, billed to your AWS account | `AWS_REGION`, plus a Bedrock API key in `AWS_BEARER_TOKEN_BEDROCK` or your usual AWS credentials | `JOBHUNT_BACKEND=bedrock` only, never automatic |

Force one with `JOBHUNT_BACKEND=claude-code`, `anthropic`, or `bedrock`. Override the model with `JOBHUNT_MODEL`: an alias like `sonnet` for the CLI, a full model ID for the SDK, or a Bedrock model ID such as `anthropic.claude-opus-5-5` for Bedrock.

Both SDK backends default to Claude Sonnet 5.5 (`claude-sonnet-5-5`, or `anthropic.claude-sonnet-5-5` on Bedrock), with thinking turned off so its short answers fit their token limits. If a safety classifier declines a scoring request, that job is skipped with a warning naming the refusal category and retried on the next run. On the `anthropic` backend, [server-side fallback](https://platform.claude.com/docs/en/build-with-claude/refusals-and-fallback) (beta) first retries the request on the model Anthropic recommends for that category, and a warning names the model that answered.

For Bedrock, enable access to the model in the AWS console first. A Bedrock API key works with the base install. To sign with regular AWS credentials (environment variables, a profile, SSO, or a role), also install the AWS signing libraries:

```bash
pip install -e ".[bedrock]"
```

The CLI backend runs each call stateless, tool-less, and single-turn, so it behaves like a plain completion endpoint. Every score and letter records which backend and model produced it.

## Use

```bash
jobhunt fetch --dry-run          # what would be recorded, without recording it
jobhunt fetch -v                 # record new postings; -v shows why each rejected one was rejected
jobhunt score                    # score everything not yet scored
jobhunt list --min-score 7       # the shortlist
jobhunt letter                   # write letters for the shortlist into output/ (skips jobs that have one; --force rewrites)
jobhunt run                      # all of the above
```

Re-running `fetch` is idempotent; `data/seen.json` is the ledger. A board that returns HTTP 404 on three fetches in a row is removed from `config/companies.yaml` (counts live in `data/misses.json`; any successful fetch resets them). SmartRecruiters answers an unknown company with an empty list rather than a 404, so those boards are never removed; `fetch` logs a warning for any SmartRecruiters board with no postings instead.

## Configure

Everything personal lives in `config/`, which is gitignored and never committed. `config.example/` holds templates built around a fictional candidate; `cp -R config.example config` gives you a starting point.

| File | Purpose |
|---|---|
| `config/companies.yaml` | Company → ATS type + board slug. Comments explain how to find a slug. |
| `config/preferences.yaml` | The hard filters and the letter threshold. The template's lists are tuned for the fictional candidate; replace every one. |
| `config/profile.md` | The candidate narrative the scorer reads. Facts here are fixed. |
| `config/kit/` | Opening, closing, and proof modules with `use_when` keywords. |

To adapt this for yourself: rewrite `profile.md` and the Kit in your own voice, edit the filters, and point `companies.yaml` at the boards you care about. Nothing in `src/` is specific to one candidate.

### Finding boards

The comments in `companies.yaml` show how to read a board slug off a careers page URL. To collect many at once, give the harvester any text file containing board URLs, such as lines grepped from [Common Crawl](https://index.commoncrawl.org/) index files:

```bash
python -m jobhunt.slugs urls.txt --companies config/companies.yaml --check
```

It writes the boards it finds to `data/companies.generated.yaml`, ready to review and paste into `companies.yaml`, leaving out any already listed there. `--check` fetches the first page of each new board and drops those with no open postings; without it, the harvester makes no network calls.

## Develop

```bash
pytest          # all offline: ATS responses from tests/fixtures/, model calls faked, config from config.example/
ruff check .
```

Read `CLAUDE.md` first. It is the contract for anyone — human or model — changing this code.

In Claude Code, `/pr-review-loop <PR#>` reviews a pull request with a skeptical reviewer agent, has a fixer agent fix and push what the review proves wrong, and repeats until a round finds nothing (at most 3), posting each round's summary on the PR. The agents and the skill are in `.claude/`.

## Design notes

- **Single source of truth for the model call.** `llm.py` is the only module that knows about the Anthropic SDK, Bedrock, or the Claude Code CLI. Everything else takes a `Completer` callable, so tests inject a fake and never need a key or a subprocess.
- **Pydantic at the boundaries.** `Job`, `Score`, `ScoredJob`, `Letter`. A malformed ATS response or an out-of-range score fails loudly at the edge.
- **Fixtures over mocks.** `tests/fixtures/` holds real-shaped responses from each ATS (anonymized), including Greenhouse's habit of returning HTML that is itself entity-escaped — a bug the fixture caught on the first run.
- **Boring storage.** JSON and JSONL on disk. The whole state is `data/`; delete it to start over.

## Not in scope, on purpose

No web UI, no database, no scheduler, no auto-apply. Those would be more code to maintain for no more signal. A cron entry running `jobhunt run` covers the scheduling case.

## License

MIT
