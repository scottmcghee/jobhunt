"""Tunable settings: one place for the knobs, overridable from the environment.

Each value resolves, highest priority first:

1. a CLI flag, where one exists (e.g. ``fetch --workers``);
2. an environment variable ``JOBHUNT_<SECTION>_<KEY>``, e.g. ``JOBHUNT_FETCH_WORKERS=16``;
   ``JOBHUNT_MODEL`` and ``JOBHUNT_BACKEND`` still work for the LLM model and backend;
3. ``config/settings.yaml`` (optional; config.example/settings.yaml documents every key);
4. the built-in default below.

Search preferences (titles, locations, ...) are not settings; they live in preferences.yaml.
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError, field_validator

from jobhunt import config, storage


class SettingsError(ValueError):
    """A setting that can't be used; the message names where it came from."""


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LLMSettings(_Section):
    backend: Literal["anthropic", "claude-code", "bedrock"] | None = None  # None: auto-detect
    model: str | None = None  # None: each backend's own default (see llm.py)
    claude_code_timeout: float = Field(180.0, gt=0)  # seconds per `claude -p` call
    score_max_tokens: int = Field(1600, ge=1)
    letter_max_tokens: int = Field(800, ge=1)
    body_chars: int = Field(12000, ge=1)  # characters of a posting sent to the model


class FetchSettings(_Section):
    workers: int = Field(32, ge=1)  # requests in flight across all hosts
    per_host: int = Field(6, ge=1)  # most in flight per Workday datacenter or API host
    start_per_host: int = Field(2, ge=1)  # where each host's adaptive limit starts
    timeout: float = Field(20.0, gt=0)  # seconds per request
    user_agent: str = "jobhunt/0.1 (+personal job search tool)"
    max_retries: int = Field(3, ge=0)  # retries on 429 (or 503 with Retry-After)
    max_retry_after: float = Field(120.0, ge=0)  # longer Retry-After: skip the board instead
    cooldown: float = Field(5.0, ge=0)  # seconds between two halvings of a host's limit
    breaker: int = Field(5, ge=1)  # refusals in a row before a host's other boards are skipped
    # Fetches in a row that find the board gone (a 404; for Workday also a 422, or a 403 "S22"
    # closed site) before it leaves companies.yaml. The name predates the Workday cases.
    prune_after_404s: int = Field(3, ge=1)
    transient_retries: int = Field(2, ge=0)  # retries on 500/502/504, connection errors, timeouts
    # Most requests sent per second, per rate group: the group names `fetch -v` stats print
    # ("workable", "lever", "workday:wd5"); *.eightfold.ai boards share "eightfold", an Eightfold
    # board on its own host has that host, and an Oracle board its tenant host
    # (eeho.fa.us2.oraclecloud.com); case-sensitive. Setting the map replaces it whole, so keep
    # "workable", "eightfold", "apply.careers.microsoft.com" and "apple" in it to keep their caps.
    # Workable's Cloudflare bans an IP for a burst of about 50 requests in 10 s, and 429s a
    # steady ~1.9/s after 900-1,250 requests (October 2026 full runs); *.eightfold.ai
    # answered 405 to a fetch's burst; Microsoft's careers site 429s at 1 per second; Apple's
    # pages are ~300 KB each.
    max_rate: dict[str, float] = Field(
        default_factory=lambda: {
            "workable": 1.4, "eightfold": 2.0, "apply.careers.microsoft.com": 0.5, "apple": 1.0
        }
    )

    # Most postings each search term may bring in, per search source (amazon, apple, eightfold,
    # oracle, phenom). A term with more hits stops there, with a warning. Amazon can't page past
    # 9,900.
    max_per_term: dict[
        Literal["amazon", "apple", "eightfold", "oracle", "phenom", "usajobs"],
        Annotated[StrictInt, Field(ge=1)],
    ] = Field(
        default_factory=lambda: {
            "amazon": 2000, "apple": 400, "eightfold": 500, "oracle": 1000, "phenom": 500,
            "usajobs": 2000,
        }
    )

    @field_validator("max_rate", "max_per_term", mode="before")
    @classmethod
    def _rate_from_json(cls, value: Any) -> Any:
        """An environment variable holds the map as JSON, e.g. '{"workable": 3}'."""
        if isinstance(value, str):
            try:
                return json.loads(value)
            except ValueError:
                raise ValueError('expected JSON like {"workable": 2}') from None
        return value

    @field_validator("max_rate")
    @classmethod
    def _positive(cls, value: dict[str, float]) -> dict[str, float]:
        # not "rate <= 0": NaN passes that and turns the cap off
        if bad := [g for g, rate in value.items() if not (math.isfinite(rate) and rate > 0)]:
            raise ValueError(f"rates must be finite and above 0 (got {', '.join(bad)})")
        return value


class PathSettings(_Section):
    data_dir: Path | None = None  # None: the repo's data/
    output_dir: Path | None = None  # None: the repo's output/

    @field_validator("data_dir", "output_dir", mode="before")
    @classmethod
    def _empty_is_default(cls, value: Any) -> Any:
        """``""`` means the default, as an empty environment variable does, not the repo root."""
        return None if value == "" else value

    @field_validator("data_dir", "output_dir")
    @classmethod
    def _from_repo(cls, value: Path | None) -> Path | None:
        """``~`` expands; a relative path is under the repo, like the defaults, from any cwd."""
        if value is None:
            return None
        try:
            return storage.DEFAULT_DATA_DIR.parent / value.expanduser()
        except RuntimeError:  # e.g. ~nosuchuser; pydantic only reports ValueError
            raise ValueError(f"can't expand '~' in {value}: no such home directory") from None


class SlugsSettings(_Section):
    check_workers: int = Field(4, ge=1)  # threads for `python -m jobhunt.slugs --check`


class UsajobsSettings(_Section):
    """USAJOBS's search API needs a key (free, from developer.usajobs.gov) and the email it was
    requested with, sent as the User-Agent. Secrets: kept out of reprs, never logged."""

    api_key: str | None = Field(None, repr=False)
    email: str | None = Field(None, repr=False)


class Settings(_Section):
    llm: LLMSettings = Field(default_factory=LLMSettings)
    fetch: FetchSettings = Field(default_factory=FetchSettings)
    paths: PathSettings = Field(default_factory=PathSettings)
    slugs: SlugsSettings = Field(default_factory=SlugsSettings)
    usajobs: UsajobsSettings = Field(default_factory=UsajobsSettings)


# Older names kept working; the JOBHUNT_<SECTION>_<KEY> form wins when both are set.
_ALIASES = {("llm", "model"): "JOBHUNT_MODEL", ("llm", "backend"): "JOBHUNT_BACKEND"}


def env_name(section: str, key: str) -> str:
    return f"JOBHUNT_{section}_{key}".upper()


def _keys() -> list[tuple[str, str]]:
    return [
        (section, key)
        for section, field in Settings.model_fields.items()
        for key in field.annotation.model_fields  # type: ignore[union-attr]
    ]


def env_names() -> list[str]:
    """Every environment variable a setting can be read from, aliases included."""
    return [env_name(s, k) for s, k in _keys()] + list(_ALIASES.values())


def load(path: Path | None = None, environ: Mapping[str, str] | None = None) -> Settings:
    """Settings from ``path`` (default: config/settings.yaml, if it exists) and the environment."""
    path = path or config.DEFAULT_CONFIG_DIR / "settings.yaml"
    environ = os.environ if environ is None else environ
    try:
        raw: Any = yaml.safe_load(path.read_text()) if path.exists() else None
    except yaml.YAMLError as e:
        raise SettingsError(f"{path}: not valid YAML: {e}") from None
    except (OSError, UnicodeDecodeError) as e:
        raise SettingsError(f"{path}: can't be read: {e}") from None
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise SettingsError(f"{path}: expected sections like 'fetch:' and 'llm:' at the top level")
    raw = {k: {} if v is None else v for k, v in raw.items()}  # a section, all keys commented
    try:
        Settings.model_validate(raw)
    except ValidationError as e:
        raise SettingsError(_describe(e, f"{path}")) from None

    sources: dict[tuple[str, str], str] = {}
    for section, key in _keys():
        for name in (env_name(section, key), _ALIASES.get((section, key))):
            if name and environ.get(name):
                raw.setdefault(section, {})[key] = environ[name]
                sources[(section, key)] = name
                break
    try:
        return Settings.model_validate(raw)
    except ValidationError as e:
        raise SettingsError(_describe(e, f"{path}", sources)) from None


def _describe(
    error: ValidationError, path: str, sources: Mapping[tuple[str, str], str] | None = None
) -> str:
    problems = []
    for err in error.errors():
        loc = tuple(str(part) for part in err["loc"][:2])
        where = (sources or {}).get(loc) or f"{path}: {'.'.join(loc)}"  # type: ignore[call-overload]
        problems.append(f"{where}: {err['msg']}")
    return "invalid setting: " + "; ".join(problems)
