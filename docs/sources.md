# Job sources

This page covers each job board platform `jobhunt fetch` can read and how it reads it. It also
covers how fetching stays polite to the servers it calls. For setup and everyday use, see the
[README](../README.md).

- [How sources work](#how-sources-work)
- [At a glance](#at-a-glance)
- Listing sources: [Greenhouse](#greenhouse), [Lever](#lever), [Ashby](#ashby),
  [Workable](#workable)
- Listing sources with descriptions on demand: [Workday](#workday),
  [SmartRecruiters](#smartrecruiters), [BambooHR](#bamboohr)
- Search sources: [Amazon](#amazon), [Eightfold](#eightfold), [Oracle Recruiting Cloud](#oracle-recruiting-cloud),
  [Apple](#apple), [Phenom](#phenom)
- [Politeness](#politeness)

## How sources work

A **board** is one entry in `config/companies.yaml`: a display name, the platform (`ats`), and a
`slug` that identifies the board on that platform. Some platforms take one extra field. Every
board can also have `tags`, which the title filter can use (see
[`include_for_tags`](../config.example/preferences.yaml)).

```yaml
companies:
  - name: Figma
    ats: greenhouse
    slug: figma
    tags: [saas]
```

No source needs an account or an API key. Each one calls the public JSON endpoints that the
company's own careers page calls; Apple, the exception, embeds its data in its HTML pages. Every
source turns its postings into the same `Job` model, so the filter, scorer and letter writer
don't know or care where a posting came from.

Sources fall into three groups.

**Listing sources** return every open posting, with its description, in one request per board:
Greenhouse, Lever, Ashby and Workable.

**Listing sources with descriptions on demand** list every posting but leave out the
descriptions, so each description costs one more request: Workday, SmartRecruiters and BambooHR.
`fetch` asks for a description only when the posting's title passes the title filter in
`preferences.yaml`. Postings whose titles fail are rejected anyway, so nothing is lost.

**Search sources** are sites too big to list in full. Instead, `fetch` runs one search per
target title word (`title.must_include_any` in `preferences.yaml`, plus any `include_for_tags`
words for the board's tags) and merges the results: Amazon, Eightfold, Oracle, Apple and Phenom.
If you list no target words, it runs one unfiltered search. These searches match whole words
only, so a filter wildcard like `recruit*` is searched as just `recruit`, and `fetch` warns about
it. Each term brings in at most `fetch.max_per_term` postings for that source (set in
`settings.yaml`); a term that hits the cap is logged. Except for Amazon, a description is fetched only for postings
whose title passes the filter.

### Remote or not

Each source sets a posting's `remote` field to yes, no, or unknown. Most platforms only say so
clearly for some postings, so many come out unknown. The location filter decides what to do with
unknowns; see `unknown_remote_is_onsite` in the
[preferences template](../config.example/preferences.yaml). A common fallback below is
"remote if the location says *remote*, else unknown"; some sources also check the title.

### Dead boards

`fetch` counts, per board, how many fetches in a row found the board gone. The third in a row
removes the board from `config/companies.yaml` (`fetch.prune_after_404s` changes the number).
Any successful fetch resets the count, and `--dry-run` changes neither the count nor the file.
The counts live in `data/misses.json`.

"Gone" is an HTTP 404 for every source, plus the Workday and BambooHR cases noted below. A few
platforms answer an unknown board with an empty result instead of an error. Those boards are
never removed; `fetch` logs a warning for an empty board instead.

## At a glance

| `ats` | Slug | Descriptions | Unknown board | Rate group |
|---|---|---|---|---|
| `greenhouse` | board token | in the listing | 404, removed | `greenhouse` |
| `lever` | company name | in the listing | 404, removed | `lever` |
| `ashby` | board name | in the listing | 404, removed | `ashby` |
| `workable` | account | in the listing | 404, removed | `workable` (2 requests/s) |
| `workday` | `tenant/site` + `datacenter` | one request each | 404, 422 or 403 `S22`, removed | `workday:wdN` |
| `smartrecruiters` | company identifier | one request each | empty list, warned | `smartrecruiters` |
| `bamboohr` | tenant | one request each | redirect to bamboohr.com, removed | `bamboohr` |
| `amazon` | country code | in the results | empty results, warned | `amazon` |
| `eightfold` | careers host (+ optional `location`) | one request each | wrong domain: 404, removed; unknown `*.eightfold.ai` host: connection error, kept | the careers host |
| `oracle` | `host/site` | one request each | unknown site: the host's postings; unknown host: connection error, kept | the tenant host |
| `apple` | location filter | one page each | empty results, warned | `apple` (1 request/s) |
| `phenom` | `host/country/language`, or `host` | one request each | wrong locale: empty results, warned | the site's host |

The rate group is the queue a board's requests share; see [Politeness](#politeness).

## Greenhouse

```yaml
  - name: Huntress
    ats: greenhouse
    slug: huntress
```

- **Slug:** the token in `boards.greenhouse.io/<slug>` or `job-boards.greenhouse.io/<slug>`.
- **Endpoint:** `GET https://boards-api.greenhouse.io/v1/boards/<slug>/jobs?content=true`
  ([docs](https://developers.greenhouse.io/job-board.html)). One request returns every posting
  with its description.
- **Remote:** yes if the location or title says "remote", else unknown.
- **Unknown board:** 404, so it is removed after three fetches.
- **Note:** Greenhouse returns descriptions as HTML that is itself entity-escaped
  (`&lt;p&gt;`). `jobhunt` unescapes it before stripping the tags.

## Lever

```yaml
  - name: Shield AI
    ats: lever
    slug: shieldai
```

- **Slug:** the name in `jobs.lever.co/<slug>`.
- **Endpoint:** `GET https://api.lever.co/v0/postings/<slug>?mode=json`
  ([docs](https://github.com/lever/postings-api)). One request, descriptions included. The body
  is the description, each list section, and the closing text.
- **Remote:** from Lever's workplace type: remote is yes, on-site or hybrid is no. Without one,
  yes if the location says "remote", else unknown.
- **Unknown board:** 404, removed.

## Ashby

```yaml
  - name: Counterpart
    ats: ashby
    slug: counterpart
```

- **Slug:** the name in `jobs.ashbyhq.com/<slug>`. Ashby slugs are case-sensitive and may contain
  spaces.
- **Endpoint:** `GET https://api.ashbyhq.com/posting-api/job-board/<slug>?includeCompensation=true`
  ([docs](https://developers.ashbyhq.com/docs/public-job-posting-api)). One request, descriptions
  included. Postings marked unlisted are skipped.
- **Location:** the primary location plus any secondary ones.
- **Remote:** Ashby's `isRemote` flag when present, else unknown.
- **Unknown board:** 404, removed.

## Workable

```yaml
  - name: Hugging Face
    ats: workable
    slug: huggingface
```

- **Slug:** the account in `apply.workable.com/<account>` (older pages use
  `<account>.workable.com/jobs/...`).
- **Endpoint:** `GET https://apply.workable.com/api/v1/widget/accounts/<account>?details=true`,
  the one each account's careers page and embed widget call. One request, descriptions included.
- **Location:** every location the posting lists.
- **Remote:** yes if Workable's `telecommuting` flag is set. The flag is false for both on-site
  and hybrid roles, so false means unknown, unless the location or title says "remote".
- **Unknown board:** 404, removed.
- **Rate cap:** 2 requests a second. Workable's Cloudflare front end bans an IP for a minute or so
  after a burst of about 50 requests in 10 seconds.

## Workday

```yaml
  - name: Adobe
    ats: workday
    slug: adobe/external_experienced
    datacenter: wd5
```

- **Slug:** Workday has no documented public API. A careers site lives at
  `<tenant>.<datacenter>.myworkdayjobs.com/[<lang>/]<site>`. The slug is `tenant/site`, and the
  `datacenter` field (`wd1`, `wd5`, ...) is required. One tenant often has several sites; each is
  its own board.
- **Endpoints**, on the tenant's own host:
  - `POST /wday/cxs/<tenant>/<site>/jobs` lists postings, 20 a page (the API rejects more). Only
    the first page reports the total, so the rest of the pages go out concurrently once it is
    known.
  - `GET /wday/cxs/<tenant>/<site><externalPath>` returns one posting's description.
- **Descriptions:** one request each, only for postings whose title passes the filter.
- **Location:** the listing's location text, replaced by the primary and additional locations
  once the description is fetched.
- **Remote:** yes if the location says "remote", else unknown.
- **Unknown board:** a 404; a 422, which Workday sends for a site that was removed; or a 403 with
  error code `S22`, which it sends for a closed site. All three count toward removal. Any other
  403 with a Workday error code is treated as a problem with that board, not as the host pushing
  back.
- **Rate group:** one per datacenter (`workday:wd5`), since a datacenter's tenants share
  infrastructure.
- **Limits:** Workday reports a total of at most 2,000 postings, so a bigger site is read only
  that far.

## SmartRecruiters

```yaml
  - name: ServiceNow
    ats: smartrecruiters
    slug: ServiceNow
```

- **Slug:** the company identifier in `jobs.smartrecruiters.com/<identifier>/...` or
  `careers.smartrecruiters.com/<identifier>`. The API treats it as case-insensitive.
- **Endpoints** ([docs](https://developers.smartrecruiters.com/docs/posting-api)):
  - `GET https://api.smartrecruiters.com/v1/companies/<identifier>/postings?limit=100&offset=N`
    lists postings, 100 a page. Later pages go out concurrently.
  - `GET .../postings/<id>` returns one posting's description.
- **Descriptions:** one request each, only for postings whose title passes the filter. The body
  is the company description, job description, qualifications and additional information.
- **Remote:** SmartRecruiters' remote flag means yes and its hybrid flag means no. Both default
  to false, so on-site and unset look the same; for the rest, yes if the location says "remote",
  else unknown.
- **Unknown board:** an unknown identifier returns 200 with no postings, the same as a company
  with no open roles. These boards are never removed; `fetch` warns about any SmartRecruiters
  board with no postings.

## BambooHR

```yaml
  - name: Example Co
    ats: bamboohr
    slug: exampleco
```

- **Slug:** the tenant in `<tenant>.bamboohr.com/careers`.
- **Endpoints**, the ones each tenant's careers page calls:
  - `GET https://<tenant>.bamboohr.com/careers/list` lists every posting in one request.
  - `GET https://<tenant>.bamboohr.com/careers/<id>/detail` returns one posting's description.
- **Descriptions:** one request each, only for postings whose title passes the filter. A stated
  compensation is added to the body.
- **Location:** the office's city and state, else the remote region, else "Remote".
- **Remote:** BambooHR's location type: remote is yes, on-site or hybrid is no. Without it, yes
  if its `isRemote` flag is set, else unknown.
- **Unknown board:** an unknown tenant redirects to bamboohr.com rather than answering 404.
  `jobhunt` doesn't follow redirects here and counts that redirect toward removal.
- **Rate group:** every tenant has its own subdomain, but they are one service, so they share
  the `bamboohr` group.

## Amazon

```yaml
  - name: Amazon
    ats: amazon
    slug: USA
    tags: [big-tech]
```

Amazon runs its own job search at amazon.jobs rather than an ATS, and lists tens of thousands of
roles. It is a [search source](#how-sources-work).

- **Slug:** the country to search, as an ISO 3166 alpha-3 code (`USA`).
- **Endpoint:** `GET https://www.amazon.jobs/en/search.json?base_query=<term>&normalized_country_code[]=<slug>&result_limit=100&offset=N&sort=recent`,
  the one the site's search page calls. Its robots.txt disallows only the internal pages.
- **Descriptions:** included in the search results, so they cost nothing extra. The body is the
  description plus the basic and preferred qualifications.
- **Location:** every location the posting lists.
- **Remote:** yes if any location is virtual, no if every location is on-site, otherwise yes if
  the text says "virtual" or "remote", else unknown.
- **Cap:** 100 results a page, 2,000 per term by default. Amazon pages through at most 10,000
  results, so the cap can't go above 9,900. Results come in Amazon's "recent" order, which is
  roughly but not strictly by posting date, so a capped term keeps roughly the newest postings.
- **Unknown board:** a wrong country code returns no results rather than a 404. The board is
  never removed; `fetch` warns that it found nothing.

## Eightfold

```yaml
  - name: Microsoft
    ats: eightfold
    slug: apply.careers.microsoft.com
    location: United States
    tags: [big-tech]
```

Eightfold runs the careers sites of Microsoft, Nvidia, Eaton, PayPal and others. It is a
[search source](#how-sources-work).

- **Slug:** the careers site's host: `<tenant>.eightfold.ai`, or a company's own host such as
  `apply.careers.microsoft.com`. The optional `location` field limits every search to one place;
  no other source accepts it.
- **Endpoints**, on that host:
  - `GET /careers` is the careers page. It embeds the company's domain (`eaton.com`), which the
    API needs, so each board costs this one extra request per run.
  - `GET /api/pcsx/search?domain=...&query=<term>&start=N[&location=...]` searches, 10 results a
    page (fixed by the API).
  - `GET /api/pcsx/position_details?position_id=...&domain=...` returns one description.
  - Some tenants are still on Eightfold's older interface. There, `/api/pcsx` answers 403 with
    "PCSX is not enabled", and `fetch` switches to `/api/apply/v2/jobs` and
    `/api/apply/v2/jobs/<id>` for the rest of that board's run. Any other 403 is treated as a
    block.
- **Descriptions:** one request each, only for postings whose title passes the filter.
- **Remote:** from the posting's work-location option: remote (including Nvidia's
  "remote_local") is yes, on-site or hybrid is no, else unknown.
- **Cap:** 500 postings per term by default.
- **Unknown board:** an unknown domain on a real host is a 404, which counts toward removal. An
  unknown `*.eightfold.ai` host doesn't resolve at all. That is a connection error, not a 404, so
  it is never removed.
- **Rate group:** the careers host. Microsoft's is capped at one request every two seconds,
  because its site answered 429 to requests one second apart.
- **Finding boards:** the [slugs harvester](../README.md#finding-companies) recognizes
  `<tenant>.eightfold.ai/careers` URLs only. A company on its own host has to be added by hand.

## Oracle Recruiting Cloud

```yaml
  - name: Example Corp
    ats: oracle
    slug: eabc.fa.us2.oraclecloud.com/CX_1
```

Oracle Fusion HCM's "Candidate Experience" careers sites. It is a
[search source](#how-sources-work).

- **Slug:** `host/site`, read off a careers URL like
  `https://<host>/hcmUI/CandidateExperience/en/sites/<site>/job/123`.
- **Endpoints**, on the tenant's host (the hosts serve no robots.txt):
  - `GET /hcmRestApi/resources/latest/recruitingCEJobRequisitions?onlyData=true&expand=requisitionList.secondaryLocations&finder=findReqs;siteNumber=<site>,keyword=<term>,limit=200,offset=N`
    searches, 200 results a page.
  - `GET /hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails?onlyData=true&expand=all&finder=ById;Id="<id>",siteNumber=<site>`
    returns one description (its description, responsibilities and qualifications).
- **Descriptions:** one request each, only for postings whose title passes the filter.
- **Location:** the primary location plus any secondary ones.
- **Remote:** from the workplace type code: remote is yes, on-site or hybrid is no. Most postings
  leave it blank, which is unknown rather than on-site, unless the location or title says
  "remote".
- **Cap:** 1,000 postings per term by default. Keyword search matches descriptions too, so a
  broad term can return thousands.
- **One board per host:** the site only shapes the job URLs. The search returns the host's
  postings whatever the site, even a made-up one. Two boards on one host would fetch, score and
  write up every posting twice, so `fetch` warns when it sees that. The harvester also keeps one
  site per host.
- **Unknown board:** an unknown site on a real host returns that host's postings, as above. An
  unknown host doesn't answer; that is a connection error, not a 404, so it is never removed.
- **Rate group:** the tenant host, e.g. `eeho.fa.us2.oraclecloud.com`.

## Apple

```yaml
  - name: Apple
    ats: apple
    slug: united-states-USA
    tags: [big-tech]
```

Apple runs its own careers site, jobs.apple.com. It has no JSON API: each search and job page is
rendered on the server and embeds its data as JSON, which `jobhunt` reads. It is a
[search source](#how-sources-work).

- **Slug:** the location filter from a search URL, e.g. `united-states-USA` from
  `jobs.apple.com/en-us/search?location=united-states-USA`.
- **Pages** (jobs.apple.com serves no robots.txt):
  - `GET https://jobs.apple.com/en-us/search?location=<slug>&search=<term>&page=N`, 20 results
    a page.
  - `GET https://jobs.apple.com/en-us/details/<id>/<title-slug>` is one posting's page.
- **Search terms:** a multi-word term is sent as a quoted phrase. Unquoted, Apple matches any of
  the words, and "head of" alone finds over 4,000 postings.
- **Descriptions:** one page each, only for postings whose title passes the filter. The body is
  the summary, description, responsibilities, and minimum and preferred qualifications.
- **Remote:** yes if the posting is marked as a home-office role, else unknown.
- **Cap:** 400 postings per term by default.
- **Unknown board:** a wrong location filter returns nothing rather than a 404. The board is never
  removed; `fetch` warns that it found nothing.
- **Rate cap:** one page a second, since each page is about 300 KB.

## Phenom

```yaml
  - name: Example Corp
    ats: phenom
    slug: careers.example.com/us/en
```

Phenom is a careers-site platform, not an ATS: a company's Phenom site (often
`careers.<company>.com`) sits in front of its ATS, which is often Workday. Every Phenom site
answers the same public JSON endpoint on its own host, the one its search page calls. It is a
[search source](#how-sources-work).

- **Slug:** the site's host plus the country and language at the start of its URLs, e.g.
  `careers.adobe.com/us/en` or `careers.cisco.com/global/en`. A site whose URLs have no country
  and language, like `careers.davita.com`, is just its host; its API then takes `us/en`.
- **Endpoint**, on the site's host (robots.txt allows it):
  - `POST /widgets` with `{"ddoKey": "refineSearch", "keywords": "<term>", "from": N, ...}`
    searches, 100 results a page.
  - `POST /widgets` with `{"ddoKey": "jobDetail", "jobId": "<id>"}` returns one posting.
- **Descriptions:** one request each, only for postings whose title passes the filter.
- **Remote:** sites name the field differently (`RemoteType` in the search results, `remote` in a
  posting). Remote or yes is yes; on-site, hybrid or no is no; anything else is unknown, unless
  the location says "remote".
- **Cap:** 500 postings per term by default.
- **Same jobs twice:** a Phenom site and the ATS behind it list the same postings, under different
  keys, so a company with both boards gets every job fetched, scored and written up twice. Add
  only one. A posting's apply link shows which ATS is behind the site; the survey (below) uses it
  to give the ATS board instead when `jobhunt` supports that ATS.
- **Unknown board:** a wrong country or language finds nothing rather than a 404, so the board is
  never removed; `fetch` warns that it found nothing. An unknown host is a connection error.
- **Rate group:** the site's host.

## Politeness

Every request from `jobhunt fetch`, and from `python -m jobhunt.slugs --check`, goes through
one throttled HTTP transport. It identifies itself with the User-Agent
`jobhunt/0.1 (+personal job search tool)` (`fetch.user_agent`).

### Rate groups and per-host limits

Requests are grouped by the server that answers them: one group per Workday datacenter, per
Eightfold careers host, per Oracle tenant host and per Phenom site, and one per API host for
every other source.
The table [above](#at-a-glance) lists each group. `jobhunt -v fetch` prints per-group stats at
the end of a run: requests, throttles, peak concurrency and the current limit.

Each group has its own queue of boards and its own limit on requests in flight. The limit starts
at 2 (`fetch.start_per_host`). It grows by a little after each success, up to `--per-host`
(default 6). When the host answers 429 it halves, never below 1 and at most once every 5 seconds
(`fetch.cooldown`), so a burst of 429s from requests already in flight counts once. `--workers`
(default 32) caps requests in flight across all groups. Groups never wait on each other: a
datacenter with a thousand boards doesn't hold up the API hosts.

Within a board, Workday's and SmartRecruiters' later listing pages, and the description requests
of every source that fetches them separately, go out concurrently in that board's group, under
the same limits. Output still comes out in `companies.yaml` order.

### Rate caps

A group can also be held to a number of requests per second, `fetch.max_rate` in
`settings.yaml`. The defaults:

| Group | Cap | Why |
|---|---|---|
| `workable` | 2/s | Cloudflare bans an IP after about 50 requests in 10 seconds. |
| `apply.careers.microsoft.com` | 0.5/s | Microsoft's site answered 429 to requests one second apart. |
| `apple` | 1/s | Each page is about 300 KB. |

Keys are the group names `jobhunt -v fetch` prints, and are case-sensitive. Setting `max_rate`, in the
file or in `JOBHUNT_FETCH_MAX_RATE`, replaces the whole map, so copy these three into it to keep
them. `{}` removes every cap.

### Retries

Retries are per request. When one finally fails, a failed listing or search request skips that
board for this run, and a failed description request leaves just that posting without a
description (with a warning).

- **429, or 503 with Retry-After:** the request waits as long as Retry-After asks (or 1, 2, then
  4 seconds if it doesn't say, or asks for under a second) and is retried, up to 3 times
  (`fetch.max_retries`). A Retry-After over 2 minutes (`fetch.max_retry_after`) isn't waited
  out: the request fails and the group pauses for 2 minutes.
- **Transient failures** (a 500, 502 or 504, a connection error such as a failed DNS lookup, or a
  timeout): retried twice, after about 1 second and then 2 (`fetch.transient_retries`). A failed
  connection also gets one immediate retry at the connection level.
- Anything else, including a 404, is not retried.

### Circuit breaker

If five boards in a row in one group are refused (a 429 that outlasted its retries, or a 403 such
as a block page), the rest of that group's boards are skipped for this run, with a warning
(`fetch.breaker`). A 403 that is about one board, like the one Workday sends for a closed site,
doesn't count. To get through a strict host, lower `--workers` and `--per-host`;
`--workers 1 --per-host 1` is the gentlest setting.

### Interrupting

Ctrl-C stops sending requests, lets the ones already sent finish, and keeps every board that
finished, including those that finished ahead of their turn. The next run picks up the rest.

### Proxies

The transport uses the HTTPS proxy (else the ALL proxy) from the environment. `NO_PROXY` is not
honored.

### The S&P 500 survey

`scripts/survey_careers.py` doesn't use the transport above; it is slower on purpose. On
companies' sites it sends one request at a time, a second apart (`--delay`). It reads each
host's robots.txt and checks every URL against it, including each redirect hop. Following RFC 9309, a robots.txt that can't
be fetched or answers 5xx disallows the whole host. A few companies get no requests at all:
Meta, whose terms forbid automated collection, and Alphabet, whose robots.txt disallows its job
pages.

On a Phenom site it sends one search request (within robots.txt, like everything else) to read
where the postings apply. If they apply on a board `jobhunt` supports, such as Workday, it gives
that board; otherwise it gives the Phenom board.
