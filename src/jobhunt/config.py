"""Load and validate the YAML/Markdown configuration under config/.

config/ is personal and gitignored. config.example/ holds committed templates to start from.
"""

from __future__ import annotations

import re
import textwrap
from collections.abc import Collection
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from jobhunt.schema import Company

DEFAULT_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


class ConfigMissing(FileNotFoundError):
    """A config file isn't there, usually because config/ hasn't been made from the templates."""


def _read(path: Path) -> str:
    if not path.exists():
        hint = "To start from the templates: cp -R config.example config"
        raise ConfigMissing(f"{path} not found. {hint}")
    return path.read_text()


class TitleRules(BaseModel):
    must_include_any: list[str] = Field(default_factory=list)
    must_exclude_any: list[str] = Field(default_factory=list)


class DomainRules(BaseModel):
    must_include_any: list[str] = Field(default_factory=list)


class LocationRules(BaseModel):
    allow_remote: bool = True
    accept_any: list[str] = Field(default_factory=list)
    reject_any: list[str] = Field(default_factory=list)
    # When set, a role that is explicitly not remote (on-site or hybrid) must be in one of these
    # places; for such roles it replaces accept_any. Empty = off.
    onsite_accept_any: list[str] = Field(default_factory=list)
    # When true (and onsite_accept_any is set), a role whose remote status is unknown is checked
    # like an on-site one if neither its location nor its text mentions remote work, unless its
    # whole location is one of country_wide_any (e.g. just "United States"). With allow_remote
    # false the rule still applies, but mentioning remote work no longer exempts a role. Off by
    # default.
    unknown_remote_is_onsite: bool = False
    country_wide_any: list[str] = Field(default_factory=list)


class ScoringRules(BaseModel):
    min_score_for_letter: int = Field(default=7, ge=1, le=10)


class Preferences(BaseModel):
    title: TitleRules = Field(default_factory=TitleRules)
    domain: DomainRules = Field(default_factory=DomainRules)
    location: LocationRules = Field(default_factory=LocationRules)
    scoring: ScoringRules = Field(default_factory=ScoringRules)


@dataclass
class KitModule:
    id: str
    title: str
    use_when: list[str]
    text: str


@dataclass
class Kit:
    opening: str
    closing: str
    modules: dict[str, KitModule] = field(default_factory=dict)


_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n(.*)\Z", re.S)
_LIST_ITEM = re.compile(r"^( +)- ")


def load_companies(path: Path | None = None) -> list[Company]:
    path = path or DEFAULT_CONFIG_DIR / "companies.yaml"
    raw = yaml.safe_load(_read(path)) or {}
    return [Company.model_validate(c) for c in raw.get("companies", [])]


def _entry_spans(lines: list[str]) -> list[tuple[int, int]]:
    """Line ranges of each entry in the companies list.

    An entry owns the indented comment lines directly above it and the blank lines after it.
    """
    items = [(i, m.group(1)) for i, line in enumerate(lines) if (m := _LIST_ITEM.match(line))]
    if not items:
        return []
    indent = items[0][1]
    starts = [i for i, ind in items if ind == indent]
    for n, i in enumerate(starts):
        while i > 0 and lines[i - 1].startswith(" ") and lines[i - 1].lstrip().startswith("#"):
            i -= 1
        starts[n] = i
    end = next(
        (i for i in range(starts[-1] + 1, len(lines)) if lines[i][:1] not in ("", " ", "\n", "#")),
        len(lines),
    )
    return list(zip(starts, [*starts[1:], end], strict=True))


def remove_companies(path: Path, keys: Collection[str]) -> list[str]:
    """Delete entries whose ``Company.key`` is in ``keys``. Returns the names removed.

    Edits the file as text, so comments and spacing elsewhere stay exactly as written.
    """
    lines = path.read_text().splitlines(keepends=True)
    removed: list[str] = []
    drop: set[int] = set()
    for lo, hi in _entry_spans(lines):
        entry = yaml.safe_load(textwrap.dedent("".join(lines[lo:hi])))[0]
        company = Company.model_validate(entry)
        if company.key in keys:
            removed.append(company.name)
            drop.update(range(lo, hi))
    if removed:
        kept = "".join(line for i, line in enumerate(lines) if i not in drop)
        path.write_text(kept.rstrip("\n") + "\n")
    return removed


def load_preferences(path: Path | None = None) -> Preferences:
    path = path or DEFAULT_CONFIG_DIR / "preferences.yaml"
    raw = yaml.safe_load(_read(path)) or {}
    return Preferences.model_validate(raw)


def load_profile(path: Path | None = None) -> str:
    path = path or DEFAULT_CONFIG_DIR / "profile.md"
    return _read(path)


def parse_module(text: str) -> KitModule:
    """Parse a kit module file: YAML frontmatter followed by the module body."""
    m = _FRONTMATTER.match(text)
    if not m:
        raise ValueError("kit module missing frontmatter")
    meta = yaml.safe_load(m.group(1)) or {}
    body = m.group(2).strip()
    return KitModule(
        id=str(meta["id"]),
        title=str(meta.get("title", meta["id"])),
        use_when=[str(x) for x in meta.get("use_when", [])],
        text=body,
    )


def load_kit(kit_dir: Path | None = None) -> Kit:
    kit_dir = kit_dir or DEFAULT_CONFIG_DIR / "kit"
    opening = _read(kit_dir / "opening.md").strip()
    closing = _read(kit_dir / "closing.md").strip()
    modules: dict[str, KitModule] = {}
    for p in sorted((kit_dir / "modules").glob("*.md")):
        mod = parse_module(p.read_text())
        if mod.id in modules:
            raise ValueError(f"duplicate kit module id: {mod.id}")
        modules[mod.id] = mod
    return Kit(opening=opening, closing=closing, modules=modules)
