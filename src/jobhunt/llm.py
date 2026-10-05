"""The one place that talks to a model.

Three backends, same ``Completer`` signature:

- ``anthropic``   — the Anthropic SDK with an API key (Claude Console, pay-as-you-go).
- ``claude-code`` — shells out to the Claude Code CLI (``claude -p``), which runs on the
                    user's own Claude subscription login. No API key needed.
- ``bedrock``     — the Anthropic SDK's Amazon Bedrock client, on the user's AWS account.
                    Credentials and region come from the standard AWS environment.

Selection: ``JOBHUNT_BACKEND`` if set; otherwise ``anthropic`` when ``ANTHROPIC_API_KEY``
is present, else ``claude-code`` when a ``claude`` binary is on PATH. ``bedrock`` is only
ever chosen explicitly: AWS credentials are often present for unrelated reasons.

Keeping this thin and injectable means every other module can be tested with a
fake ``complete`` function and never touches the network or a subprocess.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from typing import Any

DEFAULT_MODEL = "claude-sonnet-4-5"
DEFAULT_CLI_MODEL = "sonnet"  # Claude Code accepts aliases; cheaper than the default opus
DEFAULT_BEDROCK_MODEL = "anthropic.claude-sonnet-5-5"  # Bedrock IDs carry an "anthropic." prefix
_DEFAULT_MODELS = {
    "anthropic": DEFAULT_MODEL,
    "claude-code": DEFAULT_CLI_MODEL,
    "bedrock": DEFAULT_BEDROCK_MODEL,
}

# Signature every caller depends on: (system, user, max_tokens) -> assistant text
Completer = Callable[[str, str, int], str]


def model_name() -> str:
    """The model the current backend will call; recorded with every score and letter."""
    default = _DEFAULT_MODELS.get(backend_name(), DEFAULT_MODEL)
    return os.environ.get("JOBHUNT_MODEL", default)


def backend_name() -> str:
    explicit = os.environ.get("JOBHUNT_BACKEND")
    if explicit:
        return explicit
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if shutil.which("claude"):
        return "claude-code"
    return "anthropic"  # will fail with a clear SDK error about the missing key


def make_completer() -> Completer:
    name = backend_name()
    if name == "anthropic":
        return anthropic_completer()
    if name == "claude-code":
        return claude_code_completer()
    if name == "bedrock":
        return bedrock_completer()
    raise ValueError(
        f"unknown JOBHUNT_BACKEND {name!r}; use 'anthropic', 'claude-code', or 'bedrock'"
    )


# --------------------------------------------------------------------------- anthropic


def anthropic_completer(model: str | None = None) -> Completer:
    """Build a completer backed by the Anthropic SDK. Imported lazily."""
    import anthropic  # local import so tests never need the key

    client = anthropic.Anthropic()
    use_model = model or model_name()

    def complete(system: str, user: str, max_tokens: int = 1500) -> str:
        msg = client.messages.create(
            model=use_model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(getattr(block, "text", "") for block in msg.content)

    return complete


# --------------------------------------------------------------------------- bedrock


def _bedrock_thinking(model: str) -> dict[str, str] | None:
    """Thinking off for the default model, so the callers' small ``max_tokens`` go to the answer.

    ``between_tools`` is how Claude Sonnet 5.5 turns thinking off, and only it accepts the value.
    Any other model keeps its own default; one that thinks needs a larger ``max_tokens``.
    """
    return {"type": "between_tools"} if model.endswith("claude-sonnet-5-5") else None


_BEDROCK_API_KEY_VARS = ("AWS_BEARER_TOKEN_BEDROCK", "ANTHROPIC_AWS_API_KEY")


def _check_bedrock_auth() -> None:
    """Fail early and plainly when the SDK would only fail on the first request."""
    if any(os.environ.get(v) for v in _BEDROCK_API_KEY_VARS):
        return
    if importlib.util.find_spec("botocore") is None:
        raise RuntimeError(
            "JOBHUNT_BACKEND=bedrock without a Bedrock API key (AWS_BEARER_TOKEN_BEDROCK) "
            'signs requests with your AWS credentials, which needs: pip install -e ".[bedrock]"'
        )


def bedrock_completer(model: str | None = None, client: Any = None) -> Completer:
    """Build a completer on Claude in Amazon Bedrock (the Messages-API endpoint).

    The SDK reads ``AWS_REGION`` and either ``AWS_BEARER_TOKEN_BEDROCK`` (a Bedrock API key) or the
    standard AWS credential chain, which needs ``pip install -e ".[bedrock]"``. ``client`` is
    injectable so tests never reach AWS.
    """
    if client is None:
        import anthropic  # local import, as above

        _check_bedrock_auth()
        client = anthropic.AnthropicBedrockMantle()
    use_model = model or os.environ.get("JOBHUNT_MODEL", DEFAULT_BEDROCK_MODEL)
    thinking = _bedrock_thinking(use_model)

    def complete(system: str, user: str, max_tokens: int = 1500) -> str:
        extra = {"thinking": thinking} if thinking else {}
        msg = client.messages.create(
            model=use_model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            **extra,
        )
        return "".join(block.text for block in msg.content if block.type == "text")

    return complete


# --------------------------------------------------------------------------- claude code


def claude_code_command(system: str, model: str) -> list[str]:
    """The argv for one stateless, tool-less ``claude -p`` call. Separated for testing."""
    return [
        "claude",
        "-p",
        "--output-format", "json",
        "--system-prompt", system,
        "--tools", "",  # a scorer needs no file or shell access
        "--max-turns", "1",
        "--no-session-persistence",
        "--model", model,
    ]


def claude_code_completer(
    model: str | None = None,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> Completer:
    """Build a completer that shells out to the Claude Code CLI.

    The user prompt goes in on stdin; the response comes back as JSON on stdout with the
    assistant text in ``result``. ``runner`` is injectable so tests never spawn a process.
    """
    use_model = model or os.environ.get("JOBHUNT_MODEL", DEFAULT_CLI_MODEL)

    def complete(system: str, user: str, max_tokens: int = 1500) -> str:
        proc = runner(
            claude_code_command(system, use_model),
            input=user,
            capture_output=True,
            text=True,
            timeout=180,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"claude -p exited {proc.returncode}: {proc.stderr.strip() or proc.stdout.strip()}"
            )
        payload = json.loads(proc.stdout)
        if payload.get("is_error"):
            raise RuntimeError(f"claude -p reported an error: {payload.get('result')}")
        return str(payload.get("result", ""))

    return complete


# --------------------------------------------------------------------------- parsing


_JSON_BLOCK = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)


def extract_json(text: str) -> dict[str, Any]:
    """Pull a JSON object out of a model response that may be fenced or have prose around it."""
    m = _JSON_BLOCK.search(text)
    candidate = m.group(1) if m else text
    # Take the first object that decodes; models sometimes repeat themselves or add a note after.
    decoder = json.JSONDecoder()
    start = candidate.find("{")
    while start != -1:
        try:
            return decoder.raw_decode(candidate, start)[0]
        except json.JSONDecodeError:
            start = candidate.find("{", start + 1)
    raise ValueError("no JSON object in response")
