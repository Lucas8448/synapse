# synapse

Provider-agnostic AI gateway config — one spine for all models, tools, and agents.

One local gateway ([LiteLLM](https://docs.litellm.ai/docs/proxy/quick_start)) + this repo as the single source of truth. Every tool (pi, Claude Code, OpenCode, scripts) talks to `localhost:4000` using stable tier aliases — swap providers behind them without touching any tool config.

## Tiers

| Alias      | Purpose                                   | Backed by (current)                        |
|------------|-------------------------------------------|--------------------------------------------|
| `frontier` | Hard reasoning, architecture — paid       | OpenRouter · z-ai/glm-5.2                  |
| `mid`      | Everyday coding, refactors                | OpenRouter · deepseek/deepseek-v4-flash    |
| `bulk`     | Subagent fanout, classification, mining   | OpenRouter · nemotron-3-super-120b (:free) |
| `low`      | Commit msgs, renames, trivia              | OpenRouter · cohere/north-mini-code (:free)|
| `local`    | Offline fallback                          | Ollama · qwen2.5-coder                     |

Fallbacks: `frontier → mid → bulk → low → local`. Hard budget cap: $20/mo.

## Tools (auto-attached)

Every chat/responses call gets a curated MCP toolbox injected by the gateway —
apps need **zero** MCP setup. A pre-call hook (`gateway/mcp_autoinject.py`)
attaches tools by tier:

- **Everywhere** — `synapse` personal memory (Obsidian knowledge graph) + web
  `fetch` / `websearch`.
- **Capable tiers** (`frontier`/`mid`/`local`) — read-only `files`, `git`, and
  `github` (47 tools). Curated to read-only so a blanket attach can't mutate.
- **Delegation** — each tier is handed `agents_to_<tier>` tools for the tiers
  **below** it, so any model can spin up cheaper **subagents** (with their own
  toolbox) and fan out work. See [`agents/`](agents/README.md).

Each server is also reachable directly at `http://localhost:4000/<name>/mcp`.

## Setup

```sh
git clone <this repo> <wherever you keep repos>
cp env/.env.example ~/.config/synapse/.env   # fill in real keys — NEVER commit them
pip install 'litellm[proxy]'
bin/spine-sync                               # symlinks adapters, validates env, starts gateway
```

Then point any OpenAI-compatible tool at:

```
base_url = http://localhost:4000
model    = frontier | mid | bulk | local
```

## Deploy (VPS)

```sh
cd /opt/synapse/gateway
docker network create cortex || true   # once per host
chmod +x up.sh
./up.sh
curl localhost:4000/health/liveliness
```

## Layout

```
gateway/    litellm.yaml (tiers, fallbacks, budget), mcp_autoinject.py (tool hub), docker-compose.yml
memory/     synapse memory MCP server — Obsidian knowledge graph + vector recall
agents/     delegation MCP server — subagents on lower tiers (fanout, offload)
env/        .env.example — key NAMES only; real keys live in ~/.config/synapse/.env
prompts/    shared system prompts, tool-agnostic; adapters reference these
knowledge/  codex + graphify outputs per repo (symlinked in, gitignored contents)
adapters/   per-tool config, symlinked into place by spine-sync
bin/        spine-sync
```

## Notes

- Claude Pro subscription has no API key — Claude Code authenticates natively on the sub; everything else routes through the gateway.
- Swapping a provider = one line in `gateway/litellm.yaml`. Tool configs never change.
- Response cache is on: repeated identical calls are free.
