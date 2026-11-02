import type { GlobalInstructions } from "../../types/memory";

export interface MemoryRepository {
  getGlobalInstructions(): Promise<GlobalInstructions>;
  updateGlobalInstructions(content: string, enabled: boolean): Promise<GlobalInstructions>;
  resetGlobalInstructions(): Promise<GlobalInstructions>;
}
