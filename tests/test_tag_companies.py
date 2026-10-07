"""Company tagging (scripts/tag_companies.py). The model is faked; no network."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
import yaml

from jobhunt import config, storage
from jobhunt.schema import Company, Job

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "tag_companies.py"
_spec = importlib.util.spec_from_file_location("tag_companies", _SCRIPT)
tagger = importlib.util.module_from_spec(_spec)
sys.modules["tag_companies"] = tagger  # dataclasses look their module up while it loads
_spec.loader.exec_module(tagger)

COMPANIES = """\
# Companies to watch.
companies:
  # the big one
  - name: Amazon
    ats: amazon
    slug: USA
    tags: [saas, seattle]

  - name: nvidia
    ats: workday
    slug: nvidia/NVIDIAExternalCareerSite
    datacenter: wd5
    tags: []

  - name: Acme Learning
    ats: greenhouse
    slug: acmelearning
    tags:
      - remote
      - edtech

  - name: Clinicorp
    ats: lever
    slug: clinicorp

  - name: Howmet
    ats: oracle
    slug: fa-exty-saasfaprod1.fa.ocs.oraclecloud.com/CX_1
    tags: []
"""


def _companies(tmp_path, text=COMPANIES):
    p = tmp_path / "companies.yaml"
    p.write_text(text)
    return p


def _load(p):
    return {c.key: c.tags for c in (Company.model_validate(e) for e in yaml.safe_load(p.read_text())["companies"])}


def _survey(tmp_path):
    out = tmp_path / "sp500"
    out.mkdir()
    rows = [
        {"ticker": "HWM", "name": "Howmet Aerospace", "sector": "Industrials", "site": "https://howmet.com",
         "pages": [], "platforms": ["oracle"], "skipped_by_robots": [], "errors": [], "status": "", "attempts": 1,
         "boards": [{"name": "Howmet Aerospace", "ats": "oracle", "slug": "fa-exty-saasfaprod1.fa.ocs.oraclecloud.com/CX_1",
                     "datacenter": None, "location": None, "tags": []}]},
        {"ticker": "NVDA", "name": "Nvidia", "sector": "Information Technology", "site": "https://nvidia.com",
         "pages": [], "platforms": ["workday"], "skipped_by_robots": [], "errors": [], "status": "", "attempts": 1,
         "boards": [{"name": "Nvidia", "ats": "workday", "slug": "nvidia/NVIDIAExternalCareerSite",
                     "datacenter": "wd5", "location": None, "tags": []}]},
    ]
    (out / "results.json").write_text(json.dumps(rows))
    return out / "results.json"


class FakeModel:
    """Answers a classification prompt from a fixed table; records each call."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def __call__(self, system, user, max_tokens):
        self.calls.append(user)
        keys = [line.split(" | ")[0] for line in user.splitlines() if " | " in line]
        return json.dumps({k: self.answers.get(k, []) for k in keys})


def test_vocabulary():
    assert {"big-tech", "sp500", "edtech", "healthcare", "staffing", "defense"} <= set(tagger.VOCABULARY)
    assert set(tagger.INDUSTRIES) < set(tagger.VOCABULARY) and "big-tech" not in tagger.INDUSTRIES


@pytest.mark.parametrize(
    ("board", "expected"),
    [
        (Company(name="Amazon", ats="amazon", slug="USA"), True),
        (Company(name="Apple", ats="apple", slug="united-states-USA"), True),
        (Company(name="Microsoft", ats="eightfold", slug="apply.careers.microsoft.com"), True),
        (Company(name="nvidia", ats="eightfold", slug="nvidia.eightfold.ai"), True),
        (Company(name="nvidia", ats="workday", slug="nvidia/NVIDIAExternalCareerSite", datacenter="wd5"), True),
        (Company(name="eeho/CX_45001", ats="oracle", slug="eeho.fa.us2.oraclecloud.com/CX_45001"), True),
        (Company(name="googlefiber", ats="greenhouse", slug="googlefiber"), False),
        (Company(name="Applecart", ats="lever", slug="applecart"), False),
        (Company(name="Careers", ats="eightfold", slug="apply.careers.microsoft.com"), True),  # host only
        (Company(name="Netflix", ats="lever", slug="nflx"), True),  # name only
    ],
)
def test_big_tech(board, expected):
    assert tagger.is_big_tech(board) is expected


def test_sp500_boards_come_from_the_survey(tmp_path):
    sp = tagger.sp500_boards(_survey(tmp_path))
    howmet = "oracle:fa-exty-saasfaprod1.fa.ocs.oraclecloud.com"  # Oracle: by host, any site
    assert sp[howmet] == ("HWM", "Industrials")
    assert tagger.sector_tags("Industrials") == ["manufacturing"]
    assert tagger.sector_tags("Health Care") == ["healthcare"]
    assert tagger.sector_tags("Information Technology") == []  # too broad; the model decides
    assert tagger.sp500_boards(tmp_path / "nope.json") == {}


def test_classify_batches_caches_and_keeps_only_known_industries(tmp_path):
    boards = [Company(name=f"Co{i}", ats="greenhouse", slug=f"co{i}") for i in range(5)]
    model = FakeModel({"greenhouse:co0": ["edtech", "made-up"], "greenhouse:co3": ["fintech", "saas", "ai"]})
    cache = tmp_path / "company_tags.json"
    titles = {"greenhouse:co0": ["Director, Learning Platform"]}
    got = tagger.classify(boards, model, cache, titles, batch_size=2)
    assert len(model.calls) == 3  # 5 boards, 2 a call
    assert "Director, Learning Platform" in model.calls[0]
    assert got["greenhouse:co0"] == ["edtech"]  # unknown tags dropped
    assert got["greenhouse:co3"] == ["fintech", "saas"]  # at most two
    assert got["greenhouse:co1"] == []
    assert tagger.classify(boards, model, cache, titles, batch_size=2) == got
    assert len(model.calls) == 3  # all cached: no new calls
    more = [*boards, Company(name="New", ats="lever", slug="new")]
    tagger.classify(more, model, cache, titles, batch_size=2)
    assert len(model.calls) == 4 and "lever:new" in model.calls[-1]


def test_classify_normalizes_near_miss_answers(tmp_path):
    boards = [Company(name="A", ats="greenhouse", slug="a")]
    model = FakeModel({"greenhouse:a": [" Healthcare", "public sector", 3]})
    got = tagger.classify(boards, model, tmp_path / "c.json", {})
    assert got == {"greenhouse:a": ["healthcare", "public-sector"]}


def test_classify_survives_a_bad_answer(tmp_path, caplog):
    boards = [Company(name="A", ats="greenhouse", slug="a"), Company(name="B", ats="greenhouse", slug="b")]
    replies = iter(["no json here", json.dumps({"greenhouse:b": ["healthcare"]})])
    cache = tmp_path / "company_tags.json"
    got = tagger.classify(boards, lambda s, u, m: next(replies), cache, {}, batch_size=1)
    assert got == {"greenhouse:b": ["healthcare"]}  # "a" is left for the next run
    assert "couldn't read" in caplog.text
    assert "greenhouse:a" not in json.loads(cache.read_text())


def test_classify_respects_a_batch_limit(tmp_path):
    boards = [Company(name=f"Co{i}", ats="greenhouse", slug=f"co{i}") for i in range(5)]
    model = FakeModel({})
    tagger.classify(boards, model, tmp_path / "c.json", {}, batch_size=2, max_batches=1)
    assert len(model.calls) == 1


def test_titles_by_board(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    for i, title in enumerate(["Director, Platform", "VP Engineering", "Nurse", "Chef", "Driver", "Analyst"]):
        job = Job(source="greenhouse", company="Co", company_slug="co", external_id=str(i), title=title, url="https://x")
        storage.append_jsonl(data / "jobs.jsonl", job.model_dump())
    titles = tagger.titles_by_board(data, per_board=5)
    assert titles["greenhouse:co"] == ["Director, Platform", "VP Engineering", "Nurse", "Chef", "Driver"]
    assert tagger.titles_by_board(tmp_path / "empty") == {}


def test_tagging_keeps_hand_tags_layout_and_comments(tmp_path):
    p = _companies(tmp_path)
    industry = {"amazon:USA": ["retail"], "lever:clinicorp": ["healthcare"], "greenhouse:acmelearning": ["edtech", "ai"]}
    sp = tagger.sp500_boards(_survey(tmp_path))
    changed = tagger.apply_tags(p, industry, sp)
    tags = _load(p)
    assert tags["amazon:USA"] == ["seattle", "big-tech", "retail"]  # "saas" is managed: the model didn't say it
    assert tags["workday:nvidia/NVIDIAExternalCareerSite"] == ["big-tech", "sp500"]
    assert tags["greenhouse:acmelearning"] == ["remote", "edtech", "ai"]
    assert tags["lever:clinicorp"] == ["healthcare"]  # an entry with no tags line gets one
    assert tags["oracle:fa-exty-saasfaprod1.fa.ocs.oraclecloud.com/CX_1"] == ["sp500", "manufacturing"]
    text = p.read_text()
    assert text.startswith("# Companies to watch.\ncompanies:\n  # the big one\n  - name: Amazon\n")
    assert "    tags: [remote, edtech, ai]\n" in text and "      - remote" not in text  # block list → flow
    assert changed == 5
    assert tagger.apply_tags(p, industry, sp) == 0  # idempotent
    assert p.read_text() == text


def test_dry_run_changes_nothing(tmp_path):
    p = _companies(tmp_path)
    before = p.read_text()
    assert tagger.apply_tags(p, {"lever:clinicorp": ["healthcare"]}, {}, dry_run=True) > 0
    assert p.read_text() == before


def test_main_runs_end_to_end(tmp_path, monkeypatch, capsys):
    p = _companies(tmp_path)
    survey = _survey(tmp_path)
    model = FakeModel({"lever:clinicorp": ["healthcare"], "greenhouse:acmelearning": ["edtech"]})
    monkeypatch.setattr(tagger, "make_completer", lambda llm_settings=None: model)
    data = tmp_path / "data"
    data.mkdir()
    args = ["--companies", str(p), "--data-dir", str(data), "--survey", str(survey)]
    assert tagger.main(args) == 0
    assert _load(p)["lever:clinicorp"] == ["healthcare"]
    assert (data / "company_tags.json").exists()
    out = capsys.readouterr().out
    assert "entries changed" in out and "healthcare" in out
    calls = len(model.calls)
    assert tagger.main([*args, "--no-llm"]) == 0
    assert len(model.calls) == calls  # --no-llm makes no calls, but the cache still applies
    assert _load(p)["lever:clinicorp"] == ["healthcare"]


def test_an_entry_whose_tags_dont_change_is_left_exactly_as_written(tmp_path):
    p = _companies(tmp_path)
    tagger.apply_tags(p, {"greenhouse:acmelearning": ["edtech"]}, {})
    assert "    tags:\n      - remote\n      - edtech\n" in p.read_text()


def test_a_board_with_no_industry_answer_keeps_its_industries(tmp_path):
    text = "companies:\n  - name: A\n    ats: lever\n    slug: a\n    tags: [security, remote, sp500]\n"
    p = _companies(tmp_path, text)
    tagger.apply_tags(p, {}, {})  # no answer for lever:a: not "no industries"
    assert _load(p)["lever:a"] == ["remote", "security"]  # sp500 is still recomputed
    tagger.apply_tags(p, {"lever:a": []}, {})  # an answer of none clears them
    assert _load(p)["lever:a"] == ["remote"]


def test_no_llm_without_a_cache_keeps_the_example_industries(tmp_path, monkeypatch):
    example = Path(__file__).resolve().parents[1] / "config.example" / "companies.yaml"
    p = _companies(tmp_path, example.read_text())
    before = _load(p)
    data = tmp_path / "data"
    data.mkdir()
    assert tagger.main(["--companies", str(p), "--data-dir", str(data), "--no-llm"]) == 0
    after = _load(p)
    for key, tags in before.items():
        industries = [t for t in tags if t in tagger.INDUSTRIES]
        assert [t for t in after[key] if t in tagger.INDUSTRIES] == industries, key


def test_oracle_boards_match_the_survey_by_host(tmp_path):
    host = "fa-exty-saasfaprod1.fa.ocs.oraclecloud.com"
    text = (f"companies:\n  - name: Howmet\n    ats: oracle\n    slug: {host}/CX\n\n"
            "  - name: nvidia\n    ats: workday\n    slug: nvidia/OtherSite\n    datacenter: wd5\n")
    p = _companies(tmp_path, text)
    tagger.apply_tags(p, {}, tagger.sp500_boards(_survey(tmp_path)))  # the survey has host/CX_1
    tags = _load(p)
    assert tags[f"oracle:{host}/CX"] == ["sp500", "manufacturing"]
    assert tags["workday:nvidia/OtherSite"] == ["big-tech"]  # Workday: the site must match too


@pytest.mark.parametrize(("hand", "answer", "expected"), [
    ("", ["defense"], ["sp500", "defense"]),  # the model's answer wins: no sector tags
    ("", [], ["sp500", "manufacturing"]),  # an answer of none: the sector fills in
    ("", None, ["sp500", "manufacturing"]),  # no answer: the sector fills in
    ("    tags: [energy]\n", None, ["sp500", "energy", "manufacturing"]),  # ...beside what's there
    ("    tags: [energy]\n", [], ["sp500", "manufacturing"]),
])
def test_sector_industries_only_when_the_model_names_none(tmp_path, hand, answer, expected):
    host = "fa-exty-saasfaprod1.fa.ocs.oraclecloud.com"
    p = _companies(tmp_path, f"companies:\n  - name: Howmet\n    ats: oracle\n    slug: {host}/CX\n{hand}")
    industry = {} if answer is None else {f"oracle:{host}/CX": answer}
    sp = tagger.sp500_boards(_survey(tmp_path))
    tagger.apply_tags(p, industry, sp)
    assert _load(p)[f"oracle:{host}/CX"] == expected
    assert tagger.apply_tags(p, industry, sp) == 0  # idempotent


def _run(tmp_path, text, industry):
    p = _companies(tmp_path, text)
    tagger.apply_tags(p, industry, {})
    return p


ENTRY = "companies:\n  - name: A\n    ats: lever\n    slug: a\n"


@pytest.mark.parametrize("tag", ['"2024"', '"yes"', '"null"', '"a, b"', '"#1"', "'on'"])
def test_quoted_hand_tags_stay_strings(tmp_path, tag):
    p = _run(tmp_path, f"{ENTRY}    tags: [{tag}]\n", {"lever:a": ["ai"]})
    assert [c.tags for c in config.load_companies(p)] == [[yaml.safe_load(tag), "ai"]]


@pytest.mark.parametrize(
    "tags_text",
    [
        "    tags:\n    - remote\n",  # an indentless list
        "    tags:  # hand tags\n      - remote\n",  # a comment on the tags line
        "    tags:\n      # mine\n      - remote\n",  # a comment inside the list
    ],
)
def test_block_list_shapes(tmp_path, tags_text):
    text = f"{ENTRY}{tags_text}\n  - name: B\n    ats: lever\n    slug: b\n"
    p = _run(tmp_path, text, {"lever:a": ["ai"]})
    assert [c.tags for c in config.load_companies(p)] == [["remote", "ai"], []]
    assert "    slug: a\n    tags: [remote, ai]" in p.read_text()
    assert "\n\n  - name: B\n" in p.read_text()  # the blank line between entries stays


def test_a_comment_on_the_tags_line_is_kept(tmp_path):
    p = _run(tmp_path, f"{ENTRY}    tags: [remote]  # why\n", {"lever:a": ["ai"]})
    assert "    tags: [remote, ai]  # why\n" in p.read_text()


def test_an_entry_whose_first_key_is_tags(tmp_path):
    text = "companies:\n  - tags: [remote]\n    name: A\n    ats: lever\n    slug: a\n"
    p = _run(tmp_path, text, {"lever:a": ["ai"]})
    assert p.read_text().count("tags:") == 1
    assert "  - tags: [remote, ai]\n    name: A\n" in p.read_text()


def test_last_entry_without_a_final_newline(tmp_path):
    p = _run(tmp_path, ENTRY.rstrip("\n"), {"lever:a": ["ai"]})
    assert [c.tags for c in config.load_companies(p)] == [["ai"]]


def test_content_after_the_companies_list_is_kept(tmp_path):
    p = _run(tmp_path, f"{ENTRY}    tags: []\nsettings_note: keep me\n", {"lever:a": ["ai"]})
    assert p.read_text().endswith("    tags: [ai]\nsettings_note: keep me\n")


def test_a_bad_rewrite_is_never_written(tmp_path, monkeypatch):
    p = _companies(tmp_path, f"{ENTRY}    tags: []\n")
    before = p.read_text()
    monkeypatch.setattr(tagger, "_rewrite", lambda entry, tags: [*entry, "      - stray\n"])
    with pytest.raises(ValueError):
        tagger.apply_tags(p, {"lever:a": ["ai"]}, {})
    monkeypatch.setattr(tagger, "_rewrite", lambda entry, tags: entry)  # parses, but wrong tags
    with pytest.raises(ValueError):
        tagger.apply_tags(p, {"lever:a": ["ai"]}, {})
    assert p.read_text() == before


@pytest.mark.parametrize(
    "tags_text",
    [
        "    tags: [remote,\n      seattle]\n",  # a flow list wrapped across lines
        "    tags:\n      [remote, seattle]\n",  # a flow list on the line after tags:
    ],
)
def test_a_tags_shape_it_cant_rewrite_skips_only_that_entry(tmp_path, caplog, tags_text):
    a = f"{ENTRY}{tags_text}"
    p = _run(tmp_path, f"{a}\n  - name: B\n    ats: lever\n    slug: b\n", {"lever:a": ["ai"], "lever:b": ["ai"]})
    assert p.read_text().startswith(a)  # left exactly as written
    assert [c.tags for c in config.load_companies(p)] == [["remote", "seattle"], ["ai"]]
    assert "lever:a" in caplog.text


@pytest.mark.parametrize(("tags", "cached"), [
    ("[sp500, retail]", None),
    ("[sp500, retail]", []),  # an answer of none doesn't drop the sector's industries
    ("[sp500, saas]", ["saas"]),  # (a named answer replaces them, survey or not)
])
def test_a_missing_survey_keeps_sp500_tags(tmp_path, caplog, tags, cached):
    p = _companies(tmp_path, f"{ENTRY}    tags: {tags}\n")
    before = p.read_text()
    data = tmp_path / "data"
    data.mkdir()
    if cached is not None:
        (data / "company_tags.json").write_text(json.dumps({"lever:a": cached}))
    args = ["--companies", str(p), "--data-dir", str(data), "--survey", str(tmp_path / "nope.json"), "--no-llm"]
    assert tagger.main(args) == 0
    assert tagger.main(args) == 0  # and again: still idempotent
    assert p.read_text() == before
    assert "nope.json" in caplog.text


def test_a_missing_survey_keeps_a_workday_boards_sector_industry(tmp_path):
    p = _companies(tmp_path, "companies:\n  - name: AT&T\n    ats: workday\n    slug: att/ATTGeneral\n"
                             "    datacenter: wd1\n    tags: [sp500, media]\n")
    before = p.read_text()
    data = tmp_path / "data"
    data.mkdir()
    (data / "company_tags.json").write_text(json.dumps({"workday:att/ATTGeneral": []}))
    args = ["--companies", str(p), "--data-dir", str(data), "--survey", str(tmp_path / "nope.json"), "--no-llm"]
    assert tagger.main(args) == 0
    assert p.read_text() == before
