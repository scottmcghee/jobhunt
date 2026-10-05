"""Tunable settings: built-in defaults < config/settings.yaml < JOBHUNT_* env vars (< CLI flags)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from jobhunt import settings
from tests.conftest import CONFIG_DIR

TEMPLATE = CONFIG_DIR / "settings.yaml"


def test_defaults_are_todays_values():
    s = settings.Settings()
    assert (s.llm.backend, s.llm.model) == (None, None)  # auto-detect, each backend's own default
    assert (s.llm.claude_code_timeout, s.llm.score_max_tokens, s.llm.letter_max_tokens) == (180, 1600, 800)
    assert s.llm.body_chars == 12000
    f = s.fetch
    assert (f.workers, f.per_host, f.start_per_host, f.timeout) == (32, 6, 2, 20.0)
    assert f.user_agent == "jobhunt/0.1 (+personal job search tool)"
    assert (f.max_retries, f.max_retry_after, f.cooldown, f.breaker, f.prune_after_404s) == (3, 120.0, 5.0, 5, 3)
    assert (s.paths.data_dir, s.paths.output_dir) == (None, None)  # None: the repo's data/ and output/
    assert s.slugs.check_workers == 4


def test_the_template_documents_every_setting_at_its_default():
    raw = yaml.safe_load(TEMPLATE.read_text())
    for section, model in settings.Settings.model_fields.items():
        assert set(raw[section]) == set(model.annotation.model_fields), section
    assert settings.load(TEMPLATE, environ={}) == settings.Settings()
    text = TEMPLATE.read_text()
    for name in settings.env_names():
        assert name in text, f"{name} isn't mentioned in config.example/settings.yaml"


def test_a_missing_file_means_defaults(tmp_path):
    assert settings.load(tmp_path / "nope.yaml", environ={}) == settings.Settings()


def test_the_file_overrides_defaults(tmp_path):
    p = tmp_path / "settings.yaml"
    p.write_text("fetch:\n  workers: 8\nllm:\n  model: claude-opus-5-5\n")
    s = settings.load(p, environ={})
    assert s.fetch.workers == 8 and s.fetch.per_host == 6
    assert s.llm.model == "claude-opus-5-5"


def test_an_empty_file_means_defaults(tmp_path):
    p = tmp_path / "settings.yaml"
    p.write_text("# nothing set\n")
    assert settings.load(p, environ={}) == settings.Settings()


def test_env_vars_override_the_file(tmp_path):
    p = tmp_path / "settings.yaml"
    p.write_text("fetch:\n  workers: 8\n")
    env = {"JOBHUNT_FETCH_WORKERS": "16", "JOBHUNT_LLM_SCORE_MAX_TOKENS": "2000", "JOBHUNT_PATHS_DATA_DIR": "/tmp/jh"}
    s = settings.load(p, environ=env)
    assert s.fetch.workers == 16
    assert s.llm.score_max_tokens == 2000
    assert s.paths.data_dir == Path("/tmp/jh")


def test_an_empty_env_var_is_ignored(tmp_path):
    assert settings.load(tmp_path / "nope.yaml", environ={"JOBHUNT_FETCH_WORKERS": ""}).fetch.workers == 32


def test_the_old_short_names_still_work_and_the_long_ones_win(tmp_path):
    none = tmp_path / "nope.yaml"
    s = settings.load(none, environ={"JOBHUNT_MODEL": "sonnet", "JOBHUNT_BACKEND": "claude-code"})
    assert (s.llm.model, s.llm.backend) == ("sonnet", "claude-code")
    s = settings.load(none, environ={"JOBHUNT_MODEL": "sonnet", "JOBHUNT_LLM_MODEL": "opus"})
    assert s.llm.model == "opus"


def test_a_bad_env_value_names_the_variable(tmp_path):
    with pytest.raises(settings.SettingsError, match="JOBHUNT_FETCH_WORKERS"):
        settings.load(tmp_path / "nope.yaml", environ={"JOBHUNT_FETCH_WORKERS": "lots"})
    with pytest.raises(settings.SettingsError, match="JOBHUNT_FETCH_PER_HOST"):
        settings.load(tmp_path / "nope.yaml", environ={"JOBHUNT_FETCH_PER_HOST": "0"})
    with pytest.raises(settings.SettingsError, match="JOBHUNT_BACKEND"):
        settings.load(tmp_path / "nope.yaml", environ={"JOBHUNT_BACKEND": "gpt"})


def test_a_bad_file_names_the_file_and_the_key(tmp_path):
    p = tmp_path / "settings.yaml"
    p.write_text("fetch:\n  wrokers: 8\n")
    with pytest.raises(settings.SettingsError, match=r"settings\.yaml.*fetch\.wrokers"):
        settings.load(p, environ={})
    p.write_text("fetch:\n  timeout: soon\n")
    with pytest.raises(settings.SettingsError, match=r"settings\.yaml.*fetch\.timeout"):
        settings.load(p, environ={})
    p.write_text("- not a mapping\n")
    with pytest.raises(settings.SettingsError, match=r"settings\.yaml"):
        settings.load(p, environ={})


def test_load_defaults_to_the_config_dir_and_the_process_environment(monkeypatch):
    monkeypatch.setenv("JOBHUNT_FETCH_BREAKER", "9")
    assert settings.load().fetch.breaker == 9  # reads config.example/settings.yaml in tests


def test_env_names_follow_section_and_key():
    names = settings.env_names()
    assert "JOBHUNT_FETCH_PER_HOST" in names and "JOBHUNT_SLUGS_CHECK_WORKERS" in names
    assert "JOBHUNT_MODEL" in names and "JOBHUNT_BACKEND" in names  # the short aliases
