import { createInstance } from "i18next";
import { describe, expect, it } from "vitest";
import en from "../../i18n/locales/en/chat";
import zh from "../../i18n/locales/zh-CN/chat";
import { applyRunFailure, runErrorTranslationKey } from "./runErrors";
import type { ModelRun, ModelRunEvent } from "../../types/domain";

async function translated(code: string, message: string | null, language = "zh-CN") {
  const i18n = createInstance();
  await i18n.init({ lng: language, fallbackLng: "en", defaultNS: "chat", resources: {
    en: { chat: en }, "zh-CN": { chat: zh },
  } });
  return i18n.t(runErrorTranslationKey(code, message), { defaultValue: message ?? code });
}

describe("failed run limit diagnostics", () => {
  it("shows legacy tool-call exhaustion as calls rather than file operations", async () => {
    const text = await translated("tool_limit_exceeded", "The run exceeded its tool round limit.");
    expect(text).toContain("工具调用");
    expect(text).not.toContain("文件操作次数或数据量");
  });

  it.each([
    ["The run exceeded its read/search limit.", "读取"],
    ["The run exceeded its file mutation limit.", "文件变更"],
    ["Tool result exceeds the run data budget.", "数据量"],
    ["A change set exceeds the 20 MiB content budget.", "数据量"],
    ["A text mutation exceeds 10 MiB.", "数据量"],
    ["A text patch exceeds 10 MiB.", "数据量"],
    ["Terminal audit exceeds its limit.", "数据量"],
  ])("translates the known legacy cause %s", async (message, expected) => {
    expect(await translated("tool_limit_exceeded", message)).toContain(expected);
  });

  it.each([
    ["tool_call_limit_exceeded", "工具调用", "tool-call"],
    ["tool_read_limit_exceeded", "读取", "read/search"],
    ["tool_mutation_limit_exceeded", "文件变更", "file-change"],
    ["tool_data_limit_exceeded", "数据量", "data"],
  ])("localizes new limit code %s in both languages", async (code, chinese, english) => {
    expect(await translated(code, null)).toContain(chinese);
    expect((await translated(code, null, "en")).toLowerCase()).toContain(english);
  });

  it("keeps unknown legacy causes on the generic fallback", async () => {
    expect(await translated("tool_limit_exceeded", "Unknown Host data boundary")).toBe(zh.errors.tool_limit_exceeded);
  });

  it("does not classify other errors by a coincidentally matching message", async () => {
    expect(await translated("future_provider_error", "The run exceeded its tool round limit.")).toBe("The run exceeded its tool round limit.");
  });
});

describe("terminal response fallback", () => {
  const run = { id: "run-1", status: "generating", draft: { content: "Partial output", updated_at: "old" } } as ModelRun;
  const event = { run_id: "run-1", sequence: 1, event_type: "run.failed", timestamp: "now", data: {
    error_code: "internal_error", message: "Execution failed", fallback_content: "Partial output\nSystem execution explanation",
  } } as ModelRunEvent;

  it("keeps the event result when persistence has no draft", () => {
    const result = applyRunFailure({ ...run, draft: null }, event);
    expect(result.status).toBe("failed");
    expect(result.draft?.content).toContain("System execution explanation");
    expect(result.error_code).toBe("internal_error");
  });

  it("keeps previously streamed output for legacy failure events", () => {
    expect(applyRunFailure(run, { ...event, data: {} }).draft?.content).toBe("Partial output");
  });

  it("keeps interruptions distinct from failed and cancelled outcomes", () => {
    const interrupted = applyRunFailure(run, { ...event, event_type: "run.interrupted",
      data: { error_code: "core_interrupted" } });
    expect(interrupted.status).toBe("interrupted");
    expect(interrupted.draft?.content).toBe("Partial output");
  });

  it("does not apply a different run's terminal event", () => {
    expect(applyRunFailure(run, { ...event, run_id: "other" })).toBe(run);
  });
});
