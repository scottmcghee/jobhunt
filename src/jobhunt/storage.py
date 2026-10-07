"""Runtime state on disk. Deliberately boring: JSON and JSONL files under data/.

- seen.json    : {job_key: first_seen_iso}. The idempotency ledger.
- fetch_progress.txt : board keys an interrupted `fetch` finished, one a line, for `--resume`.
- misses.json  : {board_key: fetches in a row that found the board gone (see cli._board_gone)}.
                 Boards that answered fine are absent.
- jobs.jsonl   : every Job that passed the filter, appended once.
- scores.jsonl : every ScoredJob, appended once per job.
- output/      : one Markdown letter per generated job.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import UTC, datetime
from pathlib import Path

from jobhunt.schema import Job, Letter, ScoredJob

log = logging.getLogger(__name__)

DEFAULT_DATA_DIR = Path(__file__).resolve().parents[2] / "data"
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[2] / "output"


def _write_atomic(path: Path, text: str) -> None:
    """Replace ``path`` in one step: an interrupted save leaves the old file, not half a new one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


class SeenSet:
    """Persistent set of job keys we've already processed.

    With ``jobs`` (jobs.jsonl), every job recorded there counts as seen too, so a run killed
    after appending a board's jobs but before saving this set doesn't record them again.
    """

    def __init__(self, path: Path, jobs: Path | None = None):
        self.path = path
        self._seen: dict[str, str] = {}
        if path.exists():
            self._seen = json.loads(path.read_text() or "{}")
        lines = jobs.read_text(encoding="utf-8").splitlines() if jobs and jobs.exists() else []
        for line in filter(None, lines):
            try:
                self.add(Job.model_validate(json.loads(line)).key)
            except ValueError:  # half a line from a run killed mid-append
                log.warning("%s: skipped a line that doesn't parse: %.60s", jobs, line)

    def __contains__(self, key: str) -> bool:
        return key in self._seen

    def __len__(self) -> int:
        return len(self._seen)

    def add(self, key: str) -> None:
        self._seen.setdefault(key, datetime.now(UTC).isoformat())

    def save(self) -> None:
        _write_atomic(self.path, json.dumps(self._seen, indent=2, sort_keys=True))


class MissLedger:
    """Per board (by ``Company.key``): fetches in a row that found it gone (see cli._board_gone)."""

    def __init__(self, path: Path):
        self.path = path
        self.counts: dict[str, int] = {}
        if path.exists():
            self.counts = json.loads(path.read_text() or "{}")

    def miss(self, key: str) -> int:
        """Record one more fetch in a row that found the board gone, and return the new count."""
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    def clear(self, key: str) -> None:
        self.counts.pop(key, None)

    def save(self) -> None:
        _write_atomic(self.path, json.dumps(self.counts, indent=2, sort_keys=True))


class FetchProgress:
    """Board keys the current full fetch has finished, so an interrupted one can be resumed.

    Each key is appended as its board is recorded, so the file is current even after a kill.
    """

    def __init__(self, path: Path):
        self.path = path
        self.done: set[str] = set()
        if path.exists():
            self.done = set(path.read_text(encoding="utf-8").splitlines()) - {""}

    def exists(self) -> bool:
        return self.path.exists()

    def start(self) -> None:
        """Begin a fresh run: forget any earlier one."""
        _write_atomic(self.path, "")
        self.done = set()

    def mark(self, key: str) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            f.write(key + "\n")
        self.done.add(key)

    def finish(self) -> None:
        """The run completed: nothing is left to resume."""
        self.path.unlink(missing_ok=True)


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


# Up to write_letter's " | modules: " separator, not the first space: Ashby slugs keep spaces.
_LETTER_HEADER = re.compile(r"<!-- (.+?) \| modules: ")


def lettered_job_keys(output_dir: Path) -> set[str]:
    """Job keys that already have a letter, read from each letter's header comment.

    The filename can't tell: it comes from the company name, which may be the model's reading.
    """
    keys = set()
    for path in output_dir.glob("*.md") if output_dir.is_dir() else []:
        with path.open(encoding="utf-8", errors="replace") as f:  # headers are ASCII
            if m := _LETTER_HEADER.match(f.readline()):
                keys.add(m.group(1))
    return keys


def write_letter(letter: Letter, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    # A short hash of the job key keeps two postings with the same company and title apart
    # (even one requisition on two boards of a tenant) and keeps the name a bounded length.
    key_hash = hashlib.sha1(letter.job_key.encode()).hexdigest()[:10]
    path = output_dir / f"{_slug(letter.company)}__{_slug(letter.title)}__{key_hash}.md"
    header = (
        f"<!-- {letter.job_key} | modules: {', '.join(letter.modules_used)} | "
        f"{letter.model} | {letter.generated_at} -->\n\n"
    )
    path.write_text(header + letter.text, encoding="utf-8")
    return path
