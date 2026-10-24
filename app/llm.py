"""One function the agents call, `call_structured`, backed by the Claude Code subscription.

Every agent call runs `claude -p` (the Claude Code CLI) with no tools, a system prompt and a
JSON schema, authenticated by the logged-in Claude subscription. No API key is used: any
ANTHROPIC_API_KEY in the environment is stripped so calls can never switch to metered billing.

Every call returns the parsed JSON plus real token usage, so the app can show and log what
each agent spent.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field

from . import config


class LLMError(RuntimeError):
    pass


@dataclass
class Usage:
    input_tokens: int = 0          # uncached input
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    output_tokens: int = 0

    @property
    def billable(self) -> int:
        """Tokens counted against the per-run budget (cache reads are ~10x cheaper, so excluded)."""
        return self.input_tokens + self.cache_write_tokens + self.output_tokens

    def as_dict(self) -> dict:
        return {
            "input_tokens": self.input_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "output_tokens": self.output_tokens,
        }


@dataclass
class LLMResult:
    data: dict
    usage: Usage
    model: str
    provider: str
    latency_s: float
    cost_usd: float | None = None   # API-equivalent cost reported by the backend, if any
    extra: dict = field(default_factory=dict)


@dataclass
class Image:
    data: bytes
    media_type: str  # image/png or image/jpeg


# ------------------------------------------------------------------ status

_claude_error: str | None = None


def _claude_cli() -> str | None:
    return shutil.which("claude")


def provider() -> str:
    return "claude-subscription"


def status() -> dict:
    """Cheap readiness check (no tokens): is the CLI installed and logged in?"""
    if _claude_error:
        return {"provider": "claude", "ready": False, "message": _claude_error}
    cli = _claude_cli()
    if not cli:
        return {"provider": "claude", "ready": False, "message": "Claude Code is not installed. Install it, run `claude`, and log in with /login."}
    try:
        out = subprocess.run([cli, "auth", "status"], capture_output=True, text=True, timeout=15, env=_child_env()).stdout
        info = json.loads(out)
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
        info = {}
    if info and not info.get("loggedIn") or info.get("authMethod") == "none":
        return {"provider": "claude", "ready": False, "message": "Claude Code is not logged in. Run `claude` in a terminal and use /login."}
    plan = info.get("subscriptionType")
    return {"provider": "claude", "ready": True, "message": f"Claude subscription{f' ({plan})' if plan else ''} via claude -p"}


# ------------------------------------------------------------------ Claude subscription

# Only these variables reach the `claude` child process, so a stray ANTHROPIC_API_KEY can
# never switch the call to metered billing and no host session settings leak in.
_ENV_ALLOWLIST = ("HOME", "PATH", "USER", "LOGNAME", "SHELL", "TMPDIR", "LANG", "LC_ALL")


def _child_env() -> dict[str, str]:
    env = {k: os.environ[k] for k in _ENV_ALLOWLIST if k in os.environ}
    env.setdefault("LANG", "en_US.UTF-8")
    return env


async def _run(cmd: list[str], stdin: bytes, timeout: int) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=str(config.ROOT), env=_child_env(),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise LLMError(f"The model did not answer within {timeout} seconds.")
    except asyncio.CancelledError:
        proc.kill()   # a cancelled speculative step must not keep spending tokens
        raise
    return proc.returncode or 0, out, err


async def _claude(agent: str, system: str, user: str, schema: dict, images: list[Image] | None) -> LLMResult:
    global _claude_error
    cli = _claude_cli()
    if not cli:
        raise LLMError("The `claude` CLI is not installed.")
    model = config.CLAUDE_MODELS.get(agent, "sonnet")
    cmd = [
        cli, "-p",
        "--model", model,
        "--system-prompt", system,
        "--tools", "",                 # the model can only answer; it has no tools
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--no-session-persistence",
        "--json-schema", json.dumps(schema),
    ]
    if images:
        # Photos go in as image content blocks over stream-json.
        content = [
            {"type": "image", "source": {"type": "base64", "media_type": im.media_type, "data": base64.b64encode(im.data).decode()}}
            for im in images
        ] + [{"type": "text", "text": user}]
        cmd += ["--input-format", "stream-json", "--output-format", "stream-json", "--verbose"]
        stdin = (json.dumps({"type": "user", "message": {"role": "user", "content": content}}) + "\n").encode()
    else:
        cmd += ["--output-format", "json"]
        stdin = user.encode()

    started = time.monotonic()
    last_err = "unknown error"
    for _attempt in range(2):
        code, out, err = await _run(cmd, stdin, config.LLM_TIMEOUT_S)
        envelope = _parse_claude_output(out, streamed=bool(images))
        if envelope is None:
            last_err = (out + err).decode(errors="replace")[:400] or f"exit code {code}"
            continue
        if envelope.get("is_error") or envelope.get("structured_output") is None:
            last_err = str(envelope.get("result") or envelope.get("errors") or "no structured output")[:400]
            if "disabled" in last_err.lower() or "login" in last_err.lower() or "auth" in last_err.lower():
                _claude_error = f"Claude subscription unavailable: {last_err}"
                raise LLMError(_claude_error)
            continue
        _claude_error = None
        u = envelope.get("usage") or {}
        models_used = list((envelope.get("modelUsage") or {}).keys())
        return LLMResult(
            data=envelope["structured_output"],
            usage=Usage(
                input_tokens=int(u.get("input_tokens", 0)),
                cache_write_tokens=int(u.get("cache_creation_input_tokens", 0)),
                cache_read_tokens=int(u.get("cache_read_input_tokens", 0)),
                output_tokens=int(u.get("output_tokens", 0)),
            ),
            model=models_used[0] if len(models_used) == 1 else (",".join(models_used) or model),
            provider="claude-subscription",
            latency_s=round(time.monotonic() - started, 2),
            cost_usd=envelope.get("total_cost_usd"),
            extra={"models_used": models_used, "requested_model": model},
        )
    raise LLMError(f"Claude call failed: {last_err}")


def _parse_claude_output(out: bytes, streamed: bool) -> dict | None:
    text = out.decode(errors="replace").strip()
    if not text:
        return None
    if not streamed:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None
    for line in reversed(text.splitlines()):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "result":
            return event
    return None


# ------------------------------------------------------------------ entry point

async def call_structured(agent: str, system: str, user: str, schema: dict, images: list[Image] | None = None) -> LLMResult:
    return await _claude(agent, system, user, schema, images)
