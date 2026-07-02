#!/usr/bin/env python3
"""synapse memory — Obsidian-vault-native knowledge graph with vector recall.

Source of truth: markdown notes in an Obsidian vault (SYNAPSE_VAULT).
  - recall indexes the WHOLE vault (notes = entities, [[wikilinks]] = edges,
    `pred:: [[target]]` = typed relations, #tags and frontmatter tags respected)
  - remember/forget write ONLY inside SYNAPSE_AGENT_DIR (default: agent-memory/)
    so agents never touch your own notes.

Cache: Kuzu graph + embedding matrix in ~/.cache/synapse (never synced),
rebuilt automatically whenever any note changes.

Embeddings: Ollama nomic-embed-text with task prefixes (search_document /
search_query) and Matryoshka truncation to SYNAPSE_EMB_DIM (default 256).
Falls back to keyword search if Ollama is unreachable.

Run as MCP server (no args) or CLI: remember | recall | forget | neighbors | stats.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
import urllib.request
from pathlib import Path

import kuzu
import numpy as np

try:  # MCP is optional — the CLI works without it
    from fastmcp import FastMCP
except ImportError:
    FastMCP = None


class _CliOnly:
    def tool(self, f):
        return f

    def run(self):
        raise SystemExit("fastmcp not installed — CLI mode only (see --help)")


REPO = Path(__file__).resolve().parent
VAULT = Path(os.environ.get("SYNAPSE_VAULT", REPO / "store")).expanduser()
AGENT_DIR = os.environ.get("SYNAPSE_AGENT_DIR", "agent-memory")
CACHE = Path(os.environ.get("SYNAPSE_CACHE", Path.home() / ".cache" / "synapse")).expanduser()
DB_PATH = CACHE / "memory.kuzu"
EMB_FILE = CACHE / "embeddings.npz"

OLLAMA = os.environ.get("OLLAMA_API_BASE", "http://localhost:11434")
EMB_MODEL = os.environ.get("SYNAPSE_EMB_MODEL", "nomic-embed-text")
EMB_DIM = int(os.environ.get("SYNAPSE_EMB_DIM", "256"))  # Matryoshka truncation

SKIP_DIRS = {".obsidian", ".trash", ".git", "templates", "Templates"}

mcp = FastMCP("synapse-memory") if FastMCP else _CliOnly()

WIKILINK = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")
PREDICATE = re.compile(r"^\s*[-*]?\s*([\w-]+)::\s*(.+)$")
HASHTAG = re.compile(r"(?:^|\s)#([\w/-]+)")


# ── vault parsing ────────────────────────────────────────────────────────
def _notes() -> list[Path]:
    if not VAULT.exists():
        return []
    out = []
    for p in VAULT.rglob("*.md"):
        if not any(
            part in SKIP_DIRS or part.startswith(".") or "template" in part.lower()
            for part in p.relative_to(VAULT).parts[:-1]
        ):
            out.append(p)
    return sorted(out)


def _frontmatter_tags(text: str) -> tuple[list[str], str]:
    """Return (tags, body). Naive YAML frontmatter parse — good enough for tags."""
    if not text.startswith("---"):
        return [], text
    end = text.find("\n---", 3)
    if end == -1:
        return [], text
    head, body = text[3:end], text[end + 4 :]
    tags: list[str] = []
    m = re.search(r"^tags:\s*\[([^\]]*)\]", head, re.M)
    if m:
        tags = [t.strip().strip("'\"") for t in m.group(1).split(",") if t.strip()]
    else:
        m = re.search(r"^tags:\s*\n((?:\s*-\s*.+\n?)+)", head, re.M)
        if m:
            tags = [re.sub(r"^\s*-\s*", "", l).strip() for l in m.group(1).splitlines() if l.strip()]
    return tags, body


def _parse_note(path: Path) -> dict:
    """→ {entity, tags, blocks: [{id, text, links, tags}], relations: [(pred, target)]}"""
    entity = path.stem.lower()
    note_tags, body = _frontmatter_tags(path.read_text(errors="replace"))
    body = re.sub(r"```.*?(```|\Z)", "", body, flags=re.S)  # code fences aren't facts
    rel = str(path.relative_to(VAULT))
    blocks, relations = [], []
    # split into blocks: blank-line-separated; bullets are their own block
    raw_blocks: list[str] = []
    buf: list[str] = []
    for line in body.splitlines():
        if re.match(r"^\s*[-*]\s+", line):  # bullet → flush buffer, own block
            if buf:
                raw_blocks.append("\n".join(buf)); buf = []
            raw_blocks.append(line)
        elif not line.strip():
            if buf:
                raw_blocks.append("\n".join(buf)); buf = []
        elif line.startswith("#"):  # heading — context, not a fact
            if buf:
                raw_blocks.append("\n".join(buf)); buf = []
        else:
            buf.append(line)
    if buf:
        raw_blocks.append("\n".join(buf))

    seen_ids: set[str] = set()
    for raw in raw_blocks:
        m = PREDICATE.match(raw)
        if m and WIKILINK.search(m.group(2)):  # typed relation, e.g. `uses:: [[claude]]`
            for target in WIKILINK.findall(m.group(2)):
                relations.append((m.group(1).lower(), target.strip().lower()))
            continue
        text = re.sub(r"^\s*[-*]\s+", "", raw).strip()
        if len(text) < 8:  # skip stubs
            continue
        clean = WIKILINK.sub(lambda mm: mm.group(1), text)  # [[x|y]] → x for embedding
        bid = hashlib.sha256(f"{rel}:{clean}".encode()).hexdigest()[:12]
        if bid in seen_ids:  # identical line repeated in the same note = same fact
            continue
        seen_ids.add(bid)
        blocks.append(
            {
                "id": bid,
                "text": clean[:2000],
                "links": [t.strip().lower() for t in WIKILINK.findall(text)],
                "tags": note_tags + HASHTAG.findall(text),
            }
        )
    return {"entity": entity, "path": rel, "tags": note_tags, "blocks": blocks, "relations": relations}


def _parse_vault() -> list[dict]:
    return [_parse_note(p) for p in _notes()]


# ── staleness ────────────────────────────────────────────────────────────
def _vault_state() -> str:
    h = hashlib.sha256()
    for p in _notes():
        st = p.stat()
        h.update(f"{p}:{st.st_mtime_ns}:{st.st_size}".encode())
    return h.hexdigest()


_state: str | None = None
_conn: kuzu.Connection | None = None
_parsed: list[dict] = []


def _refresh() -> None:
    global _state, _conn, _parsed
    s = _vault_state()
    if s == _state and _conn is not None:
        return
    _parsed = _parse_vault()
    _conn = _rebuild_graph(_parsed)
    _state = s


# ── embeddings ───────────────────────────────────────────────────────────
def _embed(text: str, prefix: str) -> np.ndarray | None:
    """nomic-embed needs task prefixes; Matryoshka-truncate to EMB_DIM + renormalize."""
    try:
        req = urllib.request.Request(
            f"{OLLAMA}/api/embeddings",
            data=json.dumps({"model": EMB_MODEL, "prompt": f"{prefix}: {text}"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            v = np.array(json.load(r)["embedding"], dtype=np.float32)[:EMB_DIM]
        n = np.linalg.norm(v)
        return v / n if n else None
    except Exception:
        return None


def _emb_cache() -> dict[str, np.ndarray]:
    if not EMB_FILE.exists():
        return {}
    z = np.load(EMB_FILE)
    cache = {k: z[k] for k in z.files}
    # dim change → invalidate
    if cache and next(iter(cache.values())).shape[0] != EMB_DIM:
        return {}
    return cache


def _ensure_embeddings() -> dict[str, np.ndarray]:
    cache, dirty = _emb_cache(), False
    live_ids = set()
    for note in _parsed:
        for b in note["blocks"]:
            live_ids.add(b["id"])
            if b["id"] not in cache:
                v = _embed(f"{note['entity']}: {b['text']}", "search_document")
                if v is not None:
                    cache[b["id"]] = v
                    dirty = True
    stale = set(cache) - live_ids
    if stale:
        cache = {k: v for k, v in cache.items() if k in live_ids}
        dirty = True
    if dirty:
        CACHE.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(EMB_FILE, **cache)
    return cache


# ── graph ────────────────────────────────────────────────────────────────
def _rebuild_graph(parsed: list[dict]) -> kuzu.Connection:
    if DB_PATH.exists():  # kuzu: dir in old versions, single file in new
        shutil.rmtree(DB_PATH) if DB_PATH.is_dir() else DB_PATH.unlink()
    for suffix in (".wal", ".shm"):
        p = DB_PATH.with_name(DB_PATH.name + suffix)
        if p.exists():
            p.unlink()
    CACHE.mkdir(parents=True, exist_ok=True)
    db = kuzu.Database(str(DB_PATH))
    conn = kuzu.Connection(db)
    conn.execute("CREATE NODE TABLE Note(name STRING, path STRING, PRIMARY KEY(name))")
    conn.execute("CREATE NODE TABLE Block(id STRING, text STRING, note STRING, PRIMARY KEY(id))")
    conn.execute("CREATE REL TABLE MENTIONS(FROM Block TO Note)")
    conn.execute("CREATE REL TABLE RELATES(FROM Note TO Note, predicate STRING)")

    def ensure_note(name: str, path: str = "") -> None:
        conn.execute("MERGE (n:Note {name: $n}) ON CREATE SET n.path = $p", {"n": name, "p": path})

    for note in parsed:
        ensure_note(note["entity"], note["path"])
    for note in parsed:
        for b in note["blocks"]:
            conn.execute(
                "CREATE (:Block {id: $id, text: $t, note: $n})",
                {"id": b["id"], "t": b["text"], "n": note["entity"]},
            )
            for target in {note["entity"], *b["links"]}:
                ensure_note(target)
                conn.execute(
                    "MATCH (b:Block {id: $id}), (n:Note {name: $n}) CREATE (b)-[:MENTIONS]->(n)",
                    {"id": b["id"], "n": target},
                )
        for pred, target in note["relations"]:
            ensure_note(target)
            conn.execute(
                "MATCH (a:Note {name: $a}), (b:Note {name: $b}) CREATE (a)-[:RELATES {predicate: $p}]->(b)",
                {"a": note["entity"], "b": target, "p": pred},
            )
    return conn


# ── tools ────────────────────────────────────────────────────────────────
@mcp.tool
def remember(text: str, note: str = "inbox", links: list[str] | None = None, tags: list[str] | None = None) -> str:
    """Store a fact as a markdown bullet in the vault's agent folder.
    `note`: which agent note to append to (e.g. "lucas", "projects"). `links`:
    entities to wikilink (become graph edges). `tags`: appended as #tags."""
    safe = re.sub(r"[^\w\s-]", "", note).strip() or "inbox"
    target = VAULT / AGENT_DIR / f"{safe}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    line = text.strip()
    for l in links or []:
        if f"[[{l}]]" not in line:
            line += f" [[{l}]]"
    for t in tags or []:
        line += f" #{t}"
    if target.exists() and line in target.read_text():
        return "duplicate — already stored"
    if not target.exists():
        target.write_text(f"# {safe}\n\n")
    with target.open("a") as fh:
        fh.write(f"- {line}\n")
    return f"stored in {AGENT_DIR}/{safe}.md"


@mcp.tool
def recall(query: str, k: int = 5, expand: bool = True) -> str:
    """Top-k relevant blocks from the whole vault (vector search, keyword fallback);
    `expand` adds graph-neighbor blocks of the notes the hits link to."""
    _refresh()
    blocks = [(n, b) for n in _parsed for b in n["blocks"]]
    if not blocks:
        return "vault is empty"
    emb = _ensure_embeddings()
    q = _embed(query, "search_query")

    if q is not None and emb:
        scored = sorted(
            ((float(np.dot(q, emb[b["id"]])), n, b) for n, b in blocks if b["id"] in emb),
            key=lambda x: -x[0],
        )
        hits = [(n, b) for s, n, b in scored[:k] if s > 0.3]
    else:
        words = set(re.findall(r"\w+", query.lower()))
        scored = sorted(
            ((len(words & set(re.findall(r"\w+", b["text"].lower()))), n, b) for n, b in blocks),
            key=lambda x: -x[0],
        )
        hits = [(n, b) for s, n, b in scored[:k] if s > 0]

    seen = {b["id"] for _, b in hits}
    out = [f"[{n['path']}] {b['text']}" for n, b in hits]

    if expand and hits:
        ents = {e for _, b in hits for e in b["links"]} | {n["entity"] for n, _ in hits}
        conn = _conn
        for e in ents:
            res = conn.execute(
                "MATCH (b:Block)-[:MENTIONS]->(:Note {name: $n}) RETURN b.id, b.note, b.text LIMIT 5",
                {"n": e},
            )
            while res.has_next():
                bid, bnote, btext = res.get_next()
                if bid not in seen:
                    seen.add(bid)
                    out.append(f"[via {e}] ({bnote}) {btext}")
    return "\n".join(out[: k * 3]) if out else "no relevant facts"


@mcp.tool
def forget(fragment: str, note: str = "") -> str:
    """Remove agent-memory bullets containing `fragment` (agent folder ONLY —
    your own notes are never touched). Optional `note` narrows to one file."""
    root = VAULT / AGENT_DIR
    if not root.exists():
        return "agent folder is empty"
    removed = 0
    for p in root.glob(f"{note or '*'}.md"):
        lines = p.read_text().splitlines()
        kept = [l for l in lines if not (l.lstrip().startswith(("-", "*")) and fragment.lower() in l.lower())]
        if len(kept) != len(lines):
            removed += len(lines) - len(kept)
            p.write_text("\n".join(kept) + "\n")
    return f"removed {removed} bullet(s)" if removed else f"nothing matched {fragment!r}"


@mcp.tool
def neighbors(entity: str) -> str:
    """Graph view of one note/entity: typed relations, linked notes, mentioning blocks."""
    _refresh()
    conn = _conn
    name, out = entity.lower(), []
    res = conn.execute(
        "MATCH (a:Note {name: $n})-[r:RELATES]->(b:Note) RETURN r.predicate, b.name", {"n": name}
    )
    while res.has_next():
        p, b = res.get_next()
        out.append(f"{name} --{p}--> {b}")
    res = conn.execute(
        "MATCH (a:Note)-[r:RELATES]->(b:Note {name: $n}) RETURN a.name, r.predicate", {"n": name}
    )
    while res.has_next():
        a, p = res.get_next()
        out.append(f"{a} --{p}--> {name}")
    res = conn.execute(
        "MATCH (b:Block)-[:MENTIONS]->(:Note {name: $n}) RETURN b.note, b.text", {"n": name}
    )
    while res.has_next():
        bnote, btext = res.get_next()
        out.append(f"({bnote}) {btext}")
    return "\n".join(out) if out else f"unknown entity: {entity}"


@mcp.tool
def stats() -> str:
    """Vault index stats: notes, blocks, entities, relations, embedding coverage."""
    _refresh()
    nblocks = sum(len(n["blocks"]) for n in _parsed)
    nrel = sum(len(n["relations"]) for n in _parsed)
    ents = {n["entity"] for n in _parsed} | {l for n in _parsed for b in n["blocks"] for l in b["links"]}
    emb = _emb_cache()
    covered = sum(1 for n in _parsed for b in n["blocks"] if b["id"] in emb)
    return (
        f"vault: {VAULT}\n{len(_parsed)} notes, {nblocks} blocks, {len(ents)} entities, "
        f"{nrel} typed relations\nembeddings: {covered}/{nblocks} @ {EMB_DIM}d"
    )


# ── cli ──────────────────────────────────────────────────────────────────
def _cli() -> None:
    import argparse

    p = argparse.ArgumentParser(prog="mem", description="synapse memory CLI")
    sub = p.add_subparsers(dest="cmd")

    r = sub.add_parser("remember")
    r.add_argument("text")
    r.add_argument("--note", default="inbox")
    r.add_argument("--links", nargs="*", default=[])
    r.add_argument("--tags", nargs="*", default=[])

    q = sub.add_parser("recall")
    q.add_argument("query")
    q.add_argument("-k", type=int, default=5)
    q.add_argument("--no-expand", action="store_true")

    f = sub.add_parser("forget")
    f.add_argument("fragment")
    f.add_argument("--note", default="")

    n = sub.add_parser("neighbors")
    n.add_argument("entity")

    sub.add_parser("stats")

    args = p.parse_args()
    if args.cmd == "remember":
        print(remember(args.text, args.note, args.links, args.tags))
    elif args.cmd == "recall":
        print(recall(args.query, args.k, not args.no_expand))
    elif args.cmd == "forget":
        print(forget(args.fragment, args.note))
    elif args.cmd == "neighbors":
        print(neighbors(args.entity))
    elif args.cmd == "stats":
        print(stats())
    else:
        mcp.run()  # no subcommand → serve MCP


if __name__ == "__main__":
    _cli()
