import type { ToolOperation } from "../../types/domain";

export interface AgentExecutionRepository {
  listConversationOperations(conversationId: string): Promise<ToolOperation[]>;
  resolveTerminal(operationId: string, approve: boolean): Promise<void>;
  listOperations(runId: string): Promise<ToolOperation[]>;
  subscribeErrors(listener: (error: { operation_id: string | null; message: string }) => void): Promise<() => void>;
}
