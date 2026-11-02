from __future__ import annotations

from typing import Any

from agent.execution.domain import CapabilityRequirements
from agent.execution.web_intent import ResolvedWebIntent, WebIntentResolver


_FILE_READ_MARKERS = (
    "读取", "查看文件", "打开文件", "检查文件", "搜索文件", "查找文件", "列出目录",
    "列出文件", "文件列表", "目录结构", "有哪些文件", "项目里有哪些", "查看项目文件",
    "看看代码", "检查代码", "分析代码", "read file", "open file", "inspect file",
    "search files", "list directory", "list files", "inspect code", "review code",
)

_FILE_WRITE_MARKERS = (
    "创建文件", "新建文件", "保存到", "写入", "修改文件", "编辑文件", "删除文件",
    "移动文件", "重命名", "更新代码", "修复代码", "实现这个", "保存为",
    "清空项目", "清空当前项目", "清空全部文件", "清空所有文件", "清空目录", "删除空目录",
    "清理没有内容的文件夹", "清理空文件夹",
    "删除所有文件", "删除全部文件", "清空工作区", "清空整个项目",
    "create file", "save to", "write file", "modify file", "edit file", "delete file",
    "delete all files", "remove all files", "delete empty directories", "remove empty directories", "empty project", "clear project",
    "wipe project", "move file", "rename file", "update code", "fix code", "implement this",
)


def resolve_web_requirement(content: str, web_request_mode: str) -> str:
    """Compatibility wrapper for callers without conversation context."""
    return WebIntentResolver().resolve(
        content=content, request_mode=web_request_mode,
    ).requirement


def resolve_project_file_requirement(content: str, conversation_kind: str) -> tuple[str, str]:
    """Expose project tools without making every project question a file operation."""
    if conversation_kind != "project_work":
        return "none", "none"
    normalized = content.casefold()
    if any(marker in normalized for marker in _FILE_WRITE_MARKERS):
        return "write", "required"
    if any(marker in normalized for marker in _FILE_READ_MARKERS):
        return "read", "required"
    return "available", "available"


def explicit_requirements(
    *, content: str, attachments: list[dict[str, Any]], conversation_kind: str,
    web_request_mode: str, reasoning_enabled: bool, minimum_context_tokens: int | None,
    resolved_web_intent: ResolvedWebIntent | None = None,
) -> CapabilityRequirements:
    """Resolve only facts that do not need semantic model judgement."""
    has_image = any(item.get("kind") == "image" for item in attachments if isinstance(item, dict))
    web_intent = resolved_web_intent or WebIntentResolver().resolve(
        content=content, request_mode=web_request_mode,
    )
    web = web_intent.requirement
    files, tools = resolve_project_file_requirement(content, conversation_kind)
    reasoning = "required" if reasoning_enabled else "standard"
    return CapabilityRequirements(
        vision="required" if has_image else "none",
        web=web, web_evidence=web_intent.evidence,
        files=files,
        tools=tools,
        reasoning=reasoning,
        minimum_context_tokens=minimum_context_tokens,
    )


def merge_routing_intent(
    requirements: CapabilityRequirements, intent: dict[str, Any], *, web_allowed: bool,
    conversation_kind: str = "standard_chat",
) -> CapabilityRequirements:
    """Merge an advisor suggestion without allowing it to expand permissions."""
    suggested_web = intent.get("web")
    web = requirements.web
    if web == "optional" and suggested_web in {
        "not_needed", "helpful", "required",
    }:
        web = "required" if suggested_web in {"helpful", "required"} and web_allowed else "off"
    reasoning = requirements.reasoning
    if reasoning == "standard" and intent.get("reasoning") == "high":
        reasoning = "preferred"
    files = requirements.files
    tools = requirements.tools
    if conversation_kind == "project_work" and "file_work" in (intent.get("task_kinds") or ()):
        if files == "available":
            files = "write"
        if tools == "available":
            tools = "required"
    if conversation_kind == "project_work":
        suggested_files = intent.get("files")
        if suggested_files == "write" and files in {"none", "available", "read"}:
            files = "write"
        elif suggested_files == "read" and files in {"none", "available"}:
            files = "read"
        if intent.get("tools") == "required" and tools in {"none", "available"}:
            tools = "required"
    return CapabilityRequirements(
        text=requirements.text, vision=requirements.vision, web=web,
        web_evidence=requirements.web_evidence,
        files=files, tools=tools, reasoning=reasoning,
        minimum_context_tokens=requirements.minimum_context_tokens,
    )


def needs_reasoning_intent_analysis(content: str) -> bool:
    """Detect only high-signal, still-ambiguous requests that merit an advisor call."""
    normalized = content.casefold()
    markers = (
        "仔细分析", "深入分析", "全面评估", "权衡利弊", "系统设计", "架构评估",
        "是否应该", "哪种方案", "潜在风险", "完整计划",
        "analyze deeply", "carefully evaluate", "trade-offs", "architecture review",
        "which approach", "potential risks", "comprehensive plan",
    )
    return any(marker in normalized for marker in markers)
