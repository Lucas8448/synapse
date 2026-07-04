"""Auto-attach a curated set of MCP tools to every chat/responses request.

Any OpenAI-compatible client pointed at the gateway gets a useful toolbox with
zero per-app MCP setup: this pre-call hook injects ``litellm_proxy`` MCP tool
blocks, and LiteLLM's own auto-execution (``require_approval: never``) lists +
runs the tools server-side and folds the results back into the reply.

Two tiers of tools (see the policy block below):
  * LEAN  — memory + web, attached to EVERY request. Cheap and safe.
  * RICH  — filesystem / git / GitHub, attached only to capable model tiers and
            curated down to READ-ONLY tools so a blanket auto-attach can't make
            destructive changes. The full tool sets stay reachable by asking for
            a server explicitly (server_url: litellm_proxy/mcp/<name>).

Loaded via ``litellm_settings.callbacks`` in gateway/litellm.yaml. LiteLLM
resolves the module relative to the config file's directory, so this file is
symlinked into ~/.config/synapse/ (see bin/spine-sync).
"""

from typing import Any, Optional, Union

from litellm.integrations.custom_logger import CustomLogger

# ─────────────────────────────────────────────────────────────────────────────
# POLICY — edit these dicts to change what the gateway auto-attaches.
#
# Each entry maps a gateway MCP server -> the exact (prefixed) tool names to
# expose from it. LiteLLM unions the ``allowed_tools`` of every attached block
# into ONE filter, so each block must list the tools it wants: a block with no
# list would make that union drop every *other* server's tools. Listing a write
# tool here is how you'd let apps mutate state — the defaults are deliberately
# read-only for everything except memory.

# LEAN: attached to EVERY request — cheap, safe, broadly useful.
LEAN_SERVERS: dict = {
    "synapse": [  # personal memory (Obsidian knowledge graph) — read + write
        "synapse_remember", "synapse_recall", "synapse_forget",
        "synapse_neighbors", "synapse_read_note", "synapse_stats",
    ],
    "fetch": ["fetch_fetch"],                                       # URL -> markdown
    "websearch": ["websearch_search", "websearch_fetch_content"],   # web search
}

# RICH: token-heavy / higher blast radius — attached only to CAPABLE_TIERS.
# Curated to READ-ONLY tools (no write/commit/merge/delete/push).
RICH_SERVERS: dict = {
    "files": [
        "files_read_file", "files_read_text_file", "files_read_multiple_files",
        "files_list_directory", "files_list_directory_with_sizes",
        "files_directory_tree", "files_search_files", "files_get_file_info",
        "files_list_allowed_directories",
    ],
    "git": [
        "git_git_status", "git_git_diff_unstaged", "git_git_diff_staged",
        "git_git_diff", "git_git_log", "git_git_show", "git_git_branch",
    ],
    "github": [
        "github_get_me", "github_get_file_contents", "github_list_commits",
        "github_get_commit", "github_search_code", "github_search_repositories",
        "github_search_issues", "github_search_pull_requests",
        "github_list_issues", "github_issue_read", "github_list_pull_requests",
        "github_pull_request_read", "github_list_branches", "github_list_tags",
        "github_list_releases", "github_get_latest_release",
    ],
}

# Model tiers (aliases) that ALSO receive RICH_SERVERS. Any other model — the
# free bulk/low tiers, or a provider model addressed directly — gets LEAN only,
# which keeps small models focused and paid calls lean. To attach the rich tools
# everywhere, add the other tiers here (mind the added per-call token cost).
CAPABLE_TIERS = {"frontier", "mid", "local"}
# ─────────────────────────────────────────────────────────────────────────────

# Only user-facing text-generation calls take a tools list. Everything else
# (embeddings, moderation, the internal call_mcp_tool step, ...) is left alone.
ELIGIBLE_CALL_TYPES = {"completion", "acompletion", "responses", "aresponses"}


def _servers_for(model: Optional[str]) -> dict:
    """Return {server_name: allowed_tools} to attach for the requested model."""
    servers = dict(LEAN_SERVERS)
    if (model or "").strip() in CAPABLE_TIERS:
        servers.update(RICH_SERVERS)
    return servers


def _mcp_block(name: str, allowed_tools: list) -> dict:
    # server_url MUST start with "litellm_proxy" for LiteLLM to fetch +
    # auto-execute the tools; the LAST path segment selects the server, so the
    # scoping form is litellm_proxy/mcp/<name> (not litellm_proxy/<name>/mcp).
    return {
        "type": "mcp",
        "server_url": f"litellm_proxy/mcp/{name}",
        "server_label": name,
        "require_approval": "never",
        "allowed_tools": list(allowed_tools),
    }


def _has_server(tools: list, name: str) -> bool:
    """True if the request already carries an MCP block for this server."""
    suffixes = (f"/mcp/{name}", f"/{name}/mcp")
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "mcp":
            continue
        if tool.get("server_label") == name:
            return True
        if str(tool.get("server_url", "")).rstrip("/").endswith(suffixes):
            return True
    return False


class SynapseToolAutoInject(CustomLogger):
    """Inject the curated MCP tool blocks into eligible proxy requests."""

    async def async_pre_call_hook(
        self,
        user_api_key_dict: Any,
        cache: Any,
        data: dict,
        call_type: str,
    ) -> Optional[Union[Exception, str, dict]]:
        if call_type not in ELIGIBLE_CALL_TYPES:
            return data

        tools = data.get("tools")
        if tools is None:
            tools = []
        elif not isinstance(tools, list):
            # Unexpected shape — don't touch it.
            return data

        for name, allowed_tools in _servers_for(data.get("model")).items():
            if _has_server(tools, name):
                continue
            tools.append(_mcp_block(name, allowed_tools))

        data["tools"] = tools
        return data


# Instance referenced from litellm.yaml: callbacks: ["mcp_autoinject.mcp_tools_autoinject"]
mcp_tools_autoinject = SynapseToolAutoInject()
