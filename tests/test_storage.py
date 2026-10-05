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


def test_write_letter_names_file_by_company_title_and_key_hash(tmp_path):
    letter = Letter(
        job_key="greenhouse:x:1",
        company="Example Corp",
        title="Director, Platform Engineering",
        modules_used=["a", "b"],
        text="Dear hiring manager.\n",
        model="t",
    )
    path = storage.write_letter(letter, tmp_path)
    assert path.name == "example-corp__director-platform-engineering__275f06b4bb.md"
    assert "greenhouse:x:1" in path.read_text()


def test_write_letter_keeps_same_posting_on_two_boards_apart(tmp_path):
    # One Workday tenant, two sites: same company name, title, and external id; different keys.
    keys = {
        "workday:adobe/external_experienced:Senior-Software-Engineer_R1002-1",
        "workday:adobe/university:Senior-Software-Engineer_R1002-1",
    }
    for key in keys:
        letter = Letter(job_key=key, company="Adobe", title="Senior Software Engineer", modules_used=[], text="x", model="t")
        storage.write_letter(letter, tmp_path)
    assert len(list(tmp_path.glob("*.md"))) == 2
    assert storage.lettered_job_keys(tmp_path) == keys


def test_write_letter_bounds_the_filename(tmp_path):
    letter = Letter(
        job_key="workday:tenant/site:" + "x" * 300,
        company="C" * 200,
        title="T" * 200,
        modules_used=[],
        text="x",
        model="t",
    )
    path = storage.write_letter(letter, tmp_path)
    assert path.exists() and len(path.name.encode()) <= 255


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


def test_lettered_job_keys_reads_letter_headers(tmp_path):
    letter = Letter(job_key="greenhouse:acme:1", company="Acme", title="VP Eng", modules_used=["m"], text="Hi", model="t")
    storage.write_letter(letter, tmp_path)
    (tmp_path / "notes.md").write_text("no header here\n")
    (tmp_path / "empty.md").write_text("")
    (tmp_path / "latin1.md").write_bytes(b"caf\xe9 notes\n")  # not UTF-8; must not crash
    assert storage.lettered_job_keys(tmp_path) == {"greenhouse:acme:1"}
    assert storage.lettered_job_keys(tmp_path / "missing") == set()
