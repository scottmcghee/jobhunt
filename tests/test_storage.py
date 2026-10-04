"""Seen-set idempotency and JSONL round-trips."""

from __future__ import annotations

from jobhunt import storage
from jobhunt.schema import Letter


def test_seen_set_roundtrip(tmp_path):
    p = tmp_path / "seen.json"
    s = storage.SeenSet(p)
    assert len(s) == 0
    s.add("greenhouse:x:1")
    s.add("greenhouse:x:1")  # idempotent
    s.save()
    s2 = storage.SeenSet(p)
    assert "greenhouse:x:1" in s2 and len(s2) == 1


def test_jobs_jsonl_roundtrip(tmp_path, platform_director_job):
    storage.append_jsonl(tmp_path / "jobs.jsonl", platform_director_job.model_dump())
    jobs = storage.load_jobs(tmp_path)
    assert len(jobs) == 1 and jobs[0].key == platform_director_job.key


def test_write_letter_names_file_by_company_and_title(tmp_path):
    letter = Letter(
        job_key="greenhouse:x:1",
        company="Example Corp",
        title="Director, Platform Engineering",
        modules_used=["a", "b"],
        text="Dear hiring manager.\n",
        model="t",
    )
    path = storage.write_letter(letter, tmp_path)
    assert path.name == "example-corp__director-platform-engineering.md"
    assert "greenhouse:x:1" in path.read_text()


def test_miss_ledger_counts_resets_and_persists(tmp_path):
    p = tmp_path / "misses.json"
    m = storage.MissLedger(p)
    assert m.miss("greenhouse:x") == 1
    assert m.miss("greenhouse:x") == 2
    m.clear("greenhouse:never-missed")  # clearing an unknown key is a no-op
    m.save()

    m2 = storage.MissLedger(p)
    assert m2.miss("greenhouse:x") == 3
    m2.clear("greenhouse:x")
    assert m2.miss("greenhouse:x") == 1


def test_failed_save_leaves_previous_file_intact(tmp_path, monkeypatch):
    p = tmp_path / "seen.json"
    s = storage.SeenSet(p)
    s.add("greenhouse:x:1")
    s.save()
    s.add("greenhouse:x:2")

    def interrupted(src, dst):
        raise KeyboardInterrupt

    monkeypatch.setattr(storage.os, "replace", interrupted)
    try:
        s.save()
    except KeyboardInterrupt:
        pass
    reloaded = storage.SeenSet(p)
    assert "greenhouse:x:1" in reloaded and len(reloaded) == 1
