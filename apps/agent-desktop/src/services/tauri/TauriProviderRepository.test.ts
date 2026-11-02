import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn() }));
import { invoke } from "@tauri-apps/api/core";
import { TauriProviderRepository } from "./TauriProviderRepository";

describe("TauriProviderRepository", () => {
  beforeEach(() => vi.mocked(invoke).mockReset());

  it("saves preset identity separately from the entered API base", async () => {
    vi.mocked(invoke).mockResolvedValue({ schema_version: 5 });
    const provider = {
      id: "moonshot", name: "Kimi", kind: "openai_compatible" as const,
      preset_id: "moonshot_cn" as const, entered_base_url: "https://api.moonshot.cn/v1",
      enabled: false, api_key: "secret",
    };
    await new TauriProviderRepository().save(provider);
    expect(invoke).toHaveBeenCalledWith("save_model_provider", { provider });
  });

  it("requires an explicit affected-conversation confirmation to disable", async () => {
    vi.mocked(invoke).mockResolvedValue({ schema_version: 5 });
    const repository = new TauriProviderRepository();
    await repository.countReferences("remote");
    expect(invoke).toHaveBeenLastCalledWith("count_provider_conversation_references", { providerId: "remote" });
    await repository.setEnabled("remote", false, true);
    expect(invoke).toHaveBeenLastCalledWith("set_model_provider_enabled", {
      providerId: "remote", enabled: false, confirmAffectedConversations: true,
    });
  });
});
