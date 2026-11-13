import { createInstance } from "i18next";
import { describe, expect, it } from "vitest";
import en from "../../i18n/locales/en/chat";
import zh from "../../i18n/locales/zh-CN/chat";
import { documentProcessingLabel } from "./documentProcessing";

describe("document processing activity", () => {
  it.each([
    ["en", "Reading report.docx", "OCR page 2 of 8"],
    ["zh-CN", "正在读取 report.docx", "正在 OCR 第 2/8 页"],
  ])("formats parsing and OCR progress in %s", async (language, parsing, ocr) => {
    const i18n = createInstance();
    await i18n.init({ lng: language, defaultNS: "chat", resources: {
      en: { chat: en }, "zh-CN": { chat: zh },
    } });
    const translate = (key: string, values: Record<string, string | number>) => i18n.t(key, values);
    expect(documentProcessingLabel({ phase: "parsing", name: "report.docx" }, translate)).toBe(parsing);
    expect(documentProcessingLabel({ phase: "ocr", page: 2, total_pages: 8 }, translate)).toBe(ocr);
    expect(documentProcessingLabel({ phase: "other" }, translate)).toBeNull();
  });
  it("shows model loading and reranking counts", () => {
    const translate = (key: string, values: Record<string, string | number>) => `${key} ${values.completed}/${values.total}`;
    expect(documentProcessingLabel({ phase: "reranking", completed: 4, total: 9 }, translate))
      .toBe("attachments.processing.reranking 4/9");
    expect(documentProcessingLabel({ phase: "model_loading", completed: 0, total: 1 }, translate))
      .toBe("attachments.processing.modelLoading 0/1");
  });
});
