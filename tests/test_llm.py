"""Backend selection and the Claude Code CLI completer, with the subprocess faked."""

from __future__ import annotations

import json
import subprocess

import pytest

from jobhunt import llm


def test_backend_prefers_explicit_env(monkeypatch):
    monkeypatch.setenv("JOBHUNT_BACKEND", "claude-code")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    assert llm.backend_name() == "claude-code"


def test_backend_uses_api_key_when_present(monkeypatch):
    monkeypatch.delenv("JOBHUNT_BACKEND", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    assert llm.backend_name() == "anthropic"


def test_backend_falls_back_to_cli_when_installed(monkeypatch):
    monkeypatch.delenv("JOBHUNT_BACKEND", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(llm.shutil, "which", lambda name: "/usr/local/bin/claude")
    assert llm.backend_name() == "claude-code"


def test_make_completer_rejects_unknown(monkeypatch):
    monkeypatch.setenv("JOBHUNT_BACKEND", "gpt")
    with pytest.raises(ValueError):
        llm.make_completer()


def test_claude_code_command_is_toolless_single_turn():
    argv = llm.claude_code_command("SYS", "sonnet")
    assert argv[:2] == ["claude", "-p"]
    assert "--system-prompt" in argv and argv[argv.index("--system-prompt") + 1] == "SYS"
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--max-turns") + 1] == "1"
    assert "--no-session-persistence" in argv


def test_claude_code_completer_parses_result():
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"], seen["input"] = argv, kw["input"]
        return subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps({"result": "{\"score\": 7}", "is_error": False}), stderr=""
        )

    complete = llm.claude_code_completer(model="sonnet", runner=fake_run)
    out = complete("SYS", "USER", 100)
    assert out == "{\"score\": 7}"
    assert seen["input"] == "USER"
    assert "sonnet" in seen["argv"]


def test_claude_code_completer_raises_on_nonzero_exit():
    def fake_run(argv, **kw):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="Not logged in")

    complete = llm.claude_code_completer(runner=fake_run)
    with pytest.raises(RuntimeError, match="Not logged in"):
        complete("SYS", "USER", 100)


def test_claude_code_completer_raises_on_is_error():
    def fake_run(argv, **kw):
        return subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps({"result": "rate limited", "is_error": True}), stderr=""
        )

    complete = llm.claude_code_completer(runner=fake_run)
    with pytest.raises(RuntimeError, match="rate limited"):
        complete("SYS", "USER", 100)
