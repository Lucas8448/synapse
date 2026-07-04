"""Auto-attach the Synapse memory tools to every chat/responses request.

Any OpenAI-compatible client pointed at the gateway gets the memory tools with
zero per-app MCP setup: this pre-call hook injects the ``litellm_proxy`` MCP
tool block, and LiteLLM's own auto-execution (``require_approval: never``) then
lists + runs the tools server-side and folds the results back into the reply.

Loaded via ``litellm_settings.callbacks`` in gateway/litellm.yaml. LiteLLM
resolves the module relative to the config file's directory, so this file is
symlinked into ~/.config/synapse/ (see bin/spine-sync).
"""

from typing import Any, Optional, Union

from litellm.integrations.custom_logger import CustomLogger

# MCP tool block pointing back at this same proxy's /synapse/mcp route. The
# server_url MUST start with "litellm_proxy" for LiteLLM to fetch + auto-execute
# the tools instead of forwarding the raw block to the upstream provider.
SYNAPSE_MCP_TOOL: dict = {
    "type": "mcp",
    "server_url": "litellm_proxy/synapse/mcp",
    "server_label": "synapse",
    "require_approval": "never",
    # To make memory read-only for apps, uncomment (names are the unprefixed
    # tool names): "allowed_tools": ["recall", "neighbors", "read_note", "stats"],
}

# Only user-facing text-generation calls take a tools list. Everything else
# (embeddings, moderation, the internal call_mcp_tool step, ...) is left alone.
ELIGIBLE_CALL_TYPES = {"completion", "acompletion", "responses", "aresponses"}


def _already_has_synapse(tools: list) -> bool:
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "mcp":
            continue
        if tool.get("server_label") == "synapse":
            return True
        if str(tool.get("server_url", "")).rstrip("/").endswith("/synapse/mcp"):
            return True
    return False


class SynapseToolAutoInject(CustomLogger):
    """Inject the Synapse MCP tool block into eligible proxy requests."""

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

        if _already_has_synapse(tools):
            return data

        data["tools"] = [*tools, dict(SYNAPSE_MCP_TOOL)]
        return data


# Instance referenced from litellm.yaml: callbacks: ["mcp_autoinject.mcp_tools_autoinject"]
mcp_tools_autoinject = SynapseToolAutoInject()
