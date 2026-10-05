"""Score a job 1–10 against the fixed candidate profile.

The rubric is deliberately explicit so scores are comparable across runs and
across model versions. Change the rubric here and update tests/test_score.py.
"""

from __future__ import annotations

from jobhunt.config import Kit
from jobhunt.llm import Completer, backend_name, extract_json, model_name
from jobhunt.schema import Job, Score, ScoredJob

SYSTEM = """You are a rigorous, candid recruiter screening roles for one specific candidate.
You are not trying to be encouraging. You are trying to be accurate, so the candidate spends
time only on roles where they have a real shot.

Score the role 1-10 for THIS candidate using the rubric:

10  Near-perfect: level, domain, industry, location all match; gaps are cosmetic.
8-9 Strong: level and domain match; one meaningful gap the candidate can credibly address.
6-7 Plausible: level or domain is a partial match; two gaps, or one gap that is a core requirement.
4-5 Stretch: the posting's core requirement is in the candidate's known-gaps list,
    or level is off by one.
1-3 No: wrong level (well below or far above the profile's Target), wrong function,
    wrong geography, or a disqualifying requirement.

Rules:
- Treat the candidate's "Known gaps" section as facts. If the posting's primary requirement is a
  known gap (e.g., a core technology the profile lists as a gap), the ceiling is 5.
- Do not inflate. Most roles score 4-7. A 9 or 10 should be rare.
- "suggested_modules" must be exactly two ids from the provided module list, best matched to what
  the posting spends the most words on.

Respond with ONLY a JSON object:
{
  "score": <int 1-10>,
  "rationale": "<2-4 sentences, specific to this posting>",
  "strengths": ["<short phrase>", ...],
  "gaps": ["<short phrase>", ...],
  "suggested_modules": ["<module_id>", "<module_id>"]
}"""


def build_user_prompt(job: Job, profile: str, kit: Kit) -> str:
    module_list = "\n".join(
        f"- {m.id}: {m.title} (use when: {', '.join(m.use_when)})" for m in kit.modules.values()
    )
    body = job.body[:12000]  # keep prompt bounded; postings are rarely longer
    remote = job.remote if job.remote is not None else "unknown"
    return f"""# CANDIDATE PROFILE
{profile}

# AVAILABLE COVER LETTER MODULES
{module_list}

# JOB POSTING
Company: {job.company}
Title: {job.title}
Location: {job.location or 'not stated'}  Remote: {remote}
URL: {job.url}

{body}
"""


def score_job(job: Job, profile: str, kit: Kit, complete: Completer) -> ScoredJob:
    raw = complete(SYSTEM, build_user_prompt(job, profile, kit), 1600)
    data = extract_json(raw)
    # Only allow module ids that actually exist in the kit.
    mods = [m for m in data.get("suggested_modules", []) if m in kit.modules][:2]
    score = Score(
        score=int(data["score"]),
        rationale=str(data.get("rationale", "")).strip(),
        strengths=[str(s) for s in data.get("strengths", [])],
        gaps=[str(g) for g in data.get("gaps", [])],
        suggested_modules=mods,
        model=f"{backend_name()}:{model_name()}",
    )
    return ScoredJob(job=job, score=score)
