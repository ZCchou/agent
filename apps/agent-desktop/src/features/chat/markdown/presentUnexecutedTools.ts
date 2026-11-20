const zeroOperationFailure = /^> (?:执行说明（系统）|系统执行说明)：[^\n]*（(?:task_goal_invalid|model_tool_protocol_failed)）。已完成工具操作 0 项；失败或拒绝 0 项；结果尚不确定 0 项。[^\n]*\s*$/m;

function isUnexecutedEnvelope(paragraph: string): boolean {
  const block = paragraph.trimEnd();
  if (!block.startsWith("<tool_call>") || !/<\/(?:function|invoke)>$/.test(block)) return false;
  const parts = block.split(/(<tool_call>|<\/(?:function|invoke)>)/);
  let calls = 0;
  for (let index = 1; index < parts.length; index += 2) {
    const payload = parts[index + 1].trim();
    if (parts[index] !== "<tool_call>") {
      if (payload) return false;
      continue;
    }
    try {
      const call = JSON.parse(payload);
      if (!call || call.kind !== "builtin" || typeof call.id !== "string" || typeof call.name !== "string"
        || !call.args || typeof call.args !== "object" || Array.isArray(call.args)) return false;
      calls++;
    } catch { return false; }
  }
  return calls > 0;
}

/** Only replace model protocol debris accompanied by the legacy zero-operation outcome. */
export function presentUnexecutedTools(content: string, label: string) {
  if (!content.includes("<tool_call>")) return content;
  const notice = zeroOperationFailure.exec(content);
  if (!notice || content.slice(notice.index + notice[0].length).trim()) return content;
  const before = content.slice(0, notice.index);
  const paragraphs = before.split(/(\r?\n[ \t]*\r?\n)/);
  let fence = "";
  let changed = false;
  for (let index = 0; index < paragraphs.length; index += 2) {
    const paragraph = paragraphs[index];
    if (!fence && isUnexecutedEnvelope(paragraph)) {
      paragraphs[index] = label;
      changed = true;
      continue;
    }
    for (const line of paragraph.split(/\r?\n/)) {
      const marker = /^ {0,3}(`{3,}|~{3,})(.*)$/.exec(line);
      if (!marker) continue;
      if (!fence) fence = marker[1];
      else if (marker[1].startsWith(fence) && !marker[2].trim()) fence = "";
    }
  }
  return changed ? paragraphs.join("") + content.slice(notice.index) : content;
}
