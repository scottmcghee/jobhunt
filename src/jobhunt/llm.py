"""The one place that talks to a model.

Two backends, same ``Completer`` signature:

- ``anthropic``   — the Anthropic SDK with an API key (Claude Console, pay-as-you-go).
- ``claude-code`` — shells out to the Claude Code CLI (``claude -p``), which runs on the
                    user's own Claude subscription login. No API key needed.

Selection: ``JOBHUNT_BACKEND`` if set; otherwise ``anthropic`` when ``ANTHROPIC_API_KEY``
is present, else ``claude-code`` when a ``claude`` binary is on PATH.

Keeping this thin and injectable means every other module can be tested with a
fake ``complete`` function and never touches the network or a subprocess.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from typing import Any

DEFAULT_MODEL = "claude-sonnet-4-5"
DEFAULT_CLI_MODEL = "sonnet"  # Claude Code accepts aliases; cheaper than the default opus

# Signature every caller depends on: (system, user, max_tokens) -> assistant text
Completer = Callable[[str, str, int], str]


def model_name() -> str:
    return os.environ.get("JOBHUNT_MODEL", DEFAULT_MODEL)


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
    raise ValueError(f"unknown JOBHUNT_BACKEND {name!r}; use 'anthropic' or 'claude-code'")


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
