# synapse agents

A **delegation hierarchy** exposed over MCP: any model tier can spin up
subagents on strictly-cheaper tiers, so the frontier model spends its context on
what's hard and hands everything else down.

## The ladder

```
frontier  ─▶ mid ─▶ bulk ─▶ low ─▶ local
(paid)      (cheap) (free)  (free)  (offline)
```

Each caller is auto-handed `to_<tier>` tools for **only the tiers below it**
(gateway/mcp_autoinject.py enforces this): `frontier` can delegate to
mid/bulk/low/local, `mid` to bulk/low/local, … `local` to nobody. So recursion
terminates on its own — a chain is at most four hops deep.

## How a subagent runs

`to_bulk(task=…)` calls **this same gateway** (`/v1/chat/completions`) with
`model="bulk"` and no explicit tools. The gateway's auto-inject hook then hands
that subagent its *own* curated toolbox (memory + web everywhere; read-only
files/git/github on capable tiers; delegation to still-lower tiers), and LiteLLM
auto-executes one tool round. The subagent acts, then returns just its result.

- **`task`** — one self-contained instruction (the subagent sees none of the
  parent conversation, so spell it out).
- **`context`** — optional supporting data (code, notes, prior results).
- **`tasks=[…]`** — a LIST fans out that many independent jobs **concurrently**
  on the tier (up to `SYNAPSE_SUBAGENT_MAX_FANOUT`); results come back numbered.
  This is the "bulk agent fanout" pattern — cheap parallel workers.

## Cost & safety

Delegation only ever flows **downward in cost**, and the paid tiers are already
capped by the gateway's `max_budget`. bulk/low/local are free, so massive fanout
there is $0. Per-subagent time is bounded by `SYNAPSE_SUBAGENT_TIMEOUT`.

## Config

Reads from `env/.env` (or real env vars): `LITELLM_MASTER_KEY` (to call back),
`SYNAPSE_PORT` (default 4000), `SYNAPSE_GATEWAY_URL` (full base-url override),
`SYNAPSE_SUBAGENT_TIMEOUT` (default 300s), `SYNAPSE_SUBAGENT_MAX_FANOUT` (8).

CLI smoke test (talks to the running gateway):

```sh
./.venv/bin/python agents/server.py bulk "Summarize what tier you are in one line."
```
