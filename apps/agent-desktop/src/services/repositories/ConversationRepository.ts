import type {
  Conversation,
  ConversationFilter,
  ConversationKind,
  ProjectCategory,
} from "../../types/domain";

export interface CreateConversationInput {
  kind: ConversationKind;
  title: string;
  category?: ProjectCategory;
  chatGroupId?: string;
  projectId?: string;
}

export interface UpdateConversationInput {
  title: string;
  category?: ProjectCategory;
}

export interface ReorderConversationsInput {
  kind: ConversationKind;
  containerId: string | null;
  orderedIds: string[];
}

export interface ConversationRepository {
  list(filter?: ConversationFilter): Promise<Conversation[]>;
  get(id: string): Promise<Conversation>;
  create(input: CreateConversationInput): Promise<Conversation>;
  update(id: string, input: UpdateConversationInput): Promise<Conversation>;
  deriveWork(sourceId: string, projectId: string, title?: string): Promise<Conversation>;
  reorder(input: ReorderConversationsInput): Promise<Conversation[]>;
  moveChat(id: string, chatGroupId: string | null, targetIndex: number): Promise<Conversation>;
  moveWork(id: string, projectId: string, targetIndex: number): Promise<Conversation>;
  copyWork(sourceId: string, projectId: string, title?: string): Promise<Conversation>;
  delete(id: string): Promise<void>;
  updateModel(id: string, providerProfileId: string | null, modelId: string | null): Promise<Conversation>;
  updatePurpose(id: string, purpose: ProjectCategory | null): Promise<Conversation>;
  updateWebSearch(id: string, mode: "inherit" | "off" | "auto"): Promise<Conversation>;
  updateRouting(
    id: string,
    mode: "fixed" | "automatic",
    preset?: "quality" | "balanced" | "economy" | "local_first",
    providerProfileId?: string | null,
    modelId?: string | null,
  ): Promise<Conversation>;
}
