# synapse memory

Obsidian-vault-native knowledge graph with vector recall, exposed over MCP (or CLI).

## Design

Your vault **is** the memory. Notes = entities, `[[wikilinks]]` = graph edges,
`pred:: [[target]]` (Dataview syntax) = typed relations, `#tags` + frontmatter
tags respected. No parallel store, no big JSON file — every fact is a markdown
bullet you can see and edit in Obsidian, including in graph view.

- **recall** indexes the whole vault (read-only over your notes)
- **remember / forget** write ONLY inside `agent-memory/` — agents never touch your own notes
- **Cache** (Kuzu graph + embeddings) lives in `~/.cache/synapse/`, rebuilt automatically when notes change. Never synced — only markdown crosses machines (via Proton Drive).

## Embeddings

Local Ollama `nomic-embed-text` with task prefixes (`search_document` /
`search_query` — required by nomic for good retrieval) and Matryoshka truncation
to **256 dims** (~97% quality, 1/3 the cache). Falls back to keyword search if
Ollama is down. Env: `SYNAPSE_EMB_DIM`, `SYNAPSE_EMB_MODEL`, `OLLAMA_API_BASE`.

## Setup

```sh
pip install -r memory/requirements.txt
ollama pull nomic-embed-text
export SYNAPSE_VAULT="$HOME/path/to/your/obsidian-vault"   # default: memory/store/
claude mcp add synapse-memory --env SYNAPSE_VAULT="$SYNAPSE_VAULT" -- python3 <path-to-synapse>/memory/server.py
```

CLI works the same everywhere: `python3 server.py recall "..." | remember "..." | neighbors lucas | stats`.

## Conventions

- One bullet per fact — small facts compose better than paragraphs.
- Wikilink entities you want in the graph: `- Prefers [[typescript]] #preference`
- Typed relations as their own bullet: `- studies_at:: [[ntnu]]`
- `agent-memory/<topic>.md` per topic (`lucas`, `projects`, `inbox` for uncategorized).

## Privacy

`memory/store/` is gitignored (repo is public). It syncs via Proton Drive because
the folder lives there — never via git. Point `SYNAPSE_VAULT` at your real vault
and the same applies as long as the vault is outside any public repo.
