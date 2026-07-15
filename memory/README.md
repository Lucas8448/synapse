# synapse memory

Obsidian-vault-native knowledge graph with vector recall, exposed over MCP (or CLI).

## Design

Your notes directory **is** the memory. Notes = entities, `[[wikilinks]]` = graph edges,
`pred:: [[target]]` (Dataview syntax) = typed relations, `#tags` + frontmatter
tags respected. No parallel store, no big JSON file — every fact is a markdown
bullet you can see and edit in Obsidian, including in graph view.

- **recall** indexes the whole vault (read-only over your notes)
- **remember / forget** write ONLY inside `agent-memory/` — agents never touch your own notes
- **Cache**: embeddings live in `~/.cache/synapse/`; the graph is an in-memory index rebuilt from markdown whenever notes change (no database, no native deps). Never sync the cache — sync markdown only.

## Embeddings

Local Ollama `nomic-embed-text` with task prefixes (`search_document` /
`search_query` — required by nomic for good retrieval) and Matryoshka truncation
to **256 dims** (~97% quality, 1/3 the cache). Falls back to keyword search if
Ollama is down. Env: `SYNAPSE_EMB_DIM`, `SYNAPSE_EMB_MODEL`, `OLLAMA_API_BASE`.

## Setup

All config lives in `env/.env` (gitignored) — `SYNAPSE_VAULT` there points at the vault;
the server loads it itself, no shell exports needed. Real env vars override if set.

```sh
pip install -r memory/requirements.txt
ollama pull nomic-embed-text
claude mcp add synapse-memory -- python3 <path-to-synapse>/memory/server.py
```

CLI works the same everywhere: `python3 server.py recall "..." | remember "..." | neighbors <topic> | stats`.

## Conventions

- One bullet per fact — small facts compose better than paragraphs.
- Wikilink entities you want in the graph: `- Prefers [[typescript]] #preference`
- Typed relations as their own bullet: `- studies_at:: [[ntnu]]`
- `agent-memory/<topic>.md` per topic (`projects`, `inbox` for uncategorized).

## Privacy

`memory/store/` is gitignored. Point `SYNAPSE_VAULT` at a notes directory outside this repo; sync that directory however you normally sync private notes, never through git.
