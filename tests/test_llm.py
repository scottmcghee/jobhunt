"""Backend selection and the Claude Code CLI completer, with the subprocess faked."""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

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


class FakeMessages:
    """Records the request; answers like the SDK: a list of typed content blocks."""

    def __init__(self, blocks):
        self.blocks, self.calls = blocks, []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(content=self.blocks)


def _fake_client(*blocks):
    return SimpleNamespace(messages=FakeMessages(list(blocks) or [SimpleNamespace(type="text", text="ok")]))


def test_bedrock_is_only_chosen_explicitly(monkeypatch):
    monkeypatch.delenv("JOBHUNT_BACKEND", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "token")  # AWS credentials alone don't switch backends
    monkeypatch.setattr(llm.shutil, "which", lambda name: None)
    assert llm.backend_name() == "anthropic"
    monkeypatch.setenv("JOBHUNT_BACKEND", "bedrock")
    assert llm.backend_name() == "bedrock"


def test_bedrock_completer_sends_a_plain_messages_request(monkeypatch):
    monkeypatch.delenv("JOBHUNT_MODEL", raising=False)
    client = _fake_client(SimpleNamespace(type="text", text='{"score": '), SimpleNamespace(type="text", text="7}"))
    complete = llm.bedrock_completer(client=client)

    assert complete("SYS", "USER", 800) == '{"score": 7}'
    (call,) = client.messages.calls
    assert call["model"] == "anthropic.claude-sonnet-5-5"
    assert call["system"] == "SYS" and call["max_tokens"] == 800
    assert call["messages"] == [{"role": "user", "content": "USER"}]
    assert call["thinking"] == {"type": "between_tools"}  # Sonnet 5.5's way to turn thinking off


def test_bedrock_completer_ignores_non_text_blocks():
    client = _fake_client(SimpleNamespace(type="thinking", thinking="hmm"), SimpleNamespace(type="text", text="answer"))
    assert llm.bedrock_completer(client=client)("SYS", "USER", 100) == "answer"


def test_bedrock_model_override_leaves_thinking_to_the_model(monkeypatch):
    # between_tools is a Sonnet 5.5 setting; other models would reject it
    monkeypatch.setenv("JOBHUNT_MODEL", "anthropic.claude-opus-5-5")
    client = _fake_client()
    llm.bedrock_completer(client=client)("SYS", "USER", 100)
    (call,) = client.messages.calls
    assert call["model"] == "anthropic.claude-opus-5-5" and "thinking" not in call


def test_make_completer_builds_a_bedrock_client(monkeypatch):
    import anthropic

    built = []

    class FakeMantle:
        def __init__(self, **kwargs):
            built.append(kwargs)
            self.messages = FakeMessages([SimpleNamespace(type="text", text="hi")])

    monkeypatch.setenv("JOBHUNT_BACKEND", "bedrock")
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "token")
    monkeypatch.setattr(anthropic, "AnthropicBedrockMantle", FakeMantle)
    assert llm.make_completer()("SYS", "USER", 10) == "hi"
    assert built == [{}]  # region and credentials come from the standard AWS environment


@pytest.mark.parametrize(
    ("backend", "expected"),
    [("anthropic", "claude-sonnet-4-5"), ("claude-code", "sonnet"), ("bedrock", "anthropic.claude-sonnet-5-5")],
)
def test_model_name_reports_each_backends_default(monkeypatch, backend, expected):
    monkeypatch.setenv("JOBHUNT_BACKEND", backend)
    monkeypatch.delenv("JOBHUNT_MODEL", raising=False)
    assert llm.model_name() == expected
    monkeypatch.setenv("JOBHUNT_MODEL", "custom")
    assert llm.model_name() == "custom"


def test_bedrock_without_api_key_or_boto3_says_how_to_fix(monkeypatch):
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)
    monkeypatch.delenv("ANTHROPIC_AWS_API_KEY", raising=False)
    monkeypatch.setattr(llm.importlib.util, "find_spec", lambda name: None)
    with pytest.raises(RuntimeError, match=r'pip install -e "\.\[bedrock\]"'):
        llm.bedrock_completer()


def test_bedrock_api_key_needs_no_boto3(monkeypatch):
    import anthropic

    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "token")
    monkeypatch.setattr(llm.importlib.util, "find_spec", lambda name: None)
    monkeypatch.setattr(anthropic, "AnthropicBedrockMantle", lambda **kw: _fake_client())
    assert llm.bedrock_completer()("SYS", "USER", 10) == "ok"
