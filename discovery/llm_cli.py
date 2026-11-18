#!/usr/bin/env python3
"""
Shared LLM caller for the pipeline's two LLM stages.

Default backend: Claude Code CLI in headless mode (`claude -p`), model Haiku.
It runs on the Claude login of whoever runs the script, so no API key is needed.
The Gemini CLI is still available with --backend gemini.

Every raw reply is saved to <raw_dir>/<stage>/<key>.json with a hash of its
prompt. On a re-run the saved reply is reused only if the prompt is unchanged,
so editing the criteria or the prompt re-asks the model.

cost_usd is what the Claude CLI reports as the API-price equivalent. On a
claude.ai subscription login (no ANTHROPIC_API_KEY) nothing is billed per call.
"""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SYSTEM_PROMPT = (
    "You are a careful scientific curator working on microbiome literature. "
    "Answer only from the text you are given. Reply with a single JSON object "
    "and nothing else: no markdown fences, no commentary."
)

# Empty working folder, so the CLI does not pick up any project instructions.
_EMPTY_DIR = tempfile.mkdtemp(prefix="llm_cli_")


def parse_json_reply(text):
    """Return the JSON object in a model reply, tolerating ``` fences and stray prose."""
    if not text:
        raise ValueError("empty reply")
    s = text.strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1]
        if s.lstrip().lower().startswith("json"):
            s = s.lstrip()[4:]
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        start, end = s.find("{"), s.rfind("}")
        if start != -1 and end > start:
            return json.loads(s[start:end + 1])
        raise


def _run_claude(prompt, model, timeout):
    cmd = [
        "claude", "-p",
        "--model", model,
        "--tools", "",
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--no-session-persistence",
        "--output-format", "json",
        "--system-prompt", SYSTEM_PROMPT,
    ]
    t0 = time.time()
    proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                          timeout=timeout, cwd=_EMPTY_DIR)
    seconds = round(time.time() - t0, 1)
    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"claude CLI gave no JSON (exit {proc.returncode}): "
                           f"{(proc.stderr or proc.stdout)[:300]}")
    if envelope.get("is_error"):
        raise RuntimeError(f"claude CLI error: {str(envelope.get('result'))[:300]}")
    usage = envelope.get("usage", {})
    return {
        "text": envelope.get("result", ""),
        "model": model,
        "models_used": list((envelope.get("modelUsage") or {}).keys()),
        "cost_usd": envelope.get("total_cost_usd", 0),
        "input_tokens": (usage.get("input_tokens", 0)
                         + usage.get("cache_read_input_tokens", 0)
                         + usage.get("cache_creation_input_tokens", 0)),
        "output_tokens": usage.get("output_tokens", 0),
        "seconds": seconds,
    }


def _run_gemini(prompt, model, timeout):
    cmd = ["gemini", "-p", prompt]
    if model:
        cmd += ["-m", model]
    t0 = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=True)
    return {"text": proc.stdout.strip(), "model": model or "gemini-default",
            "cost_usd": None, "seconds": round(time.time() - t0, 1)}


def call_llm(prompt, stage, key, model="haiku", backend="claude", timeout=180,
             raw_dir="llm_raw", retries=1):
    """
    Send one prompt and return (data, meta).

    data: the parsed JSON object, or None on failure.
    meta: dict with text, model, cost_usd, tokens, seconds, cached, error.
    """
    raw_path = Path(raw_dir) / stage / f"{key}.json"
    prompt_sha = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]
    if raw_path.exists():
        meta = json.loads(raw_path.read_text(encoding="utf-8"))
        # Replies saved before prompt hashing have no hash and are kept as they are.
        if meta.get("prompt_sha256", prompt_sha) != prompt_sha:
            meta = None  # prompt or criteria changed since this reply; ask again
    if raw_path.exists() and meta:
        try:
            return parse_json_reply(meta.get("text")), {**meta, "cached": True}
        except (ValueError, json.JSONDecodeError):
            pass  # stored reply was bad; ask again

    runner = _run_claude if backend == "claude" else _run_gemini
    last_error = None
    for attempt in range(retries + 1):
        try:
            meta = runner(prompt, model, timeout)
            meta.update(stage=stage, key=key, attempt=attempt + 1, prompt_sha256=prompt_sha,
                        saved_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
            return parse_json_reply(meta["text"]), {**meta, "cached": False}
        except FileNotFoundError:
            print(f"ERROR: '{backend}' CLI not found on PATH", file=sys.stderr)
            sys.exit(1)
        except subprocess.TimeoutExpired:
            last_error = f"timeout after {timeout}s"
        except (ValueError, json.JSONDecodeError) as e:
            last_error = f"unparseable reply: {e}"
        except (RuntimeError, subprocess.CalledProcessError) as e:
            last_error = str(e)
            if "authenticate" in last_error.lower() or "login" in last_error.lower():
                print(f"ERROR: {last_error}\nRun `claude auth login` first.", file=sys.stderr)
                sys.exit(2)
        print(f"  WARNING: LLM call {stage}/{key} attempt {attempt + 1} failed: {last_error}",
              file=sys.stderr)
        time.sleep(2)
    return None, {"error": last_error, "cached": False, "cost_usd": 0}


if __name__ == "__main__":
    data, meta = call_llm('Reply with this JSON object: {"ok": true}', "smoke", "test",
                          raw_dir=os.environ.get("LLM_RAW_DIR", "llm_raw"))
    print(json.dumps({"data": data, "meta": {k: v for k, v in meta.items() if k != "text"}}, indent=1))
