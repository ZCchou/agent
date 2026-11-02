import { describe, expect, it } from "vitest";
import type { ProviderModelSettings } from "../../types/settings";
import { assistantRunStatusKey } from "../chat/AssistantRunMessage";
import { filterCatalogModels } from "./ModelCatalogDrawer";

function model(id: string, enabled: boolean): ProviderModelSettings {
  return {
    id, enabled, allow_automatic_routing: false, source: "discovered", status: "available", tool_mode: "unknown", last_seen_at: null,
    capabilities: {
      text: "yes", vision: "unknown", vision_source: "unknown", vision_status: "unknown",
      vision_error_code: null, vision_verified_at: null, reasoning: "unknown", tools: "unknown",
      context_length: null, max_output_tokens: null, context_length_source: "fallback",
      input_price_per_million: null, output_price_per_million: null,
    },
  };
}

describe("model and chat UI state", () => {
  it("filters the model catalog by query and admission without reordering", () => {
    const models = [model("Qwen-Coder", true), model("qwen-chat", false), model("deepseek-r1", true)];
    expect(filterCatalogModels(models, "QWEN", false).map((item) => item.id)).toEqual(["Qwen-Coder", "qwen-chat"]);
    expect(filterCatalogModels(models, "qwen", true).map((item) => item.id)).toEqual(["Qwen-Coder"]);
  });

  it("maps run phases to stable user-facing status keys", () => {
    expect(assistantRunStatusKey("queued", false)).toBe("preparing");
    expect(assistantRunStatusKey("building_context", false)).toBe("preparing");
    expect(assistantRunStatusKey("generating", false)).toBe("thinking");
    expect(assistantRunStatusKey("generating", true)).toBe("generating");
    expect(assistantRunStatusKey("waiting_tool", true)).toBe("workingFiles");
  });
});
