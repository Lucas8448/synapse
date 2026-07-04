#!/usr/bin/env python3
"""synapse agents — a delegation hierarchy exposed as MCP tools.

Turns the gateway's model tiers into a chain of command: any tier can spin up
subagents on STRICTLY-LOWER tiers to offload work (fanout, summarization,
lookups, offline/private tasks) while it keeps the expensive context for itself.

Each `to_<tier>` tool runs a real subagent: it calls the gateway
(/v1/chat/completions) with that tier's model and NO explicit tools, so the
gateway's own auto-inject hook hands the subagent the right curated toolbox
(memory + web everywhere; read-only files/git/github on capable tiers; plus the
delegation tools for tiers below it). LiteLLM auto-executes one tool round and
folds the results into the reply, so the subagent can actually act — and, being
handed lower-tier `to_*` tools itself, can sub-delegate further. Recursion is
bounded by the tier ladder (frontier > mid > bulk > low > local): `local`, at
the bottom, gets no delegation tools, so a chain can be at most four hops.

gateway/mcp_autoinject.py decides WHICH `to_*` tools each caller sees (only
strictly-lower tiers). This server just executes a delegated task on a tier.

Run as an MCP server (no args) or from the CLI for a one-shot test:
    ./.venv/bin/python agents/server.py <tier> "task text"
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

try:  # MCP is optional — the CLI works without it
    from fastmcp import FastMCP
except ImportError:  # pragma: no cover
    FastMCP = None


class _CliOnly:
    def tool(self, f):
        return f

    def run(self, *a, **k):
        raise SystemExit("fastmcp not installed — CLI mode only (see --help)")


REPO = Path(__file__).resolve().parent.parent


def _load_env() -> None:
    """Load ../env/.env so the CLI needs no shell exports. Real env vars win."""
    env_file = REPO / "env" / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.split("#")[0].strip().strip("'\"")
        os.environ.setdefault(key, val)


_load_env()

# ── Config ───────────────────────────────────────────────────────────────────
# One line per tier: name -> what it's for. Order = capability/cost, high → low.
# Keep in sync with gateway/litellm.yaml's model_list comments.
TIERS: dict[str, str] = {
    "frontier": "hard reasoning, architecture, gnarly debugging (paid)",
    "mid": "everyday coding, refactors, tests (cheap)",
    "bulk": "parallel fanout, classification, summarization (free)",
    "low": "trivial tasks — commit messages, renames, quick lookups (free)",
    "local": "offline, zero-cost, fully private — no network leaves the machine",
}

_PORT = os.environ.get("SYNAPSE_PORT", "4000").strip() or "4000"
GATEWAY_URL = (
    os.environ.get("SYNAPSE_GATEWAY_URL", "").strip()
    or f"http://127.0.0.1:{_PORT}"
).rstrip("/")
MASTER_KEY = os.environ.get("LITELLM_MASTER_KEY", "").strip()
TIMEOUT = float(os.environ.get("SYNAPSE_SUBAGENT_TIMEOUT", "300") or "300")
MAX_FANOUT = int(os.environ.get("SYNAPSE_SUBAGENT_MAX_FANOUT", "8") or "8")

mcp = FastMCP("synapse-agents") if FastMCP else _CliOnly()


def _system_prompt(tier: str) -> str:
    return (
        f"You are the '{tier}' subagent ({TIERS.get(tier, 'worker')}) inside "
        "Lucas's Synapse gateway, invoked by a higher-tier agent to handle one "
        "delegated task. A curated toolbox is auto-attached to you (personal "
        "memory, web fetch/search, and — depending on your tier — read-only "
        "files/git/github and the ability to delegate to still-lower tiers). "
        "Use the tools as needed to actually complete the task; if you "
        "sub-delegate, synthesize the sub-results. Return ONLY the final result: "
        "concise, directly usable, no preamble and no restating the task."
    )


def _post_chat(tier: str, task: str, context: str) -> str:
    """Run one subagent turn on `tier` via the gateway; return its final text."""
    if not MASTER_KEY:
        return "error: LITELLM_MASTER_KEY not set — cannot reach the gateway"
    user = task.strip()
    if context.strip():
        user += f"\n\n--- context ---\n{context.strip()}"
    body = json.dumps(
        {
            "model": tier,
            "messages": [
                {"role": "system", "content": _system_prompt(tier)},
                {"role": "user", "content": user},
            ],
        }
    ).encode()
    req = urllib.request.Request(
        f"{GATEWAY_URL}/v1/chat/completions",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {MASTER_KEY}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            payload = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:400]
        return f"error: {tier} subagent HTTP {e.code} — {detail}"
    except Exception as e:  # noqa: BLE001 — MCP tools must return, not raise
        return f"error: {tier} subagent call failed — {e}"
    try:
        msg = payload["choices"][0]["message"]
        text = (msg.get("content") or "").strip()
        return text or "(subagent produced no text output)"
    except (KeyError, IndexError, TypeError):
        return f"error: unexpected gateway response — {json.dumps(payload)[:400]}"


def _delegate(tier: str, task: str, context: str, tasks: list[str] | None) -> str:
    """Single task, or concurrent fanout when `tasks` is given."""
    jobs = [t for t in (tasks or []) if t and t.strip()]
    if not jobs:
        if not task.strip():
            return "error: provide `task` (a self-contained instruction) or `tasks`"
        return _post_chat(tier, task, context)

    jobs = jobs[:MAX_FANOUT]
    results: list[str] = [""] * len(jobs)
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futs = {pool.submit(_post_chat, tier, j, context): i for i, j in enumerate(jobs)}
        for fut in concurrent.futures.as_completed(futs):
            results[futs[fut]] = fut.result()
    return "\n\n".join(f"### subagent {i + 1}\n{r}" for i, r in enumerate(results))


def _make_tool(tier: str):
    desc = TIERS[tier]

    def tool(task: str = "", context: str = "", tasks: list[str] | None = None) -> str:
        return _delegate(tier, task, context, tasks)

    tool.__name__ = f"to_{tier}"
    tool.__doc__ = (
        f"Delegate a subtask to the '{tier}' tier ({desc}).\n"
        "`task`: one self-contained instruction — the subagent has no view of "
        "this conversation, so include everything it needs. `context`: optional "
        "supporting data (code, notes, prior results). `tasks`: pass a LIST to "
        f"fan out that many INDEPENDENT jobs concurrently on {tier} (up to "
        f"{MAX_FANOUT}); results come back numbered. The subagent runs with its "
        "own auto-attached tools and returns just the result."
    )
    return tool


# Register one delegation tool per non-top tier (nothing delegates UP to frontier).
for _t in [t for t in TIERS if t != "frontier"]:
    mcp.tool(_make_tool(_t))


def _cli() -> None:
    args = sys.argv[1:]
    if not args or args[0] in {"-h", "--help"}:
        print(__doc__)
        print("\ntiers:")
        for name, desc in TIERS.items():
            print(f"  {name:9} {desc}")
        return
    tier = args[0]
    if tier not in TIERS:
        raise SystemExit(f"unknown tier '{tier}' — one of: {', '.join(TIERS)}")
    task = " ".join(args[1:]) or "Say hello and name which tier you are."
    print(_post_chat(tier, task, ""))


if __name__ == "__main__":
    if len(sys.argv) > 1:
        _cli()
    else:
        try:
            mcp.run(show_banner=False)
        except TypeError:  # older fastmcp without the kwarg
            mcp.run()
