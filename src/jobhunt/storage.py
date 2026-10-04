"""Runtime state on disk. Deliberately boring: JSON and JSONL files under data/.

- seen.json    : {job_key: first_seen_iso}. The idempotency ledger.
- misses.json  : {board_key: consecutive HTTP 404s}. Boards that answered fine are absent.
- jobs.jsonl   : every Job that passed the filter, appended once.
- scores.jsonl : every ScoredJob, appended once per job.
- output/      : one Markdown letter per generated job.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

from jobhunt.schema import Job, Letter, ScoredJob

DEFAULT_DATA_DIR = Path(__file__).resolve().parents[2] / "data"
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[2] / "output"


class SeenSet:
    """Persistent set of job keys we've already processed."""

    def __init__(self, path: Path):
        self.path = path
        self._seen: dict[str, str] = {}
        if path.exists():
            self._seen = json.loads(path.read_text() or "{}")

    def __contains__(self, key: str) -> bool:
        return key in self._seen

    def __len__(self) -> int:
        return len(self._seen)

    def add(self, key: str) -> None:
        self._seen.setdefault(key, datetime.now(UTC).isoformat())

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._seen, indent=2, sort_keys=True))


class MissLedger:
    """Consecutive HTTP 404 count per board, keyed by ``Company.key``."""

    def __init__(self, path: Path):
        self.path = path
        self.counts: dict[str, int] = {}
        if path.exists():
            self.counts = json.loads(path.read_text() or "{}")

    def miss(self, key: str) -> int:
        """Record one more 404 in a row and return the new count."""
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    def clear(self, key: str) -> None:
        self.counts.pop(key, None)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.counts, indent=2, sort_keys=True))


def append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def load_jobs(data_dir: Path) -> list[Job]:
    return [Job.model_validate(r) for r in read_jsonl(data_dir / "jobs.jsonl")]


def load_scores(data_dir: Path) -> list[ScoredJob]:
    return [ScoredJob.model_validate(r) for r in read_jsonl(data_dir / "scores.jsonl")]


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:60]


def write_letter(letter: Letter, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{_slug(letter.company)}__{_slug(letter.title)}.md"
    header = (
        f"<!-- {letter.job_key} | modules: {', '.join(letter.modules_used)} | "
        f"{letter.model} | {letter.generated_at} -->\n\n"
    )
    path.write_text(header + letter.text, encoding="utf-8")
    return path
