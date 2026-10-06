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

    def __init__(self, blocks, **response):
        self.blocks, self.calls, self.response = blocks, [], response

    def create(self, **kwargs):
        self.calls.append(kwargs)
        fields = {"stop_reason": "end_turn", "stop_details": None, "usage": SimpleNamespace(iterations=None)}
        return SimpleNamespace(content=self.blocks, **{**fields, **self.response})


def _fake_client(*blocks, **response):
    messages = FakeMessages(list(blocks) or [SimpleNamespace(type="text", text="ok")], **response)
    # the SDK serves beta requests from client.beta.messages; one recorder covers both
    return SimpleNamespace(messages=messages, beta=SimpleNamespace(messages=messages))


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
    [("anthropic", "claude-sonnet-5-5"), ("claude-code", "sonnet"), ("bedrock", "anthropic.claude-sonnet-5-5")],
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



def test_anthropic_completer_defaults_to_sonnet_5_5_with_fallback(monkeypatch):
    monkeypatch.delenv("JOBHUNT_MODEL", raising=False)
    client = _fake_client(SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text="answer"))
    complete = llm.anthropic_completer(client=client)

    assert complete("SYS", "USER", 1600) == "answer"
    (call,) = client.messages.calls
    assert call["model"] == "claude-sonnet-5-5"
    assert call["system"] == "SYS" and call["max_tokens"] == 1600
    assert call["messages"] == [{"role": "user", "content": "USER"}]
    assert call["thinking"] == {"type": "between_tools"}
    assert call["fallbacks"] == "default"
    assert call["betas"] == ["server-side-fallback-2026-07-01"]


def test_anthropic_completer_sends_fallback_only_where_supported(monkeypatch):
    monkeypatch.setenv("JOBHUNT_MODEL", "claude-haiku-4-5")
    client = _fake_client()
    llm.anthropic_completer(client=client)("SYS", "USER", 100)
    (call,) = client.messages.calls
    assert call["model"] == "claude-haiku-4-5"
    assert "fallbacks" not in call and "betas" not in call and "thinking" not in call


def test_anthropic_completer_fallback_without_thinking_off_for_opus(monkeypatch):
    monkeypatch.setenv("JOBHUNT_MODEL", "claude-opus-5-5")
    client = _fake_client()
    llm.anthropic_completer(client=client)("SYS", "USER", 100)
    (call,) = client.messages.calls
    assert call["fallbacks"] == "default" and "thinking" not in call


@pytest.mark.parametrize("make", [llm.anthropic_completer, llm.bedrock_completer])
def test_a_refusal_raises_a_clear_error(make):
    messages = FakeMessages([], stop_reason="refusal", stop_details=SimpleNamespace(category="cyber"))
    client = SimpleNamespace(messages=messages, beta=SimpleNamespace(messages=messages))
    with pytest.raises(ValueError, match="declined.*cyber"):
        make(client=client)("SYS", "USER", 100)


def test_an_uncategorized_refusal_still_says_so():
    messages = FakeMessages([], stop_reason="refusal", stop_details=SimpleNamespace(category=None))
    client = SimpleNamespace(messages=messages, beta=SimpleNamespace(messages=messages))
    with pytest.raises(ValueError, match="declined.*uncategorized"):
        llm.anthropic_completer(client=client)("SYS", "USER", 100)


def test_a_fallback_answer_is_logged(caplog):
    iterations = [SimpleNamespace(type="message"), SimpleNamespace(type="fallback_message")]
    client = _fake_client(model="claude-opus-4-8", usage=SimpleNamespace(iterations=iterations))
    assert llm.anthropic_completer(client=client)("SYS", "USER", 100) == "ok"
    assert "answered by fallback model claude-opus-4-8" in caplog.text


def test_no_fallback_log_when_the_fallback_model_declines_too(caplog):
    iterations = [SimpleNamespace(type="message"), SimpleNamespace(type="fallback_message")]
    messages = FakeMessages(
        [], stop_reason="refusal", stop_details=SimpleNamespace(category="cyber"),
        model="claude-opus-4-8", usage=SimpleNamespace(iterations=iterations),
    )
    client = SimpleNamespace(messages=messages, beta=SimpleNamespace(messages=messages))
    with pytest.raises(ValueError, match="cyber"):
        llm.anthropic_completer(client=client)("SYS", "USER", 100)
    assert "answered by fallback" not in caplog.text


# ------------------------------------------------------------------ settings


def test_backend_and_model_can_come_from_the_settings_file(tmp_path, monkeypatch):
    monkeypatch.delenv("JOBHUNT_BACKEND", raising=False)
    monkeypatch.delenv("JOBHUNT_MODEL", raising=False)
    (tmp_path / "settings.yaml").write_text("llm:\n  backend: claude-code\n  model: opus\n")
    monkeypatch.setattr(llm.settings.config, "DEFAULT_CONFIG_DIR", tmp_path)
    assert llm.backend_name() == "claude-code"
    assert llm.model_name() == "opus"


def test_the_long_env_names_work_for_backend_and_model(monkeypatch):
    monkeypatch.delenv("JOBHUNT_MODEL", raising=False)
    monkeypatch.setenv("JOBHUNT_LLM_BACKEND", "anthropic")
    monkeypatch.setenv("JOBHUNT_LLM_MODEL", "claude-opus-5-5")
    assert llm.backend_name() == "anthropic"
    assert llm.model_name() == "claude-opus-5-5"
    client = _fake_client()
    llm.anthropic_completer(client=client)("SYS", "USER", 10)
    assert client.messages.calls[0]["model"] == "claude-opus-5-5"


def test_claude_code_timeout_comes_from_settings(monkeypatch):
    monkeypatch.setenv("JOBHUNT_LLM_CLAUDE_CODE_TIMEOUT", "42")
    seen = {}

    def fake_run(argv, **kw):
        seen["timeout"] = kw["timeout"]
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({"result": "ok", "is_error": False}), stderr="")

    llm.claude_code_completer(runner=fake_run)("SYS", "USER", 10)
    assert seen["timeout"] == 42


def test_settings_passed_in_are_used_without_reading_the_file(tmp_path, monkeypatch):
    # One CLI run loads settings once; an edit (or a typo) in the file mid-run changes nothing.
    (tmp_path / "settings.yaml").write_text("llm:\n  model: [oops\n")
    monkeypatch.setattr(llm.settings.config, "DEFAULT_CONFIG_DIR", tmp_path)
    s = llm.settings.LLMSettings(backend="claude-code", model="opus", claude_code_timeout=42)
    assert (llm.backend_name(s), llm.model_name(s)) == ("claude-code", "opus")
    seen = {}

    def fake_run(argv, **kw):
        seen.update(model=argv[argv.index("--model") + 1], timeout=kw["timeout"])
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({"result": "ok", "is_error": False}), stderr="")

    llm.claude_code_completer(runner=fake_run, llm_settings=s)("SYS", "USER", 10)
    assert seen == {"model": "opus", "timeout": 42}
    for make in (llm.anthropic_completer, llm.bedrock_completer):
        client = _fake_client()
        make(client=client, llm_settings=s)("SYS", "USER", 10)
        assert client.messages.calls[0]["model"] == "opus"
    monkeypatch.setattr(llm, "claude_code_completer", lambda llm_settings: llm_settings)
    assert llm.make_completer(s) is s


def test_make_completer_passes_its_settings_to_every_backend(monkeypatch):
    for backend, factory in (("anthropic", "anthropic_completer"), ("bedrock", "bedrock_completer")):
        s = llm.settings.LLMSettings(backend=backend, model="x")
        monkeypatch.setattr(llm, factory, lambda llm_settings: llm_settings)
        assert llm.make_completer(s) is s
