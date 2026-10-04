"""Board-slug harvesting from Common Crawl index dumps."""

from __future__ import annotations

import json

import pytest
import yaml

from jobhunt import slugs
from jobhunt.schema import Company


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://boards.greenhouse.io/0x/jobs/4209667002?ref=x.com", ("greenhouse", "0x")),
        ("https://boards.greenhouse.io/1047games/", ("greenhouse", "1047games")),
        ("https://job-boards.greenhouse.io/figma?error=true", ("greenhouse", "figma")),
        ("https://job-boards.anz.greenhouse.io/dawnaerospace/jobs/1", ("greenhouse", "dawnaerospace")),
        ("https://job-boards.eu.greenhouse.io/acme", ("greenhouse", "acme")),
        ("https://boards.greenhouse.io/Convene/jobs/8053971", ("greenhouse", "convene")),
        ("https://boards.greenhouse.io/dicefm-careers/jobs/1", ("greenhouse", "dicefm-careers")),
        ("https://boards.greenhouse.io/embed/job_board?for=acme&b=x", ("greenhouse", "acme")),
        ("https://boards.greenhouse.io/embed/job_app?token=1&for=Acme", ("greenhouse", "acme")),
        ("https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true", ("greenhouse", "acme")),
        ("https://jobs.lever.co/Timely/abc-123/apply", ("lever", "timely")),
        ("https://jobs.ashbyhq.com/counterpart/abc-123", ("ashby", "counterpart")),
        ("https://jobs.ashbyhq.com/Some%20Co", ("ashby", "Some Co")),
        # not boards
        ("https://www.greenhouse.io/blog/5-culture-fit-questions", None),
        ("https://app7.greenhouse.io/ai_opt_out_request/job_post/4823689007/ai_opt_out", None),
        ("https://api.greenhouse.io/users/sign_in", None),
        # board hosts, but no slug
        ("https://boards.greenhouse.io/", None),
        ("https://job-boards.anz.greenhouse.io/robots.txt", None),
        ("https://boards.greenhouse.io/embed/job_board", None),
        ("https://boards-api.greenhouse.io/v1/boards/", None),
        ("http://[bad", None),
    ],
)
def test_slug_from_url(url, expected):
    assert slugs.slug_from_url(url) == expected


def test_discover_dedupes_and_sorts():
    urls = [
        "https://jobs.lever.co/zeta/1",
        "https://boards.greenhouse.io/beta/jobs/1",
        "https://boards.greenhouse.io/alpha/jobs/1",
        "https://job-boards.greenhouse.io/Beta/jobs/2",
        "https://www.greenhouse.io/",
    ]
    found = slugs.discover(urls)
    assert [(c.ats, c.slug) for c in found] == [
        ("greenhouse", "alpha"),
        ("greenhouse", "beta"),
        ("lever", "zeta"),
    ]
    assert all(c.name == c.slug and c.tags == [] for c in found)


def test_discover_skips_known_companies():
    known = [Company(name="Alpha", ats="greenhouse", slug="alpha")]
    urls = ["https://boards.greenhouse.io/alpha", "https://jobs.lever.co/alpha"]
    assert [(c.ats, c.slug) for c in slugs.discover(urls, known)] == [("lever", "alpha")]


def test_discover_dedupes_ashby_case_insensitively():
    urls = ["https://jobs.ashbyhq.com/Acme/1", "https://jobs.ashbyhq.com/acme/2"]
    assert [c.slug for c in slugs.discover(urls)] == ["Acme"]


def test_render_matches_companies_yaml_style():
    out = slugs.render([Company(name="alpha", ats="greenhouse", slug="alpha")])
    assert out == "  - name: alpha\n    ats: greenhouse\n    slug: alpha\n    tags: []\n"


def test_render_roundtrips_awkward_slugs():
    awkward = ["0x", "123", "true", "null", "1e3", "Some Co", "a-b.c"]
    companies = [Company(name=s, ats="ashby", slug=s) for s in awkward]
    loaded = yaml.safe_load("companies:\n" + slugs.render(companies))["companies"]
    assert [Company.model_validate(c) for c in loaded] == companies


def test_render_empty_is_empty():
    assert slugs.render([]) == ""


def test_read_urls_skips_malformed_lines(tmp_path):
    p = tmp_path / "cc.json"
    p.write_text(
        json.dumps({"url": "https://boards.greenhouse.io/alpha", "status": "301"})
        + "\n\nnot json\n"
        + json.dumps({"status": "200"})
        + "\n"
        + json.dumps({"url": "https://boards.greenhouse.io/beta"})
        + "\n"
    )
    assert list(slugs.read_urls(p)) == [
        "https://boards.greenhouse.io/alpha",
        "https://boards.greenhouse.io/beta",
    ]


def test_main_writes_pasteable_yaml(tmp_path):
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    a.write_text(json.dumps({"url": "https://boards.greenhouse.io/alpha/jobs/1"}) + "\n")
    b.write_text(
        json.dumps({"url": "https://boards.greenhouse.io/known/jobs/1"})
        + "\n"
        + json.dumps({"url": "https://jobs.lever.co/gamma"})
        + "\n"
    )
    existing = tmp_path / "companies.yaml"
    existing.write_text("companies:\n  - name: Known\n    ats: greenhouse\n    slug: known\n")
    out = tmp_path / "out.yaml"

    rc = slugs.main([str(a), str(b), "--companies", str(existing), "-o", str(out)])

    assert rc == 0
    merged = yaml.safe_load(existing.read_text() + out.read_text())["companies"]
    assert [(c["ats"], c["slug"]) for c in merged] == [
        ("greenhouse", "known"),
        ("greenhouse", "alpha"),
        ("lever", "gamma"),
    ]


def test_main_missing_input_is_an_error(tmp_path):
    assert slugs.main([str(tmp_path / "nope.json"), "-o", str(tmp_path / "out.yaml")]) == 2
