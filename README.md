# jobhunt

[![CI](https://github.com/scottmcghee/jobhunt/actions/workflows/ci.yml/badge.svg)](https://github.com/scottmcghee/jobhunt/actions/workflows/ci.yml)

jobhunt is a small Python CLI for a job search. It pulls open roles from company job boards,
drops the ones that fail your hard requirements, has Claude score the rest against your profile,
and drafts cover letters for the best matches from paragraphs you wrote yourself.

I built it for my own search. It is also a worked example of how I like to build software with AI
assistance: a written constitution ([CLAUDE.md](CLAUDE.md)), tests first, no network in the test
suite, and a hard line between what the model may decide and what it may not.

## Quick start

You need Python 3.11 or later.

```bash
git clone https://github.com/scottmcghee/jobhunt.git && cd jobhunt
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp -R config.example config      # your own copy; config/ is gitignored
```

The templates in `config.example/` describe a fictional candidate, Jordan Example. They work as
they are, so you can try the tool before making it yours.

Next, give it a way to reach Claude. Pick one backend:

| Backend | How it runs | What you need |
|---|---|---|
| `claude-code` | Shells out to the Claude Code CLI (`claude -p`), stateless and with no tools | A Claude subscription and `claude auth login` |
| `anthropic` | The Anthropic Python SDK | `ANTHROPIC_API_KEY` from the [Claude Console](https://platform.claude.com) (pay as you go) |
| `bedrock` | The Anthropic SDK's [Amazon Bedrock](https://platform.claude.com/docs/en/build-with-claude/claude-in-amazon-bedrock) client, billed to your AWS account | `AWS_REGION`, plus a Bedrock API key in `AWS_BEARER_TOKEN_BEDROCK` or your usual AWS credentials |

If you don't choose, jobhunt uses `anthropic` when `ANTHROPIC_API_KEY` is set, and otherwise
`claude-code` when `claude` is on your PATH. Bedrock is never picked automatically: set
`JOBHUNT_BACKEND=bedrock`, and enable access to the model in the AWS console first. There is more
on backends and models under [Tuning](#tuning).

Then run it:

```bash
jobhunt fetch --dry-run     # see what would be recorded, without recording it
jobhunt run                 # fetch, score, and write letters
jobhunt list --min-score 7  # the shortlist
```

Letters land in `output/`. Everything else (postings, scores, bookkeeping) is in `data/`;
delete it to start over.

## How it works

```
companies.yaml ──► fetch ──► filter ──► score ──► letter
                  (boards)   (rules)   (Claude)  (Kit + Claude)
                                │          │          │
                          jobs.jsonl  scores.jsonl  output/*.md
```

### 1. Fetch

`jobhunt fetch` reads every board in `config/companies.yaml`. It supports ten applicant-tracking
systems (Greenhouse, Lever, Ashby, Workable, Gem, Rippling, Workday, SmartRecruiters, BambooHR and
SuccessFactors) and iCIMS's branded careers sites,
plus Amazon, Apple, Eightfold, Oracle Recruiting Cloud and Phenom sites and USAJOBS (federal jobs;
needs a free API key), which are too big to list and are searched by your target title words
instead, and Radancy and Paradox careers sites, read
through their sitemaps. It calls the public endpoints and pages the companies' own careers sites
use, within their robots.txt, so no accounts are needed, except USAJOBS's free API key.

New postings that pass the filter are appended to `data/jobs.jsonl`. `data/seen.json` remembers
what was already recorded, so running `fetch` again never adds a duplicate. A board found gone
three fetches in a row is removed from `companies.yaml`.

Fetching is polite: small, adaptive per-host limits, rate caps where a host needs them, and
patient retries. [docs/sources.md](docs/sources.md) has the details for each platform.

### 2. Filter

The filter applies the rules in `config/preferences.yaml` during the fetch, so only postings that
pass are recorded. A posting must pass three checks:

- **Title:** it contains one of your target words (such as "director") and none of your excluded
  words. Boards can carry extra target words by tag, so in the template a board tagged `big-tech`
  also accepts "manager".
- **Domain:** the title or description mentions one of your domain keywords.
- **Location:** it is remote (if you allow that) or in a place you accept, and not in a place you
  reject. You can keep a separate, stricter list for on-site and hybrid roles.

Terms match whole words, ignoring case, and a trailing `*` matches any ending. Every rejection
carries a reason, which `jobhunt -v fetch` prints. This stage is pure, fully unit-tested code with
no model calls, and it keeps the expensive scoring step small.

### 3. Score

`jobhunt score` sends each unscored posting to Claude with `config/profile.md`. Claude returns a
score from 1 to 10, a short rationale, strengths, gaps, and the two Kit paragraphs that best fit
the posting. The rubric is fixed in code and pinned by a golden test. It treats the profile's
"Known gaps" as facts, and caps the score at 5 when a posting's core requirement is one of them.
Scores go to `data/scores.jsonl`, each labeled with the backend and model that produced it.

### 4. Letter

`jobhunt letter` writes a letter for every job scoring at or above the threshold in
`preferences.yaml` (7 in the template). The letter is assembled from your **Cover Letter Kit** in
`config/kit/`: a fixed opening, the two proof paragraphs the scorer picked, and a fixed closing. The model
writes exactly two sentences: one showing it read something specific in the posting, and one on
why the role fits. (If `companies.yaml` names a company only by its slug, the model also copies
the proper name from the posting, and code checks that the posting contains it.) Code, not the
prompt, enforces this structure.

Why so little? A model that writes a whole letter produces fluent, forgettable prose, and it
drifts: a "$4M cloud budget" becomes "about $4 million". Here every factual claim in a letter was
written once, by a person, in the Kit. The model's job is choosing, plus the two sentences that
need the posting. Treat the result as a first draft and tune it to your voice.

### 5. Track applications

When you apply to a job jobhunt found, record it, and record what comes of it:

```bash
jobhunt applied greenhouse:northwind:4410 --resume v2 --warm   # a referral, resume v2
jobhunt outcome greenhouse:northwind:4410 screen
jobhunt applications
```

`--warm` marks a warm contact (a referral or an intro), and `--date` (default today, in UTC)
backdates either command; `applied --force` corrects an application and keeps its original date
unless `--date` is given. An outcome is one of `no_response`, `rejected`, `screen`, `interview`, `offer`
or `withdrawn`, and the latest one is where the application stands. `jobhunt applications` lists
them, then breaks them down by resume version, warm or cold, and the score jobhunt gave the job,
so questions like "did the new resume get more replies?" have numbers behind them (a fictional
example):

```
2026-09-20  rejected     8/10  v1       cold  Northwind        Director of Platform Engineering
2026-09-22  no_response  7/10  v1       cold  Contoso          Director, Developer Experience
2026-10-01  interview    8/10  v2       warm  Fabrikam         VP, Infrastructure
2026-10-02  pending      7/10  v2       cold  Tailspin         Director of SRE

by resume:
             applied  replied  screen+ interview+ offer days to reply
  v1               2        1        0          0     0            10
  v2               2        1        1          1     0             3
```

"Replied" counts any answer, a rejection included; "days to reply" is the median from applying
to the first one. Everything is appended to `data/applications.jsonl` (a correction is a newer
entry), which stays on your machine. `jobhunt list --hide-applied` leaves out jobs you've
applied to.

### 6. Check the scorer

How far can you trust the scores? Score a sample of jobs yourself, then compare:

```bash
jobhunt label            # one job at a time: your own score, 1 to 10, and an optional note
jobhunt eval             # how well the scorer agrees with you
jobhunt eval --rescore   # the same, with the labeled jobs scored afresh by the current prompt
```

`jobhunt label` builds up 50 labels (`--sample N` for another number), and you can stop and come
back. It shows each job's title, location, link and the start of its description, but not the
scorer's score, so it can't sway you. The sample is drawn from each score band, and shown in
mixed order so a job's place in line gives nothing away: most jobs score 1 or 2, and a random
sample would say little about the ones near the letter line.

`jobhunt eval` reports rank agreement, the average gap and which way the scorer leans, and the
decision that matters most, a letter or not: of the jobs you'd write to, how many the scorer
found, and how many of its picks you agree with. It ends with the biggest disagreements and the
scorer's reasoning for each, which is where to look when changing the prompt. Every run is
appended to `data/evals.jsonl`; with `--rescore` it records a fingerprint of the prompt, so runs
before and after a prompt change can be compared. Rescoring costs one model call per labeled
job and leaves your stored scores alone.

## Configuration

Everything personal lives in `config/`, which is gitignored. Copy `config.example/` to start; its
comments explain each setting.

| File | What it's for |
|---|---|
| `companies.yaml` | The boards to fetch: a name, the platform (`ats`), the board's `slug`, and optional `tags`. The comments show how to read a slug off a careers page URL. |
| `preferences.yaml` | The filter rules (title, domain, location) and the score a job needs to get a letter. The template's lists suit the fictional candidate; replace them all. |
| `profile.md` | The candidate narrative the scorer reads. Keep the `Target` and `Known gaps` headings; the prompts refer to them by name. |
| `kit/` | The Cover Letter Kit. `opening.md` and `closing.md` hold placeholders the model fills. Each file in `modules/` is one proof paragraph, with an `id`, a `title`, and `use_when` keywords. |
| `settings.yaml` | Optional knobs: model and backend, token budgets, fetch concurrency, timeouts, retries, pruning, and where data and letters go. See [Tuning](#tuning). |

To make it yours, rewrite `profile.md` and the Kit in your own words, edit the filters, and point
`companies.yaml` at the boards you care about. Nothing in `src/` is specific to one candidate.

## Commands

| Command | What it does | Common flags |
|---|---|---|
| `jobhunt fetch` | Pull postings, filter them, record the new ones | `--company NAME`, `--dry-run`, `--resume`, `--workers N`, `--per-host N` |
| `jobhunt score` | Score every recorded job that has no score yet | `--limit N`, `--rescore` |
| `jobhunt list` | Show scored jobs, best first, with their keys | `--min-score N`, `--hide-applied` |
| `jobhunt letter` | Write letters for high scorers that don't have one | `--min-score N`, `--job KEY`, `--force` |
| `jobhunt run` | `fetch`, then `score`, then `letter` | any of the above |
| `jobhunt applied KEY` | Record an application to a job jobhunt found | `--resume VERSION` (required), `--warm`, `--date`, `--force` |
| `jobhunt outcome KEY STATUS` | Record what came of it | `--date` |
| `jobhunt applications` | Each application, and reply rates by resume, warm or cold, and score | |
| `jobhunt label` | Score a sample of jobs yourself | `--sample N` |
| `jobhunt eval` | How well the scorer agrees with your labels | `--rescore` |

- `--company NAME` fetches every board whose `name` in `companies.yaml` matches, ignoring case.
  Harvested Workday sites of one tenant share a name, so `--company adobe` fetches all of
  Adobe's sites. With `--dry-run` it's a quick way to check a new slug.
- `--dry-run` records nothing and removes no boards. In `run`, it applies to the fetch step only;
  scoring and letters still run for jobs recorded earlier.
- `--resume` fetches only the boards an interrupted fetch didn't finish (see below). It can't be
  combined with `--company`.
- `--workers` (default 32) caps requests in flight overall, and `--per-host` (default 6) caps
  them per host. `--workers 1 --per-host 1` is the gentlest setting.
- `--job KEY` writes a letter for one job whatever its score. A key looks like
  `greenhouse:huntress:7777533003`; `jobhunt list` shows them.
- `--force` rewrites letters that already exist.

A few options go *before* the command: `-v` (log progress and print why each posting was
rejected), `--companies PATH`, `--data-dir PATH` and `--output-dir PATH`. For example:
`jobhunt -v fetch --dry-run`.

`jobhunt <command> --help` lists everything.

A fetch saves each board's new jobs as soon as that board is done, so stopping it never loses
them: Ctrl-C, closing the terminal (or VS Code), `kill`, even a crash. The boards it finished
are listed in `data/fetch_progress.txt`, and `jobhunt fetch --resume` fetches only the rest. A
plain `jobhunt fetch` starts over with every board; a completed fetch deletes the file.

## Finding companies

The comments in `companies.yaml` show how to read a board's slug off its careers page. Three tools
help find boards in bulk. Each writes a file for you to review and paste into `companies.yaml`, and
each leaves out boards you already have.

**Discovery** finds boards two ways at once, and is the one to start with:

```bash
python -m jobhunt.discover                                  # every known platform, latest crawl
python -m jobhunt.discover --hosts careers_hosts.txt        # and these companies' careers sites
python -m jobhunt.discover --platforms gem rippling --check # just these; drop boards with no jobs
```

On platforms whose board URLs follow a pattern (Workday, Greenhouse, Lever, Ashby,
SmartRecruiters, Workable, BambooHR, Eightfold, Gem, Rippling; Oracle with `--platforms oracle`),
it reads [Common Crawl](https://commoncrawl.org/)'s URL index: each platform's URLs sit together,
so about 100 MB of index (cached after the first run) plus some tens of MB of index blocks per
crawl finds every board a crawl saw (5,749 new ones in a test run). Careers
sites on companies' own domains (`careers.acme.com`) say nothing in their URLs, so each host in a
`--hosts` file (plain hosts, URLs, or lines grepped from Common Crawl's `cluster.idx`) is read the
way the S&P 500 survey reads a site: which platform it runs, and the board it points at. Hosts are
cached in `data/discovery/hosts.json`, so a rerun visits only new ones; a host that didn't
answer is tried again on a later run, a day or more on, three tries in all (`discover` in
settings.yaml changes both; `--refresh` visits
every host again). It writes `data/discovered.yaml`; boards from careers hosts are named after the
host, so fix names as you paste. With `--check`, the unchecked list is written first, so stopping
the check with Ctrl-C still leaves it.

**The slugs harvester** finds board URLs in any text file, such as lines grepped from
[Common Crawl](https://index.commoncrawl.org/) index files, saved HTML, or a plain list of URLs:

```bash
python -m jobhunt.slugs urls.txt --check
```

It writes `data/companies.generated.yaml`. Without `--check` it makes no network calls. With it,
it fetches the first page of each new board and drops boards that no longer exist or have no open
postings.

**The S&P 500 survey** looks up each company in the index, finds its careers pages, and records
which hiring platform it uses:

```bash
python scripts/survey_careers.py              # --limit N or --only TICKER ... to narrow it
```

Boards on a supported platform go to `data/sp500/companies.generated.yaml` under the company's
real name, and `data/sp500/survey.md` has a table of every company and its platform. It sends one
request a second and honours robots.txt, so a full run takes well over an hour. It saves as it
goes, and a rerun picks up where it stopped.

**Tagging boards.** Filter rules can target groups of boards by tag (see `include_for_tags` in
`preferences.yaml`). The tagging script keeps a fixed set of tags up to date in `companies.yaml`:
`big-tech`, `sp500` (from the survey), and an industry such as `healthcare` or `fintech`, chosen
by the model from each board's name and a few of its job titles:

```bash
python scripts/tag_companies.py --dry-run     # report what would change
python scripts/tag_companies.py               # write it; --no-llm uses cached answers only
```

Answers are cached in `data/company_tags.json`, so a rerun only asks about new boards. Tags
outside the fixed set, such as `seattle`, are left as written, and so are the file's comments and
layout.

## Tuning

`config/settings.yaml` holds every tunable setting. All of them are optional, and
[config.example/settings.yaml](config.example/settings.yaml) lists each one at its default with a
comment saying what it does. The sections are:

- `llm`: backend, model, token budgets, and how much of a posting the model sees.
- `fetch`: concurrency, timeouts, retries, the circuit breaker, dead-board pruning, per-host rate
  caps, and how many postings each search term may bring in.
- `paths`: where `data/` and `output/` live.
- `slugs`: threads for `jobhunt.slugs --check`.

Any setting can be overridden for one run with an environment variable named
`JOBHUNT_<SECTION>_<KEY>`, for example `JOBHUNT_FETCH_WORKERS=16 jobhunt fetch`. Map-valued
settings take JSON. A CLI flag beats the environment, which beats `settings.yaml`, which beats the
built-in default. A bad value stops the command with a message saying where it came from.

**Models.** Both SDK backends default to Claude Sonnet 5.5 (`claude-sonnet-5-5`, or
`anthropic.claude-sonnet-5-5` on Bedrock), and `claude-code` defaults to the `sonnet` alias. Set
`JOBHUNT_MODEL` (or `llm.model`) to change it, and `JOBHUNT_BACKEND` (or `llm.backend`) to force
a backend. Bedrock with regular AWS credentials, rather than a Bedrock API key, needs
`pip install -e ".[bedrock]"`. If Claude declines a request, that job is skipped with a warning
and retried on the next run; on the `anthropic` backend,
[server-side fallback](https://platform.claude.com/docs/en/build-with-claude/refusals-and-fallback)
first retries it on the model Anthropic recommends.

**Fetch speed.** To see how your own boards respond, `python scripts/bench_fetch.py sample` writes
a 150-board sample that keeps your mix of hosts, and `python scripts/bench_fetch.py run --per-host N`
times a dry-run fetch of it.

## Development

```bash
pytest          # offline: recorded responses, faked model calls, config from config.example/
ruff check .
```

Platform responses come from anonymized fixtures in `tests/fixtures/`. CI runs `ruff`, and the
tests on Python 3.11 and 3.14.

Read [CLAUDE.md](CLAUDE.md) before changing anything. It is the contract for anyone, human or
model, working on this code.

In Claude Code, `/pr-review-loop <PR#>` reviews a pull request with a skeptical reviewer agent,
has a fixer agent fix and push what the review confirms, and repeats until a round finds nothing
(at most three rounds), posting each round's summary on the PR. The agents and the skill are in
`.claude/`.

## Design notes

- **One place talks to the model.** `llm.py` is the only module that knows about the Anthropic
  SDK, Bedrock or the Claude Code CLI. Everything else takes a plain function, so tests pass in a
  fake and never need a key or a subprocess.
- **Pydantic at the boundaries.** `Job`, `Score`, `ScoredJob` and `Letter` are validated models,
  so a malformed response or an out-of-range score fails loudly at the edge.
- **Fixtures over mocks.** `tests/fixtures/` holds real-shaped responses from each platform. On
  its first run, one caught Greenhouse's habit of returning HTML that is itself entity-escaped.
- **Boring storage.** JSON and JSONL files on disk. The whole state is `data/`.

## Not in scope, on purpose

No web UI, no database, no scheduler, no auto-apply. They would be more code to maintain for no
more signal. A cron entry running `jobhunt run` covers scheduling.

## More documentation

- [docs/sources.md](docs/sources.md): each platform's endpoints, slug format, remote detection,
  dead-board handling and limits, plus how fetching stays polite.
- [config.example/](config.example/): the templates, with comments on every setting.
- [CLAUDE.md](CLAUDE.md): how changes to this repo are made.

## License

MIT
