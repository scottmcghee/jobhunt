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
    assert f.transient_retries == 2
    # Workable's Cloudflare bans bursts of about 50 in 10 s; Microsoft's Eightfold site 429s at 1/s
    assert f.max_rate == {"workable": 1.4, "eightfold": 2.0, "apply.careers.microsoft.com": 0.5, "apple": 1.0}
    assert f.max_per_term == {
        "amazon": 2000, "apple": 400, "eightfold": 500, "oracle": 1000, "phenom": 500, "usajobs": 2000
    }
    assert (s.paths.data_dir, s.paths.output_dir) == (None, None)  # None: the repo's data/ and output/
    assert (s.slugs.check_workers, s.slugs.check_progress_every) == (4, 50)
    assert (s.discover.max_attempts, s.discover.retry_after_hours) == (3, 24.0)


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


def test_a_section_with_every_key_commented_out_means_defaults(tmp_path):
    p = tmp_path / "settings.yaml"
    p.write_text("fetch:\n  # workers: 8\nllm:\n")
    assert settings.load(p, environ={}) == settings.Settings()
    assert settings.load(p, environ={"JOBHUNT_FETCH_WORKERS": "9"}).fetch.workers == 9


def test_a_yaml_syntax_error_names_the_file(tmp_path):
    p = tmp_path / "settings.yaml"
    p.write_text("llm:\n  model: [oops\n")
    with pytest.raises(settings.SettingsError, match=r"settings\.yaml"):
        settings.load(p, environ={})


def test_paths_expand_home_and_resolve_relative_ones_against_the_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)  # the current directory doesn't matter
    p = tmp_path / "settings.yaml"
    p.write_text("paths:\n  data_dir: ~/x\n  output_dir: letters\n")
    s = settings.load(p, environ={})
    repo = Path(__file__).resolve().parents[1]
    assert s.paths.data_dir == tmp_path / "x"
    assert s.paths.output_dir == repo / "letters"
    s = settings.load(p, environ={"JOBHUNT_PATHS_OUTPUT_DIR": "/abs/out"})
    assert s.paths.output_dir == Path("/abs/out")


def test_a_home_dir_that_cant_be_found_names_the_variable_or_the_key(tmp_path):
    bad = "~nosuchuser_zz/data"
    with pytest.raises(settings.SettingsError, match="JOBHUNT_PATHS_DATA_DIR"):
        settings.load(tmp_path / "nope.yaml", environ={"JOBHUNT_PATHS_DATA_DIR": bad})
    p = tmp_path / "settings.yaml"
    p.write_text(f"paths:\n  output_dir: {bad}\n")
    with pytest.raises(settings.SettingsError, match=r"settings\.yaml.*paths\.output_dir"):
        settings.load(p, environ={})


def test_a_file_that_cant_be_read_names_the_file(tmp_path):
    p = tmp_path / "settings.yaml"
    p.write_bytes(b"llm:\n  model: caf\xe9\n")  # not UTF-8
    with pytest.raises(settings.SettingsError, match=r"settings\.yaml"):
        settings.load(p, environ={})
    d = tmp_path / "dir" / "settings.yaml"
    d.mkdir(parents=True)  # a directory where the file should be
    with pytest.raises(settings.SettingsError, match=r"settings\.yaml"):
        settings.load(d, environ={})


def test_an_empty_path_in_the_file_means_the_default(tmp_path):
    p = tmp_path / "settings.yaml"
    p.write_text('paths:\n  data_dir: ""\n  output_dir: ""\n')
    s = settings.load(p, environ={})
    assert (s.paths.data_dir, s.paths.output_dir) == (None, None)  # not the repo root


def test_max_rate_from_the_file_and_from_json_in_the_environment(tmp_path):
    p = tmp_path / "settings.yaml"
    p.write_text("fetch:\n  max_rate:\n    workable: 1.5\n    bamboohr: 4\n")
    assert settings.load(p, environ={}).fetch.max_rate == {"workable": 1.5, "bamboohr": 4.0}
    env = {"JOBHUNT_FETCH_MAX_RATE": '{"workable": 3}'}
    assert settings.load(p, environ=env).fetch.max_rate == {"workable": 3.0}
    p.write_text("fetch:\n  max_rate: {}\n")
    assert settings.load(p, environ={}).fetch.max_rate == {}  # no caps at all


@pytest.mark.parametrize(
    "value",
    ["{\"workable\": 0}", "{\"workable\": -1}", "fast", "[2]",
     "{\"workable\": NaN}", "{\"workable\": Infinity}"],
)
def test_a_bad_max_rate_names_the_variable(tmp_path, value):
    with pytest.raises(settings.SettingsError, match="JOBHUNT_FETCH_MAX_RATE"):
        settings.load(tmp_path / "nope.yaml", environ={"JOBHUNT_FETCH_MAX_RATE": value})


def test_a_nan_max_rate_in_the_file_is_rejected(tmp_path):
    # NaN would pass a "<= 0" check and silently turn the cap off
    p = tmp_path / "settings.yaml"
    p.write_text("fetch:\n  max_rate:\n    workable: .nan\n")
    with pytest.raises(settings.SettingsError, match="fetch.max_rate"):
        settings.load(p, environ={})


def test_max_per_term_from_the_file_and_json_in_the_environment(tmp_path):
    p = tmp_path / "settings.yaml"
    p.write_text("fetch:\n  max_per_term:\n    amazon: 9900\n")
    assert settings.load(p, environ={}).fetch.max_per_term == {"amazon": 9900}
    env = {"JOBHUNT_FETCH_MAX_PER_TERM": '{"apple": 2000}'}
    assert settings.load(p, environ=env).fetch.max_per_term == {"apple": 2000}


@pytest.mark.parametrize(
    "value",
    ['{"apple": 0}', '{"apple": -5}', '{"apple": 1.5}', "lots", "[1]",
     '{"amazn": 9900}', '{"Amazon": 9900}', '{"amazon": true}'],  # a typo, a case slip, a bool
)
def test_a_bad_max_per_term_names_the_variable(tmp_path, value):
    with pytest.raises(settings.SettingsError, match="JOBHUNT_FETCH_MAX_PER_TERM"):
        settings.load(tmp_path / "nope.yaml", environ={"JOBHUNT_FETCH_MAX_PER_TERM": value})


def test_a_bad_max_per_term_key_in_the_file_is_rejected(tmp_path):
    p = tmp_path / "settings.yaml"
    p.write_text("fetch:\n  max_per_term:\n    Amazon: 9900\n")
    with pytest.raises(settings.SettingsError, match="fetch.max_per_term.*'amazon'"):
        settings.load(p, environ={})


def test_usajobs_credentials_come_from_the_file_or_the_environment(tmp_path):
    p = tmp_path / "settings.yaml"
    p.write_text("usajobs:\n  api_key: from-file\n  email: me@example.com\n")
    s = settings.load(p, environ={})
    assert (s.usajobs.api_key, s.usajobs.email) == ("from-file", "me@example.com")
    s = settings.load(p, environ={"JOBHUNT_USAJOBS_API_KEY": "from-env"})
    assert s.usajobs.api_key == "from-env"
    assert settings.Settings().usajobs.api_key is None  # off until set


def test_the_usajobs_key_and_email_never_show_in_a_repr(tmp_path):
    s = settings.Settings.model_validate({"usajobs": {"api_key": "s3cret", "email": "me@example.com"}})
    assert "s3cret" not in repr(s) and "me@example.com" not in repr(s)
    assert "s3cret" not in str(s)
