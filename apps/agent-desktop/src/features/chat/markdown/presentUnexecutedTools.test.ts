import { describe, expect, it } from "vitest";
import { presentUnexecutedTools } from "./presentUnexecutedTools";

const envelope = '<tool_call>\n{"args":{"query":"task"},"id":"task","kind":"builtin","name":"task"}\n</function>\n'
  + '<tool_call>\n{"args":{"query":"rag"},"id":"rag","kind":"builtin","name":"rag"}\n</invoke>';
const outcome = "> 执行说明（系统）：任务目标要求无效，请核对处理范围与交付要求（task_goal_invalid）。已完成工具操作 0 项；失败或拒绝 0 项；结果尚不确定 0 项。";
const label = "任务未完成。模型生成的工具请求没有执行。";
describe("legacy assistant protocol failure presentation", () => {
  it("explains the failed requests and keeps the system outcome and existing work", () => {
    const text = ["已核查第 10.1 节。", envelope, "尚待复核湿热试验。", outcome].join("\n\n");
    expect(presentUnexecutedTools(text, label)).toBe(text.replace(envelope, label));
  });
  it("keeps code examples, quoted examples and ordinary discussion", () => {
    for (const source of [ "```xml\n\n" + envelope + "\n\n```", "~~~\n\n" + envelope + "\n\n~~~",
      "    " + envelope.replaceAll("\n", "\n    "), "> " + envelope.replaceAll("\n", "\n> "),
      "示例 <tool_call> 不会启动检索。" ]) {
      const text = source + "\n\n" + outcome;
      expect(presentUnexecutedTools(text,label)).toBe(text);
    }
  });
  it("keeps records while outcome is unknown or reports completed or uncertain operations", () => {
    for (const footer of ["", outcome.replace("已完成工具操作 0", "已完成工具操作 1"),
      outcome.replace("结果尚不确定 0", "结果尚不确定 1"), outcome + "\n\n这是文中示例。"]) {
      const text = envelope + "\n\n" + footer;
      expect(presentUnexecutedTools(text,label)).toBe(text);
    }
  });
});
