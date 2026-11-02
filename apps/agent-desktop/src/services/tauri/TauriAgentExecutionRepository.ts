import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import type { ToolOperation } from "../../types/domain";
import type { AgentExecutionRepository } from "../repositories/AgentExecutionRepository";

export class TauriAgentExecutionRepository implements AgentExecutionRepository {
  async resolveTerminal(operationId: string, approve: boolean): Promise<void> {
    await invoke('resolve_terminal_operation', { operationId, approve });
  }
  async listConversationOperations(conversationId: string): Promise<ToolOperation[]> {
    return (await invoke<{ operations: ToolOperation[] }>('list_conversation_tool_operations', { conversationId })).operations;
  }
  async listOperations(runId: string): Promise<ToolOperation[]> {
    return (await invoke<{ operations: ToolOperation[] }>("list_tool_operations", { runId })).operations;
  }

  subscribeErrors(listener: (error: { operation_id: string | null; message: string }) => void): Promise<() => void> {
    return listen("agent-tool-error", (event) => listener(event.payload as { operation_id: string | null; message: string }));
  }
}
