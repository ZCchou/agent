import { afterEach, expect, it, vi } from "vitest";
import { useState } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { LocalModelConfigurationForm, LocalModelJobs, LocalModelsPanel } from "./LocalModelsPanel";
import type { LocalModel } from "../../types/localModels";
vi.mock("react", async importOriginal => {
  const actual = await importOriginal<typeof import("react")>();
  return { ...actual, useState: vi.fn(actual.useState) };
});
afterEach(() => vi.mocked(useState).mockReset());
const model = { id: "a".repeat(64), device: "cpu", context_length: 8192, max_context_length: 32768, threads: 4, gpu_layers: -1, source: "local", license: "MIT" } as LocalModel;
it("offers a maximum shortcut only for an explicitly declared model limit", () => {
  const known = renderToStaticMarkup(<LocalModelConfigurationForm model={{ ...model, declared_context_length: 32768 }} busy={false} onSave={() => {}} />);
  expect(known).toContain('使用模型声明的最大上下文');
  expect(known).toContain('模型声明上限 32768');
  const missing = renderToStaticMarkup(<LocalModelConfigurationForm model={model} busy={false} onSave={() => {}} />);
  expect(missing).toContain('模型文件未声明最大上下文');
  expect(missing).toMatch(/<button[^>]*disabled[^>]*>使用模型声明的最大上下文/);
});
it("bounds configuration by model metadata and exposes explicit GPU choices", () => {
  const html = renderToStaticMarkup(<LocalModelConfigurationForm model={model} busy={false} onSave={() => {}} />);
  expect(html).toContain('max="32768"'); expect(html).toContain('value="8192"'); expect(html).toContain('value="cuda"'); expect(html).toContain('min="-1"');
});
it("renders task progress, cancellation and device errors", () => {
  const html = renderToStaticMarkup(<LocalModelJobs busy={false} jobs={[{ id: "job", status: "running", progress: 0.5 }, { id: "gpu", status: "failed", progress: 0, error: "CUDA not available" }]} onCancel={() => {}} />);
  expect(html).toContain('value="0.5"'); expect(html).toContain("取消任务"); expect(html).toContain("CUDA not available"); expect(html).toContain('role="alert"');
});

it("renders structured historical job diagnostics alongside a later successful job", () => {
  const jobs = [
    { id: "failed-import", kind: "import", model_id: null, status: "failed", progress: 0, error: { code: "source_changed", message: "The selected model changed during import." }, created_at: "2026-11-07", updated_at: "2026-11-07" },
    { id: "loaded", kind: "load", model_id: "a".repeat(64), status: "completed", progress: 1, error: null, created_at: "2026-11-08", updated_at: "2026-11-08" },
  ];
  const html = renderToStaticMarkup(<LocalModelJobs jobs={jobs} busy={false} onCancel={() => {}} />);
  expect(html).toContain("The selected model changed during import.");
  expect(html).toContain("source_changed");
  expect(html).toContain("load · completed");
  expect(html).not.toContain("取消任务");
});

it.each([
  { code: "device_unavailable", message: "CUDA device unavailable" },
  "Legacy CUDA failure",
])("keeps model and runtime failure recovery visible for %j", error => {
  vi.mocked(useState)
    .mockImplementationOnce(() => [{ models: [{ ...model, name: "Failed GGUF", status: "error", error, size_bytes: 1024 }], root: "C:/models" }, vi.fn()] as never)
    .mockImplementationOnce(() => [{ state: "error", error }, vi.fn()] as never);
  const html = renderToStaticMarkup(<LocalModelsPanel onChanged={async () => {}} onAuxiliary={() => {}} />);
  const message = typeof error === "string" ? error : error.message;
  expect(html.split(message)).toHaveLength(3);
  if (typeof error !== "string") expect(html.split("device_unavailable")).toHaveLength(3);
  expect(html).toContain("保存配置");
  expect(html).toContain("加载模型");
});


it("offers an explicit save-and-reload path for a loaded model", () => {
  const html = renderToStaticMarkup(<LocalModelConfigurationForm model={{ ...model, status: "loaded" }} busy={false} onSave={() => {}} />);
  expect(html).toContain("保存配置并重新加载");
  expect(html).toContain("优先 GPU");
});
