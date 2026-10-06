"""Config loaders and kit parsing."""

from __future__ import annotations

from typing import get_args

import pytest

from jobhunt import config
from jobhunt.schema import ATSName
from tests.conftest import CONFIG_DIR, LOCAL_CONFIG_DIR


def test_companies_load_and_validate():
    companies = config.load_companies(CONFIG_DIR / "companies.yaml")
    assert companies, "companies.yaml should not be empty"
    assert all(c.ats in get_args(ATSName) for c in companies)
    assert len({(c.ats, c.slug) for c in companies}) == len(companies), "duplicate slugs"


def test_preferences_have_sane_threshold(prefs):
    assert 1 <= prefs.scoring.min_score_for_letter <= 10
    assert prefs.title.must_include_any


def test_kit_loads_every_module(kit):
    assert len(kit.modules) == len(list((CONFIG_DIR / "kit" / "modules").glob("*.md"))) >= 2
    assert "{custom_opening_sentence}" in kit.opening
    assert "{custom_closing_sentence}" in kit.closing
    for m in kit.modules.values():
        assert m.text and m.use_when, m.id


def test_parse_module_requires_frontmatter():
    with pytest.raises(ValueError):
        config.parse_module("no frontmatter here")


def test_parse_module_reads_fields():
    m = config.parse_module("---\nid: x\ntitle: X\nuse_when: [a, b]\n---\nbody text\n")
    assert m.id == "x" and m.title == "X" and m.use_when == ["a", "b"] and m.text == "body text"


COMPANIES_YAML = """\
# Companies to watch.
# (header comments must survive pruning)

companies:
  - name: Alpha
    ats: greenhouse
    slug: alpha
    tags: [infra]

  # a note about Beta
  - name: Beta
    ats: greenhouse
    slug: beta
    tags: []

  - name: Beta on Lever
    ats: lever
    slug: beta
    tags: []

  - name: Gamma
    ats: ashby
    slug: gamma
    tags: []
"""


def test_company_key():
    assert config.Company(name="A", ats="lever", slug="a").key == "lever:a"


def test_remove_companies_keeps_everything_else(tmp_path):
    p = tmp_path / "companies.yaml"
    p.write_text(COMPANIES_YAML)

    removed = config.remove_companies(p, {"greenhouse:beta", "ashby:nope"})

    assert [c.name for c in removed] == ["Beta"]
    text = p.read_text()
    assert text.startswith("# Companies to watch.\n# (header comments must survive pruning)\n")
    assert "  - name: Beta\n" not in text
    assert [c.key for c in config.load_companies(p)] == ["greenhouse:alpha", "lever:beta", "ashby:gamma"]
    assert "\n\n\n" not in text


def test_load_companies_warns_about_oracle_boards_sharing_a_host(tmp_path, caplog):
    p = tmp_path / "companies.yaml"
    p.write_text(
        "companies:\n"
        "  - {name: A, ats: oracle, slug: eeho.fa.us2.oraclecloud.com/CX_1}\n"
        "  - {name: B, ats: oracle, slug: EEHO.fa.us2.oraclecloud.com/jobsearch}\n"
        "  - {name: C, ats: oracle, slug: ehzq.fa.us2.oraclecloud.com/CX_1}\n"
    )

    assert len(config.load_companies(p)) == 3  # a warning, not an error
    assert "eeho.fa.us2.oraclecloud.com: 2 oracle boards" in caplog.text
    assert "ehzq" not in caplog.text


def test_remove_companies_last_entry(tmp_path):
    p = tmp_path / "companies.yaml"
    p.write_text(COMPANIES_YAML)

    assert [c.name for c in config.remove_companies(p, {"ashby:gamma"})] == ["Gamma"]
    assert p.read_text().endswith("    slug: beta\n    tags: []\n")
    assert [c.name for c in config.load_companies(p)] == ["Alpha", "Beta", "Beta on Lever"]


def test_remove_companies_nothing_to_do_leaves_file_untouched(tmp_path):
    p = tmp_path / "companies.yaml"
    p.write_text(COMPANIES_YAML)
    assert config.remove_companies(p, set()) == []
    assert p.read_text() == COMPANIES_YAML


def test_missing_config_names_the_templates(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DEFAULT_CONFIG_DIR", tmp_path / "config")
    with pytest.raises(config.ConfigMissing, match="config.example"):
        config.load_profile()


@pytest.mark.skipif(not LOCAL_CONFIG_DIR.is_dir(), reason="no personal config/ in this checkout")
def test_local_config_is_valid():
    """Your own config/ (gitignored) must still load: no duplicate boards, a parseable kit."""
    companies = config.load_companies(LOCAL_CONFIG_DIR / "companies.yaml")
    assert len({c.key for c in companies}) == len(companies), "duplicate (ats, slug) in config/"
    config.load_preferences(LOCAL_CONFIG_DIR / "preferences.yaml")
    assert config.load_profile(LOCAL_CONFIG_DIR / "profile.md").strip()
    assert config.load_kit(LOCAL_CONFIG_DIR / "kit").modules


def test_workday_company_needs_tenant_site_and_datacenter():
    from pydantic import ValidationError

    c = config.Company(name="Adobe", ats="workday", slug="adobe/external_experienced", datacenter="wd5")
    assert c.key == "workday:adobe/external_experienced"
    with pytest.raises(ValidationError):
        config.Company(name="Adobe", ats="workday", slug="adobe/external_experienced")  # no datacenter
    with pytest.raises(ValidationError):
        config.Company(name="Adobe", ats="workday", slug="adobe", datacenter="wd5")  # no site
    with pytest.raises(ValidationError):
        config.Company(name="Adobe", ats="workday", slug="adobe/x", datacenter="eu-west")


def test_profile_template_keeps_the_headings_the_prompts_name():
    # score.py names "Known gaps" and generate.py names "Target"; renaming either breaks a prompt.
    text = config.load_profile(CONFIG_DIR / "profile.md")
    assert "## Target" in text
    assert "## Known gaps" in text
    assert '"Target" and "Known gaps" headings' in text
