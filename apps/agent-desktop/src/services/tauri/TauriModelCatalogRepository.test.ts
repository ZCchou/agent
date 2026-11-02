import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn() }));
import { invoke } from "@tauri-apps/api/core";
import { TauriModelCatalogRepository } from "./TauriModelCatalogRepository";

describe("TauriModelCatalogRepository", () => {
  beforeEach(() => vi.mocked(invoke).mockReset());

  it("maps model admission to one typed change object", async () => {
    vi.mocked(invoke).mockResolvedValue({ schema_version: 5 });
    await new TauriModelCatalogRepository().setEnabled("remote", "model-a", false, true);
    expect(invoke).toHaveBeenCalledWith("set_provider_model_enabled", { change: {
      provider_id: "remote", model_id: "model-a", enabled: false,
      confirm_affected_conversations: true,
    } });
  });

  it("keeps manual models and default selection as separate operations", async () => {
    vi.mocked(invoke).mockResolvedValue({ schema_version: 5 });
    const repository = new TauriModelCatalogRepository();
    await repository.addManual("remote", "private-model");
    expect(invoke).toHaveBeenLastCalledWith("add_manual_provider_model", {
      providerId: "remote", modelId: "private-model",
    });
    await repository.updateDefault("remote", "private-model");
    expect(invoke).toHaveBeenLastCalledWith("update_default_model", {
      providerProfileId: "remote", modelId: "private-model",
    });
  });

  it("persists explicit model context limits without guessing them in the UI", async () => {
    vi.mocked(invoke).mockResolvedValue({ schema_version: 7 });
    await new TauriModelCatalogRepository().updateContextLimits("remote", "model-a", 32768, 4096);
    expect(invoke).toHaveBeenCalledWith("update_model_context_limits", { request: {
      provider_id: "remote", model_id: "model-a", context_length: 32768, max_output_tokens: 4096,
    } });
  });

  it("persists routing metadata through one typed request", async () => {
    vi.mocked(invoke).mockResolvedValue({ schema_version: 12 });
    await new TauriModelCatalogRepository().updateRoutingMetadata(
      "remote", "model-a", "1.25", "4.50", 82, 3,
    );
    expect(invoke).toHaveBeenCalledWith("update_model_routing_metadata", { request: {
      provider_id: "remote", model_id: "model-a", input_price: "1.25",
      output_price: "4.50", quality_score: 82, priority: 3,
    } });
  });
});
