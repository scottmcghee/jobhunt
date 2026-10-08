# Job sources

This page covers each job board platform `jobhunt fetch` can read and how it reads it. It also
covers how fetching stays polite to the servers it calls. For setup and everyday use, see the
[README](../README.md).

- [How sources work](#how-sources-work)
- [At a glance](#at-a-glance)
- Listing sources: [Greenhouse](#greenhouse), [Lever](#lever), [Ashby](#ashby),
  [Workable](#workable), [Gem](#gem), [iCIMS Career Sites](#icims-career-sites)
- Listing sources with descriptions on demand: [Workday](#workday),
  [SmartRecruiters](#smartrecruiters), [BambooHR](#bamboohr), [Rippling](#rippling),
  [SuccessFactors](#successfactors-career-site-builder), [Radancy and Paradox](#radancy-and-paradox)
- Search sources: [Amazon](#amazon), [Eightfold](#eightfold), [Oracle Recruiting Cloud](#oracle-recruiting-cloud),
  [Apple](#apple), [Phenom](#phenom), [USAJOBS](#usajobs)
- [Politeness](#politeness)
- [Not covered: watch these by hand](#not-covered-watch-these-by-hand)

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
Greenhouse, Lever, Ashby, Workable and Gem. iCIMS Career Sites is a listing source too, but paged: one
request per 100 postings.

**Listing sources with descriptions on demand** list every posting but leave out the
descriptions, so each description costs one more request: Workday, SmartRecruiters, BambooHR,
Rippling, and SuccessFactors sites without a full feed, Radancy and Paradox.
`fetch` asks for a description only when the posting's title passes the title filter in
`preferences.yaml`. Postings whose titles fail are rejected anyway, so nothing is lost.

**Search sources** are sites too big to list in full. Instead, `fetch` runs one search per
target title word (`title.must_include_any` in `preferences.yaml`, plus any `include_for_tags`
words for the board's tags) and merges the results: Amazon, Eightfold, Oracle, Apple, Phenom and
USAJOBS.
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
| `workable` | account | in the listing | 404, removed | `workable` (1.4 requests/s) |
| `gem` | board name | in the listing | 404, removed | `gem` |
| `icims_careers` | careers site host | in the listing | 404, removed; unknown host: connection error, kept | the careers host (0.2 requests/s) |
| `workday` | `tenant/site` + `datacenter` | one request each | 404, 422 or 403 `S22`, removed | `workday:wdN` |
| `smartrecruiters` | company identifier | one request each | empty list, warned | `smartrecruiters` |
| `rippling` | board name | one request each | 404, removed | `rippling` |
| `bamboohr` | tenant | one request each | redirect to bamboohr.com, removed | `bamboohr` |
| `radancy` | careers site host | one page each | no sitemap: 404, removed; no job URLs: empty, warned | the careers host |
| `paradox` | careers site host | one page each | no sitemap: 404, removed; no job URLs: empty, warned | the careers host |
| `successfactors` | careers site host | in the feed, or one page each | no sitemap: 404, removed; not Career Site Builder: empty, warned | the careers host |
| `amazon` | country code | in the results | empty results, warned | `amazon` |
| `eightfold` | careers host (+ optional `location`) | one request each | wrong domain: 404, removed; unknown `*.eightfold.ai` host: connection error, kept | `eightfold` (2 requests/s) for `*.eightfold.ai`; else the careers host |
| `oracle` | `host/site` | one request each | unknown site: the host's postings; unknown host: connection error, kept | the tenant host |
| `apple` | location filter | one page each | empty results, warned | `apple` (1 request/s) |
| `phenom` | `host/country/language`, or `host` | one request each | wrong locale: empty results, warned | the site's host |
| `usajobs` | a place (`City, State`, optionally `/radius`), or `remote` | in the results | no key or email: skipped, warned | `usajobs` (1 request/s) |

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
- **Rate cap:** 1.4 requests a second. Workable's Cloudflare front end bans an IP for a minute or
  so after a burst of about 50 requests in 10 seconds. It also has a longer-window limit: at a
  steady 1.9 a second, 429s began after 900 to 1,250 requests, and in a full fetch enough of
  them tripped the circuit breaker, skipping about 1,900 boards.

## Gem

```yaml
  - name: Gem
    ats: gem
    slug: gem
```

- **Slug:** the board name in `jobs.gem.com/<slug>`; case is kept.
- **Endpoint:** `GET https://api.gem.com/job_board/v0/<slug>/job_posts/`, Gem's documented Job
  Board API. One request, descriptions included, in much the shape of Greenhouse's.
- **Remote:** from Gem's `location_type`: `remote` is yes, `hybrid` and `in_office` are no; otherwise yes
  only if the location or title says "remote".
- **Unknown board:** 404, removed.

## iCIMS Career Sites

```yaml
  - name: AMD
    ats: icims_careers
    slug: careers.amd.com
```

The branded careers sites iCIMS hosts on a company's own domain (formerly Jibe): AMD, Aon,
Keysight, S&P Global, Paychex and others. Not the classic `careers-<company>.icims.com` portals,
whose robots.txt disallows everything. A site's pages load scripts from `jibecdn.com`.

- **Slug:** the site's host.
- **Endpoint:** `GET https://<host>/api/jobs?page=N&limit=100`, the one the site's own search
  page calls; `N` counts from 1, and the reply reports `totalCount`. Every page is read until that
  total (at most 100 pages), descriptions included: the description, responsibilities and
  qualifications.
- **Job link:** `https://<host>/jobs/<id>`, which the site redirects to its own path.
- **Remote:** yes if the location or title says "remote"; otherwise unknown.
- **Unknown board:** 404, removed; a host that doesn't resolve is a connection error, and kept.
- **Rate cap:** 0.2 requests a second per site: their robots.txt allows everything but asks for
  a 5-second crawl delay. The source sends the cap with its requests, since a company's own domain
  doesn't say which platform it runs; a `fetch.max_rate` entry for the host overrides it.

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

## Rippling

```yaml
  - name: Rippling
    ats: rippling
    slug: rippling
```

- **Slug:** the board name in `ats.rippling.com/<slug>`; case is kept.
- **Endpoints:** `GET https://api.rippling.com/platform/api/ats/v1/board/<slug>/jobs` lists every
  posting without descriptions, and `.../jobs/<uuid>` gives one posting's description (the role,
  then the company blurb) and its creation date, asked for only when the title passes the filter.
- **Location:** the listing has one entry per location, merged into one posting; a fetched
  posting's own list of locations replaces them.
- **Remote:** yes if a location or the title says "remote" (Rippling writes "Remote (United
  States)"); otherwise unknown.
- **Unknown board:** 404, removed.

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

## SuccessFactors (Career Site Builder)

```yaml
  - name: Ball
    ats: successfactors
    slug: jobs.ball.com
```

SAP SuccessFactors careers sites built with Career Site Builder, often `jobs.<company>.com` or
`careers.<company>.com`. Their pages load scripts from `/platform/js/j2w/` or `/platform/csb`. There is no public JSON
API, and robots.txt disallows `/services/`, the paths their feeds and search use. `jobhunt` reads
what the sites allow: the sitemap, and job pages.

- **Slug:** the careers site's host, e.g. `jobs.ball.com`.
- **Pages:**
  - `GET /robots.txt` first. If it disallows the sitemap, the board is skipped with a warning; if
    it disallows job pages, postings are kept without descriptions. Following RFC 9309, a
    robots.txt that answers 5xx disallows everything.
  - `GET /sitemap.xml` comes in two kinds. Some sites serve an RSS feed of every posting with its
    description (25 to 30 MB for the biggest), so the whole board is one request. Most serve a
    plain sitemap of job page URLs.
  - `GET /job/<title-and-place>/<id>/` (some sites put a brand first: `/<brand>/job/...`) is one
    posting's page. Job URLs on another host are ignored. Redirects (of the sitemap or a page)
    are followed on the site's host only, each hop checked against robots.txt; a page that
    redirects elsewhere is kept without a description.
- **Descriptions:** in the feed, or one page each for postings that pass the title filter. A job
  URL's words (`Richmond Senior Manager VA 23230`) stand in for the title in that check. The title
  is one run of those words, so a page is fetched if any run passes, with `_` read as `.` (the
  URLs write `Sr.` as `Sr_`) and words joined by a dropped `/` (`ManagerDirector`, `VPDirector`,
  `SVPGM`) split apart. That fetches some pages whose title then fails the filter. It can still
  skip one if the URL joined two lowercase words, or joined all-caps words more than once in a
  title or into one longer than 8 letters. A page's
  schema.org microdata gives the real title, location, date posted and description. A posting
  that passed but has no page (robots.txt disallows it, or the page failed) gets as its title the
  reading of its URL words that passed (`Seattle Sr. Manager, ...`, `Seattle VP Director, ...`),
  so the final filter agrees; postings that didn't pass keep the URL's words.
- **Location:** the feed's location, or the page's address. A page without one gets the URL's
  words before and after the title, matched word by word and ignoring punctuation
  (`Richmond, VA 23230`); if the title isn't in the URL, all of its words. Either way `_` reads
  as `.` (`St_ Louis` is `St. Louis`).
- **Remote:** yes if the location or title says "remote", else unknown.
- **Unknown board:** a host with no sitemap answers 404 and is removed. A site that isn't Career
  Site Builder (careers.netapp.com mentions SuccessFactors but runs another platform) lists no job
  URLs in this shape; it is never removed, and `fetch` warns that it found nothing.
- **Rate group:** the careers host.

## Radancy and Paradox

```yaml
  - name: L3Harris
    ats: radancy
    slug: careers.l3harris.com

  - name: ADP
    ats: paradox
    slug: jobs.adp.com
```

Radancy (TalentBrew) and Paradox build careers sites, usually in front of an ATS. Radancy's
search (`/search-jobs/`) is disallowed by robots.txt. Paradox is best known for its "Olivia" chat
assistant, which many sites on other platforms embed; only a company's own Paradox careers site
is a `paradox` board. Both kinds of site allow what `jobhunt` reads: their sitemaps, and job pages
that carry a schema.org `JobPosting` as JSON-LD. One module does the work for both; they differ
only in their job URLs.

- **Slug:** the careers site's host.
- **Pages:**
  - `GET /robots.txt` first. Job pages it disallows are kept without descriptions; a disallowed
    sitemap is an error. Following RFC 9309, a robots.txt that answers 5xx disallows everything.
  - The sitemaps robots.txt names on the site's host, else `/sitemap.xml`. Sitemap index files
    are followed (FedEx splits its jobs into 31 files), up to 50 files a board, and so are
    redirects on the same host (L3Harris's `/sitemap.xml` moved to `/en/sitemap.xml`), each hop
    checked against robots.txt; job pages' redirects too. A sitemap or page that redirects off
    the host isn't followed, and job URLs on another host are ignored. A sitemap that fails is
    skipped with a warning, unless none of the starting ones answers.
  - Radancy job URLs: `/[<lang>/]job/<city>/<title>/<org>/<id>`. Paradox job URLs:
    `/[<lang>/]jobs/<id>/<title>/` (ADP, GM, Verizon; other languages' words for "jobs" too) or
    `/<title>/job/<id>` (FedEx). A posting listed once per language is kept once, under its
    English URL (`en`, `en-ca`, `en-gb`, ...).
- **Descriptions:** one page each, for postings that pass the title filter by the same URL-word
  rules as [SuccessFactors](#successfactors-career-site-builder) (Radancy's URL words are the city
  and the title). The page's JSON-LD gives the title, description, location and date posted.
- **Location:** the JSON-LD's places (city, region, country), joined with `;` when there are
  several; without any, the URL's words around the title.
- **Remote:** yes if the JSON-LD says `TELECOMMUTE` or the location or title says "remote", else
  unknown.
- **Same jobs twice:** many of these sites front Workday or another ATS `jobhunt` reads (a
  posting's apply link says which). Add only one of the two boards; the survey gives the ATS
  board when it can.
- **Unknown board:** a host with no sitemap answers 404 and is removed. A sitemap with no job URLs
  in these shapes is never removed; `fetch` warns that it found nothing.
- **Rate group:** the careers host.

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
- **Rate group:** every `*.eightfold.ai` board shares the `eightfold` group, capped at two
  requests a second: they are one service, and 20 of them fetched side by side were answered
  405. A board on a company's own host has that host as its group. Microsoft's is capped at one
  request every two seconds, because its site answered 429 to requests one second apart.
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

## USAJOBS

```yaml
  - name: USAJOBS
    ats: usajobs
    slug: Seattle, Washington/50   # or: remote
```

The US federal government's jobs, through USAJOBS's documented search API. Unlike every other
source it needs credentials: a free API key from developer.usajobs.gov and the email address it
was requested with, set as `usajobs.api_key` and `usajobs.email` in `config/settings.yaml` (or
`JOBHUNT_USAJOBS_API_KEY` and `JOBHUNT_USAJOBS_EMAIL`). They are sent to data.usajobs.gov only;
without both, a USAJOBS board is skipped with a warning.

- **Slug:** where to search: a place USAJOBS knows (`Seattle, Washington`), optionally with a
  radius in miles after a slash (`Seattle, Washington/50`), or `remote` for remote jobs anywhere.
  A place's search already includes remote jobs, so one board is usually enough.
- **Endpoint:** `GET https://data.usajobs.gov/api/Search?Keyword=...&ResultsPerPage=500&Page=N&Fields=Full`,
  with the place as `LocationName` and `Radius`, or `RemoteIndicator=True`; headers `Host`,
  `User-Agent` (the email) and `Authorization-Key`. One search per term, descriptions included.
- **Company:** each posting's agency (`OrganizationName`), not "USAJOBS".
- **Id:** the control number (`MatchedObjectId`, the number in its link); one announcement can list
  several.
- **Remote:** yes if USAJOBS marks it remote or its location says so.
- **Titles:** federal technology leadership is often "Supervisory IT Specialist" or "Chief ...",
  which the usual target words don't search for; an `include_for_tags` entry for a tag on the
  USAJOBS board adds such words to its searches and its title filter.

## Politeness

Every request from `jobhunt fetch`, and from `python -m jobhunt.slugs --check`, goes through
one throttled HTTP transport. It identifies itself with the User-Agent
`jobhunt/0.1 (+personal job search tool)` (`fetch.user_agent`). It sends each host back the
cookies that host set itself: Cloudflare (in front of Workday and Workable) and Eightfold
expect them, and Eightfold blocked a fetch that didn't. They're kept per exact host
(unlike a browser, a `Domain=` cookie isn't shared with sibling subdomains), so the
tens of thousands Workday and BambooHR set over a run don't slow every request
(`throttle.no_cookies`).

### Rate groups and per-host limits

Requests are grouped by the server that answers them: one group per Workday datacenter, one
for all `*.eightfold.ai` and one for all BambooHR tenants, one per other careers host (Eightfold
on a company's own host, Oracle tenants, Phenom, SuccessFactors, Radancy and Paradox sites), and
one per API host for every other source.
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
| `workable` | 1.4/s | Cloudflare bans an IP after about 50 requests in 10 seconds, and 429s a steady 1.9/s after about 1,000. |
| `eightfold` | 2/s | Every `*.eightfold.ai` board; 20 fetched side by side were answered 405. |
| `apply.careers.microsoft.com` | 0.5/s | Microsoft's site answered 429 to requests one second apart. |
| `apple` | 1/s | Each page is about 300 KB. |

iCIMS Career Sites aren't in the map: each site's 0.2/s cap comes from its robots.txt's crawl
delay and is built into the source, though an entry for its host overrides it.

Keys are the group names `jobhunt -v fetch` prints, and are case-sensitive. Setting `max_rate`, in the
file or in `JOBHUNT_FETCH_MAX_RATE`, replaces the whole map, so copy these four into it to keep
them. Groups not listed have no cap, except iCIMS Career Sites; `{}` removes every configured
cap, while an iCIMS site keeps its built-in 0.2/s unless its host gets its own entry.

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

Each board is saved as it finishes. Ctrl-C, closing the terminal (SIGHUP) and SIGTERM all stop
the run the same way: no new requests, the ones already sent finish, and every finished board is
kept, including those that finished ahead of their turn. `jobhunt fetch --resume` fetches the
rest; a plain `jobhunt fetch` starts over.

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

A page that loads Career Site Builder's own scripts (`/platform/js/j2w/` or `/platform/csb`) gives a SuccessFactors
board for its host; a page that only mentions SuccessFactors gives none.

On a Radancy or Paradox site it reads the sitemaps (at most three files) until it finds a job
URL, then that one posting's page. If the page's apply links point at a board `jobhunt`
supports, such as Workday, it gives that board; otherwise the Radancy or Paradox board. A site
whose sitemaps list no job URLs gives none: Paradox's chat widget on another platform's site.

On a Phenom site it sends one search request (within robots.txt, like everything else) to read
where the postings apply. If they apply on a board `jobhunt` supports, such as Workday, it gives
that board; otherwise it gives the Phenom board, and none if the search is disallowed, fails
or finds nothing.

## Not covered: watch these by hand

Some platforms forbid automated access in robots.txt or their terms, or block it outright, so `jobhunt` doesn't
support them, and their boards must not be added to `companies.yaml`. Nothing enforces this for
listing and API sources: `jobhunt` checks robots.txt itself only for the sitemap sources, so
check a board's robots.txt before adding it. Their jobs are worth a manual search or the
platform's own email alerts.

| Platform | Who posts there | Why it's off-limits | Instead |
|---|---|---|---|
| NEOGOV (governmentjobs.com, schooljobs.com) | Most US state, county and city governments, and many school districts and transit agencies; many public-sector IT roles are posted here | robots.txt disallows everything but named search engines, and the terms (section 6) ban any "page-scrape, robot, spider", public pages included | A free account per employer's site, with job alerts or "job interest cards" by keyword |
| ctcLink (PeopleSoft) | Washington's 34 community and technical colleges | robots.txt: `Disallow: /` | Each college's careers page; some offer email alerts |
| UKG / UltiPro | Some transit agencies and REITs | robots.txt disallows `*/JobBoardView`, the listing the board loads | The employer's job board; check whether it offers alerts |
| PageUp | Some universities | A bot wall in front of the board | The university's careers page |
| SCALIS | Small companies | robots.txt disallows `/api/`, where the listings come from; the pages themselves have none | The company's careers page |
| Pinpoint, Dover | Startups | robots.txt disallows the boards (Pinpoint) or their API (Dover) | The company's careers page |
| Classic iCIMS portals (`careers-<company>.icims.com`) | Many mid-size and large employers | robots.txt: `Disallow: /` | Their newer branded careers sites, which `jobhunt` reads ([iCIMS Career Sites](#icims-career-sites)) |
| Any board whose robots.txt disallows its own path | Some employers on platforms `jobhunt` otherwise reads; some Workday sites disallow their site path | robots.txt disallows that board (only the sitemap sources notice; check before adding a board) | The employer's careers page |
| Google, Meta | Themselves | Google's robots.txt disallows its job pages; Meta's terms forbid automated collection | Their careers sites' own alerts |

Federal jobs are different: USAJOBS publishes an official API for exactly this.
