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


def test_lettered_job_keys_finds_keys_with_spaces(tmp_path):
    # Ashby board names keep their spelling, so a slug (and the job key) can contain spaces.
    letter = Letter(job_key="ashby:Some Co:3f2a", company="Some Co", title="VP Eng", modules_used=["m"], text="Hi", model="t")
    storage.write_letter(letter, tmp_path)
    assert storage.lettered_job_keys(tmp_path) == {"ashby:Some Co:3f2a"}


def test_seen_set_counts_jobs_already_in_jobs_jsonl(tmp_path, platform_director_job):
    # A run killed after appending a board's jobs but before saving seen.json must not
    # record them again next time.
    storage.append_jsonl(tmp_path / "jobs.jsonl", platform_director_job.model_dump())
    seen = storage.SeenSet(tmp_path / "seen.json", jobs=tmp_path / "jobs.jsonl")
    assert platform_director_job.key in seen
    assert platform_director_job.key not in storage.SeenSet(tmp_path / "seen.json")


def test_seen_set_without_a_jobs_file(tmp_path):
    seen = storage.SeenSet(tmp_path / "seen.json", jobs=tmp_path / "jobs.jsonl")
    assert len(seen) == 0


def test_fetch_progress_lifecycle(tmp_path):
    path = tmp_path / "fetch_progress.txt"
    progress = storage.FetchProgress(path)
    assert progress.done == set() and not progress.exists()

    progress.start()
    progress.mark("greenhouse:a")
    progress.mark("lever:b")
    assert storage.FetchProgress(path).done == {"greenhouse:a", "lever:b"}  # on disk at once

    progress.start()  # a fresh run forgets the last one
    assert storage.FetchProgress(path).done == set() and path.exists()

    progress.finish()
    assert not path.exists()
    progress.finish()  # already gone is fine


def test_fetch_progress_keeps_keys_with_spaces(tmp_path):
    progress = storage.FetchProgress(tmp_path / "fetch_progress.txt")
    progress.start()
    progress.mark("ashby:Acme Labs")  # Ashby slugs keep spaces
    assert storage.FetchProgress(progress.path).done == {"ashby:Acme Labs"}
