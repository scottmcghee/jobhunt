"""Generate a cover letter from the Kit for a high-scoring job.

Design constraint: the model writes ONLY the two custom sentences and may lightly
adapt vocabulary inside the chosen modules to mirror the posting. It never
introduces new claims. We enforce the structure in code, not in the prompt.

It also reports the company's name as the posting writes it, because many boards in
companies.yaml carry only their slug ("axios") as a name. Code accepts that name only if
the posting really contains it.
"""

from __future__ import annotations

import re

from jobhunt.config import Kit, KitModule
from jobhunt.llm import Completer, backend_name, extract_json, model_name
from jobhunt.schema import Job, Letter, ScoredJob

SYSTEM = """You write two sentences for a cover letter. Nothing more.

You will be given a job posting, the candidate's fixed profile, and two pre-written proof
paragraphs that will appear in the letter. Produce:

0. company_name — the hiring company's name exactly as the posting writes it, with its
   capitalization and spacing (e.g. "Grafana Labs", not "grafanalabs"). Copy it; don't expand it.

1. custom_opening_sentence — ONE sentence that proves the candidate read something specific about
   this company or posting that most applicants would gloss over: an engineering-blog post, an
   unusual emphasis in the JD, a technology choice, a mission detail. Concrete beats flattering.
   Never "I've long admired..." Never generic.
2. custom_closing_sentence — ONE sentence on why this specific role fits what the candidate wants
   (Director/VP, infrastructure/platform/data/engineering leadership).

Rules:
- Use only facts present in the profile or the posting. Invent nothing about the candidate.
- Mirror the posting's vocabulary (if they say "platform engineering", don't say "SRE").
- No exclamation marks. No "passionate". No "excited". Plain, confident, specific.
- If the posting's core requirement is a known gap for the candidate, the closing sentence should
  name it plainly and briefly rather than hide it.

Respond with ONLY a JSON object:
{"company_name": "...", "custom_opening_sentence": "...", "custom_closing_sentence": "..."}"""


def choose_modules(scored: ScoredJob, kit: Kit) -> list[KitModule]:
    """Prefer the scorer's suggestion; fall back to keyword matching against use_when."""
    ids = [m for m in scored.score.suggested_modules if m in kit.modules]
    if len(ids) < 2:
        text = f"{scored.job.title}\n{scored.job.body}".lower()
        ranked = sorted(
            (m for m in kit.modules.values() if m.id not in ids),
            key=lambda m: -sum(text.count(k.lower()) for k in m.use_when),
        )
        for m in ranked:
            if len(ids) == 2:
                break
            ids.append(m.id)
    return [kit.modules[i] for i in ids[:2]]


def company_display_name(job: Job, proposed: object) -> str:
    """The company name to print in the letter.

    A name curated in companies.yaml wins. A placeholder name (the bare slug, or a Workday
    board's tenant) is replaced by the model's reading of the posting, but only if the posting
    contains it word for word.
    """
    if job.company not in (job.company_slug, job.company_slug.split("/")[0]):
        return job.company
    name = str(proposed or "").strip()
    if not name or "\n" in name or len(name) > 80:
        return job.company
    if re.search(rf"(?<!\w){re.escape(name)}(?!\w)", f"{job.title}\n{job.body}"):
        return name
    return job.company


def _fill(template: str, **values: str) -> str:
    out = template
    for k, v in values.items():
        out = out.replace("{" + k + "}", v)
    leftover = re.findall(r"\{[a-z_]+\}", out)
    if leftover:
        raise ValueError(f"unfilled placeholders: {leftover}")
    return out


def assemble(
    scored: ScoredJob,
    kit: Kit,
    modules: list[KitModule],
    opening_sentence: str,
    closing_sentence: str,
    company: str | None = None,
) -> str:
    opening = _fill(
        kit.opening,
        role_title=scored.job.title,
        company=company or scored.job.company,
        custom_opening_sentence=opening_sentence.strip(),
    )
    closing = _fill(kit.closing, custom_closing_sentence=closing_sentence.strip())
    body = "\n\n".join(m.text for m in modules)
    return f"{opening}\n\n{body}\n\n{closing}\n"


def word_count(text: str) -> int:
    return len(re.findall(r"\b\w[\w'’\-]*\b", text))


def generate_letter(scored: ScoredJob, profile: str, kit: Kit, complete: Completer) -> Letter:
    modules = choose_modules(scored, kit)
    user = f"""# CANDIDATE PROFILE
{profile}

# JOB POSTING
Company (as configured; may be a lowercase board slug): {scored.job.company}
Title: {scored.job.title}
URL: {scored.job.url}

{scored.job.body[:12000]}

# SCORER'S NOTES
Score: {scored.score.score}/10
Rationale: {scored.score.rationale}
Gaps: {'; '.join(scored.score.gaps) or 'none noted'}

# PROOF PARAGRAPHS THAT WILL APPEAR IN THE LETTER
{chr(10).join(f'[{m.id}] {m.text}' for m in modules)}
"""
    data = extract_json(complete(SYSTEM, user, 800))
    company = company_display_name(scored.job, data.get("company_name"))
    text = assemble(
        scored,
        kit,
        modules,
        str(data["custom_opening_sentence"]),
        str(data["custom_closing_sentence"]),
        company,
    )
    return Letter(
        job_key=scored.job.key,
        company=company,
        title=scored.job.title,
        modules_used=[m.id for m in modules],
        text=text,
        model=f"{backend_name()}:{model_name()}",
    )
