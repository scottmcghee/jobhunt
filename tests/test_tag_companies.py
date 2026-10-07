"""Company tagging (scripts/tag_companies.py). The model is faked; no network."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
import yaml

from jobhunt import storage
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
    ],
)
def test_big_tech(board, expected):
    assert tagger.is_big_tech(board) is expected


def test_sp500_boards_come_from_the_survey(tmp_path):
    sp = tagger.sp500_boards(_survey(tmp_path))
    howmet = "oracle:fa-exty-saasfaprod1.fa.ocs.oraclecloud.com/cx_1"
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
