import { invoke } from "@tauri-apps/api/core";
import type { MemoryRepository } from "../repositories/MemoryRepository";
import type { GlobalInstructions } from "../../types/memory";

export class TauriMemoryRepository implements MemoryRepository {
  async getGlobalInstructions(): Promise<GlobalInstructions> {
    return (await invoke<{ global_instructions: GlobalInstructions }>(
      "get_global_instructions",
    )).global_instructions;
  }

  async updateGlobalInstructions(content: string, enabled: boolean): Promise<GlobalInstructions> {
    return (await invoke<{ global_instructions: GlobalInstructions }>(
      "update_global_instructions", { content, enabled },
    )).global_instructions;
  }

  async resetGlobalInstructions(): Promise<GlobalInstructions> {
    return (await invoke<{ global_instructions: GlobalInstructions }>(
      "reset_global_instructions",
    )).global_instructions;
  }
}
