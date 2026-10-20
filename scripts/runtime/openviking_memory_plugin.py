"""Generate the OpenClaw tools that call the trusted Memory Gateway."""

from __future__ import annotations

import json
from pathlib import Path


PLUGIN_ID = "touming-memory-tools"
CROSS_AGENT_QUERY_TOOL = "touming_agent_query"
TOOL_ACTIONS = {
    "touming_memory_read": "read",
    "touming_memory_write": "write",
    "touming_memory_update": "update",
    "touming_memory_delete": "delete",
}


def write_memory_plugin(openclaw_dir: Path, namespace: str, listen_port: int) -> Path:
    """Write one namespace-specific plugin without embedding OpenViking secrets."""

    plugin_dir = openclaw_dir / "extensions" / PLUGIN_ID
    plugin_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "id": PLUGIN_ID,
        "name": "Touming Memory Tools",
        "description": "Identity-bound OpenViking memory tools for Touming agents.",
        "version": "1.0.0",
        "configSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "bridgeUrl": {"type": "string"},
                "namespace": {"type": "string"},
            },
        },
    }
    package_json = {
        "name": PLUGIN_ID,
        "version": "1.0.0",
        "type": "module",
        "main": "./bundle.js",
        "openclaw": {"extensions": ["./bundle.js"]},
    }
    bridge_url = f"http://host.docker.internal:{listen_port}"
    bundle = _build_bundle(namespace=namespace, bridge_url=bridge_url)
    (plugin_dir / "openclaw.plugin.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (plugin_dir / "package.json").write_text(
        json.dumps(package_json, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (plugin_dir / "bundle.js").write_text(bundle, encoding="utf-8")
    return plugin_dir


def _build_bundle(*, namespace: str, bridge_url: str) -> str:
    tool_actions = json.dumps(TOOL_ACTIONS)
    return f"""const TOOL_ACTIONS = {tool_actions};

function asString(value) {{
  return value === undefined || value === null ? "" : String(value);
}}

function resolveAgentId(ctx) {{
  const explicit = asString(ctx?.agentId).trim();
  if (explicit) return explicit;
  const match = /^agent:([^:]+):/.exec(asString(ctx?.sessionKey).trim());
  return match ? match[1] : "";
}}

function parametersFor(action) {{
  if (action === "read") {{
    return {{
      type: "object",
      additionalProperties: false,
      properties: {{
        query: {{ type: "string", description: "Question to search in the current Agent's own memory." }},
        uri: {{ type: "string", description: "Optional URI returned by an earlier memory search." }},
        limit: {{ type: "integer", minimum: 1, maximum: 20 }},
      }},
    }};
  }}
  if (action === "write") {{
    return {{
      type: "object",
      additionalProperties: false,
      required: ["content"],
      properties: {{
        path: {{ type: "string", description: "Optional relative .md path inside this Agent's memory space." }},
        content: {{ type: "string", description: "Long-term memory content to persist." }},
      }},
    }};
  }}
  if (action === "update") {{
    return {{
      type: "object",
      additionalProperties: false,
      required: ["uri", "content"],
      properties: {{
        uri: {{ type: "string", description: "Existing URI from this Agent's own memory space." }},
        content: {{ type: "string", description: "Replacement memory content." }},
      }},
    }};
  }}
  return {{
    type: "object",
    additionalProperties: false,
    required: ["uri"],
    properties: {{
      uri: {{ type: "string", description: "Existing URI from this Agent's own memory space." }},
    }},
  }};
}}

function descriptionFor(action) {{
  if (action === "read") return "Search or read only the current Agent's own long-term memory.";
  if (action === "write") return "Create a memory only in the current Agent's own memory space.";
  if (action === "update") return "Update a memory only in the current Agent's own memory space.";
  return "Delete a memory only from the current Agent's own memory space.";
}}

async function postJson(url, token, body) {{
  const headers = {{ "Content-Type": "application/json" }};
  if (token) headers.Authorization = `Bearer ${{token}}`;
  const response = await fetch(url, {{
    method: "POST",
    headers,
    body: JSON.stringify(body),
  }});
  const payload = await response.json().catch(() => ({{ status: "denied", message: "Invalid Memory Gateway response." }}));
  if (!response.ok && !payload.message) payload.message = "Memory Gateway request failed.";
  return payload;
}}

export default function(api) {{
  const config = api.pluginConfig && typeof api.pluginConfig === "object" ? api.pluginConfig : {{}};
  const runtimeNamespace = asString(config.namespace || process.env.TSDS_RUNTIME_NAMESPACE || {namespace!r});
  const gateway = asString(config.bridgeUrl || process.env.TSDS_MEMORY_TOOL_BRIDGE_URL || {bridge_url!r}).replace(/\\/+$/, "");
  const token = asString(process.env.TSDS_RUNTIME_API_TOKEN || "");
  const names = Object.keys(TOOL_ACTIONS);
  api.registerTool((ctx) => names.map((name) => ({{
    name,
    description: descriptionFor(TOOL_ACTIONS[name]),
    parameters: parametersFor(TOOL_ACTIONS[name]),
    async execute(_toolCallId, params) {{
      const payload = await postJson(
        `${{gateway}}/runtime/${{encodeURIComponent(runtimeNamespace)}}/agent-memory-tool`,
        token,
        {{
          tool: name,
          action: TOOL_ACTIONS[name],
          agent_id: resolveAgentId(ctx),
          session_key: asString(ctx?.sessionKey),
          arguments: params && typeof params === "object" ? params : {{}},
        }},
      );
      const text = payload.status === "completed"
        ? JSON.stringify(payload.data ?? payload, null, 2)
        : asString(payload.message || "Memory request was denied.");
      return {{ content: [{{ type: "text", text }}], data: payload }};
    }},
  }})), {{ optional: true, names }});

  api.registerTool((ctx) => ({{
    name: {CROSS_AGENT_QUERY_TOOL!r},
    description: "CEO-only: query another Agent's OpenViking memory and authorized knowledge resources in this company. Use this when the user asks for facts owned by another Agent.",
    parameters: {{
      type: "object",
      additionalProperties: false,
      required: ["target_agent", "query"],
      properties: {{
        target_agent: {{ type: "string", description: "Target Agent ID, name, or role, for example 采购 Agent." }},
        query: {{ type: "string", description: "The concrete business question to search for." }},
        sources: {{
          type: "array",
          items: {{ type: "string", enum: ["memory", "resource"] }},
          description: "Optional sources; defaults to both memory and resource.",
        }},
        limit: {{ type: "integer", minimum: 1, maximum: 20 }},
      }},
    }},
    async execute(_toolCallId, params) {{
      const payload = await postJson(
        `${{gateway}}/runtime/${{encodeURIComponent(runtimeNamespace)}}/agent-query`,
        token,
        {{
          tool: {CROSS_AGENT_QUERY_TOOL!r},
          agent_id: resolveAgentId(ctx),
          session_key: asString(ctx?.sessionKey),
          arguments: params && typeof params === "object" ? params : {{}},
        }},
      );
      const text = payload.status === "completed"
        ? JSON.stringify(payload, null, 2)
        : asString(payload.message || "Cross-Agent query was denied.");
      return {{ content: [{{ type: "text", text }}], data: payload }};
    }},
  }}), {{ optional: true, names: [{CROSS_AGENT_QUERY_TOOL!r}] }});
}}
"""
