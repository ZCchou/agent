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
  it("shows reranking candidate counts", () => {
    const translate = (key: string, values: Record<string, string | number>) => `${key} ${values.completed}/${values.total}`;
    expect(documentProcessingLabel({ phase: "reranking", completed: 4, total: 9 }, translate))
      .toBe("attachments.processing.reranking 4/9");
  });
  it.each([
    ['embedding', 'Loading embedding model BGE'],
    ['reranker', 'Loading reranking model BGE'],
    [undefined, 'Loading a retrieval model'],
  ])('identifies a loading %s model without treating candidates as model counts', async (role, expected) => {
    const i18n = createInstance();
    await i18n.init({ lng: 'en', defaultNS: 'chat', resources: { en: { chat: en } } });
    expect(documentProcessingLabel({ phase: 'model_loading', model_role: role, model_name: 'BGE', completed: 0, total: 20 },
      (key, values) => i18n.t(key, values))).toBe(expected);
  });
  it.each([
    ["en", "Planning template 2/8", "Filling template 5/12"],
    ["zh-CN", "正在规划模板 2/8", "正在填充模板 5/12"],
  ])("formats template planning and filling progress in %s", async (language, planning, filling) => {
    const i18n = createInstance();
    await i18n.init({ lng: language, defaultNS: "chat", resources: {
      en: { chat: en }, "zh-CN": { chat: zh },
    } });
    const translate = (key: string, values: Record<string, string | number>) => i18n.t(key, values);
    expect(documentProcessingLabel({ phase: "template_planning", completed: 2, total: 8, elapsed_seconds: 9 }, translate))
      .toBe(planning);
    expect(documentProcessingLabel({ phase: "template_filling", completed: 5, total: 12 }, translate))
      .toBe(filling);
  });
});
