"""Scoring: prompt construction and response parsing, with a fake completer."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from jobhunt.llm import extract_json
from jobhunt.score import SYSTEM, build_user_prompt, score_job
from tests.conftest import make_completer


def test_rubric_mentions_known_gaps_ceiling():
    # Golden assertion: the rubric must keep the "known gaps cap at 5" rule.
    assert "ceiling is 5" in SYSTEM
    assert "Do not inflate" in SYSTEM


def test_user_prompt_includes_profile_modules_and_posting(platform_director_job, profile, kit):
    p = build_user_prompt(platform_director_job, profile, kit)
    assert "Known gaps" in p
    assert "build_infra_devx" in p
    assert "Director of Platform Engineering" in p


def test_score_job_parses_and_filters_modules(platform_director_job, profile, kit):
    complete = make_completer(
        {
            "score": 8,
            "rationale": "Good match.",
            "strengths": ["a"],
            "gaps": ["b"],
            "suggested_modules": ["build_infra_devx", "not_a_real_module", "sre_from_nothing"],
        }
    )
    sj = score_job(platform_director_job, profile, kit, complete)
    assert sj.score.score == 8
    assert sj.score.suggested_modules == ["build_infra_devx", "sre_from_nothing"]
    assert len(complete.calls) == 1


def test_score_out_of_range_rejected(platform_director_job, profile, kit):
    complete = make_completer({"score": 11, "rationale": "", "suggested_modules": []})
    with pytest.raises(ValidationError):
        score_job(platform_director_job, profile, kit, complete)


@pytest.mark.parametrize(
    "text",
    [
        '{"a": 1}',
        'Sure! ```json\n{"a": 1}\n```',
        'prefix {"a": 1} suffix',
        '{"a": 1}\n\n{"a": 1}',  # the model answered twice
        '{"a": 1}\n\nNote: I used the {known gaps} list.',
        'Scoring {this} role:\n{"a": 1}',
        '```json\n{"a": 1}\n```\n\n```json\n{"b": 2}\n```',
    ],
)
def test_extract_json_variants(text):
    assert extract_json(text) == {"a": 1}


def test_extract_json_keeps_nested_objects():
    assert extract_json('{"a": {"b": [1, {"c": 2}]}} trailing }') == {"a": {"b": [1, {"c": 2}]}}


@pytest.mark.parametrize("text", ["nothing here", "{not json}", '{"a": 1'])
def test_extract_json_no_object(text):
    with pytest.raises(ValueError):
        extract_json(text)
