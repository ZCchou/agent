from __future__ import annotations

import json
from typing import Any

from agent.providers import ProviderError
from agent.tool_contracts import TOOL_CONTRACT_VERSION, ToolCall, ToolContractRegistry, validate_tool_call


STRUCTURED_TOOL_INSTRUCTION = """This model endpoint does not expose native tool calls.
Return exactly one JSON object and no markdown. For a final reply use:
{"type":"text","content":"..."}
For an allowed operation use:
{"type":"tool_call","id":"unique-id","name":"ALLOWED_TOOL_NAME","arguments":{...}}
Never invent a tool name or add fields outside that object."""
PROJECT_LINK_INSTRUCTION = (
    "When referring to a project directory in the final reply, use a controlled markdown link "
    "like [Open folder](project-dir:relative/path). Use project-relative paths only; never expose "
    "absolute filesystem paths."
)


def structured_messages(
    messages: list[dict[str, Any]], allowed_tools: set[str] | None = None,
) -> list[dict[str, Any]]:
    allowed = sorted(allowed_tools or {"fs.list", "fs.search", "fs.read", "fs.propose_changes"})
    schema = ToolContractRegistry.prompt_schema(allowed) if allowed else "[]"
    instruction = (
        f"{STRUCTURED_TOOL_INSTRUCTION}\n"
        f"{PROJECT_LINK_INSTRUCTION}\n"
        f"Tool contract version: {TOOL_CONTRACT_VERSION}.\n"
        f"Allowed tool names: {', '.join(allowed) if allowed else 'none'}.\n"
        f"Complete allowed tool schemas (JSON): {schema}"
    )
    if any("适航资料问答输出约定" in str(message.get("content", "")) for message in messages):
        instruction += (
            "\nFor airworthiness evidence answers, use the required transport envelope. "
            "Serialize the claims JSON as the string value of the content field in "
            "{\"type\":\"text\",\"content\":\"...\"}. Tool calls still use the tool_call envelope."
        )
    return [{"role": "system", "content": instruction}, *messages]


def parse_structured_output(content: str) -> tuple[str | None, ToolCall | None]:
    try:
        value = json.loads(content)
    except json.JSONDecodeError as error:
        raise ProviderError(
            "model_tool_protocol_failed",
            "The model did not return the required structured tool envelope.",
        ) from error
    if not isinstance(value, dict) or set(value) - {"type", "content", "id", "name", "arguments"}:
        raise ProviderError("model_tool_protocol_failed", "The structured tool envelope is invalid.")
    if value.get("type") == "text" and isinstance(value.get("content"), str) \
            and set(value) <= {"type", "content"}:
        return value["content"], None
    if value.get("type") == "tool_call" and set(value) == {"type", "id", "name", "arguments"}:
        call = ToolCall(value["id"], value["name"], value["arguments"])
        return None, validate_tool_call(call)
    raise ProviderError("model_tool_protocol_failed", "The structured tool envelope is invalid.")
