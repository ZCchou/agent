export const PROJECT_CATEGORIES = [
  "casual", "research", "work", "writing", "coding", "gaming", "other",
] as const;

export type ProjectCategory = typeof PROJECT_CATEGORIES[number];
export type ConversationKind = "standard_chat" | "project_work";

export interface ChatGroup {
  id: string;
  name: string;
  sort_order: number;
  created_at: string;
  updated_at: string;
}

export interface Project {
  id: string;
  name: string;
  category: ProjectCategory;
  sort_order: number;
  created_at: string;
  updated_at: string;
  deleted_at?: string | null;
  model_access_policy?: "local_only" | "allow_online";
  model_scope_policy?: "inherit_global" | "restricted";
  web_access_policy?: "disabled" | "allow_search";
}

export type MessageRole = "user" | "assistant" | "system_event";

export interface AttachmentReference {
  attachment_id: string;
  source_start?: number;
  source_end?: number;
}

export interface MessageAttachment {
  id: string;
  message_id?: string;
  kind: "image" | "text";
  name: string;
  media_type: string;
  byte_size: number;
  sha256: string;
  storage_key?: string;
  width: number | null;
  height: number | null;
  model_storage_key?: string | null;
  model_media_type?: "image/png" | "image/jpeg" | null;
  model_byte_size?: number | null;
  model_sha256?: string | null;
  model_width?: number | null;
  model_height?: number | null;
  normalization_version?: string | null;
  source_attachment_id?: string | null;
  created_at?: string;
  artifacts?: AttachmentArtifact[];
}

export interface AttachmentArtifact {
  artifact_id: string;
  kind: "text.utf8" | "image.normalized" | "document.text" | "document.page_image" | "audio.transcript" | "archive.manifest";
  processor_id: string;
  processor_version: string;
  media_type: string;
  byte_size: number;
  sha256: string;
  storage_key?: string;
  width?: number | null;
  height?: number | null;
}

export interface RejectedAttachment {
  receipt_id: string;
  display_name: string;
  detected_media_type?: string | null;
  error_code: string;
  processor_hint?: string | null;
}

export interface AttachmentImportBatchResult {
  accepted: MessageAttachment[];
  rejected: RejectedAttachment[];
}

export interface AttachmentDragStateEvent {
  target_id: string;
  phase: "enter" | "leave" | "processing";
  count: number;
  batch_id?: string | null;
}

export interface AttachmentDropImportedEvent {
  target_id: string;
  batch_id: string;
  result?: AttachmentImportBatchResult | null;
  error_code?: string | null;
}

export interface AttachmentPreview {
  id: string;
  kind: "image" | "text";
  name: string;
  media_type: string;
  data_url: string | null;
  content: string | null;
}

export interface AttachmentNormalizationStatus {
  attachment_id: string;
  status: "ready" | "legacy_pending" | "failed" | "not_applicable";
  normalization_version: string | null;
  model_media_type: "image/png" | "image/jpeg" | null;
  model_byte_size: number | null;
  model_width: number | null;
  model_height: number | null;
  error_code: string | null;
}

export interface ProjectFilePreview {
  path: string;
  kind: "text" | "image";
  media_type: string;
  content: string | null;
  data_url: string | null;
}

export interface ChatMessage {
  id: string;
  conversation_id: string;
  role: MessageRole;
  content: string;
  event_type: string | null;
  source_message_id: string | null;
  created_at: string;
  attachments?: MessageAttachment[];
  attachment_rejections?: Array<{
    id: string;
    display_name: string;
    detected_media_type?: string | null;
    error_code: string;
    processor_hint?: string | null;
  }>;
}

export interface Conversation {
  id: string;
  kind: ConversationKind;
  title: string;
  category: ProjectCategory | null;
  resolved_category: ProjectCategory;
  chat_group_id: string | null;
  project_id: string | null;
  sort_order: number;
  derived_from_conversation_id: string | null;
  derived_through_message_id: string | null;
  derived_at: string | null;
  copied_from_conversation_id: string | null;
  copied_through_message_id: string | null;
  copied_at: string | null;
  created_at: string;
  updated_at: string;
  deleted_at?: string | null;
  provider_profile_id?: string | null;
  model_id?: string | null;
  model_access_policy?: "local_only" | "allow_online" | null;
  model_scope_policy?: "inherit_global" | "restricted" | null;
  purpose_override?: ProjectCategory | null;
  web_search_mode?: "inherit" | "off" | "auto";
  model_selection_mode?: "fixed" | "automatic";
  routing_preset?: "quality" | "balanced" | "economy" | "local_first";
  coordinator_provider_profile_id?: string | null;
  coordinator_model_id?: string | null;
  messages: ChatMessage[];
}

export type ModelRunStatus = "queued" | "building_context" | "waiting_context" | "waiting_permission" | "waiting_budget" | "generating" | "waiting_tool" | "completed" | "failed" | "cancelled" | "interrupted";
export type ModelSelectionSource = "global_default" | "conversation_override" | "user_once" | "router";
export interface ModelRoutingPolicy {
  preset: "quality" | "balanced" | "economy" | "local_first";
  allow_online: boolean;
  max_estimated_cost?: string | null;
  max_extra_online_cost_usd?: string | null;
  max_context_tokens?: number | null;
  allowed_provider_ids?: string[];
  allowed_models?: ProjectModelRef[];
}
export interface ModelSelectionRequest {
  mode: "fixed" | "automatic";
  provider_profile_id?: string;
  model_id?: string;
  source?: Exclude<ModelSelectionSource, "router">;
  routing_policy?: ModelRoutingPolicy | null;
}
export interface ModelSelectionDecision {
  provider_profile_id: string;
  model_id: string;
  selection_source: ModelSelectionSource;
  reason_code: string;
  policy_version: string | null;
}
export interface ExecutionStep {
  id: string;
  phase: "analysis" | "specialist" | "synthesis";
  step_type: "intent_analysis" | "vision_analysis" | "web_research" | "reasoning_analysis" | "final_response" | "grounding_fallback";
  provider_profile_id: string;
  model_id: string;
  protocol_id?: "memory" | "ollama" | "openai_chat" | "openai_responses" | "anthropic_messages";
  invocation_route_id?: string;
  adapter_id?: string;
  capability_signature?: string[];
  status: "queued" | "running" | "completed" | "skipped" | "failed" | "cancelled" | "interrupted" | "unknown_outcome" | "stale";
}
export interface ExecutionPlan {
  id: string;
  status: string;
  plan_hash: string;
  selection_mode: "fixed" | "automatic";
  routing_preset: "quality" | "balanced" | "economy" | "local_first";
  coordinator_provider_profile_id: string;
  coordinator_model_id: string;
  estimated_extra_online_cost_usd: string | null;
  route_catalog_hash?: string;
  route_policy_version?: string;
  authority_generation?: number;
  web_backend_kind?: "tavily" | "searxng" | null;
  degraded_capabilities?: CapabilityAdvisory[];
  requirements?: Record<string, unknown>;
  resolved_intent?: {
    requirement?: string;
    freshness?: string;
    evidence?: string;
    source?: string;
    confidence?: string;
    signals?: string[];
    explanation_code?: string;
    advisor_error_code?: string | null;
    requested_requirements?: Record<string, unknown>;
    effective_requirements?: Record<string, unknown>;
    advisor?: Record<string, unknown>;
  };
  steps: ExecutionStep[];
  invocations?: InvocationSpecSummary[];
}
export interface CapabilityAdvisory {
  capability_id: string;
  requested_level: string;
  status: "unavailable" | "not_authorized" | "unverified" | "route_failed";
  reason_code: string;
  fallback_mode: string;
  user_message_key: string;
}
export interface InvocationSpecSummary {
  id: string;
  step_id: string;
  round_no: number;
  attempt_no: number;
  kind: "model" | "host_search";
  provider_or_backend_id: string;
  model_id: string | null;
  route_id: string;
  protocol_id: string;
  adapter_id: string;
  capability_signature: string[];
  status: "prepared" | "dispatched" | "streaming" | "completed" | "failed" | "cancelled" | "interrupted" | "unknown_outcome" | "stale";
  error_code: string | null;
}
export interface ModelRun {
  id: string;
  conversation_id: string;
  user_message_id: string;
  status: ModelRunStatus;
  provider_profile_id: string;
  model_id: string;
  selection_source?: ModelSelectionSource;
  selection_reason_code?: string;
  routing_policy_version?: string | null;
  execution_plan_id?: string | null;
  execution_policy_version?: string | null;
  context_strategy?: "default" | "expanded_retry";
  context_generation?: number;
  automatic_compaction_used?: number;
  web_search_request_mode?: "off" | "auto" | "force_once";
  web_search_route?: "none" | "native" | "host_tool" | "host_presearch" | "specialist";
  web_search_reason_code?: string;
  web_grounding_status?: "not_required" | "grounded" | "ungrounded_fallback" | "failed";
  error_code: string | null;
  error_message: string | null;
  draft: { content: string; updated_at: string } | null;
  assistant_message: ChatMessage | null;
}

export type WebSearchRequestMode = "off" | "auto" | "force_once";
export type WebEvidenceStatus = "not_evaluated" | "provider_reference" | "traceable_sources" | "missing" | "invalid" | "legacy_unknown";
export const WEB_SEARCH_ROUTES = ["none", "native", "host_tool", "host_presearch", "specialist"] as const;
export const WEB_OPERATION_STATUSES = [
  "prepared", "dispatched", "searching", "reading_sources", "completed",
  "failed", "cancelled", "interrupted", "unknown_outcome",
] as const;
export interface WebSource {
  id: string;
  operation_id: string;
  title: string;
  url: string;
  domain: string;
  snippet: string | null;
  published_at: string | null;
  retrieved_at: string;
  content_hash: string | null;
  trust: "external_untrusted";
}
export interface WebOperation {
  id: string;
  run_id: string;
  conversation_id?: string;
  user_message_id?: string;
  provider_profile_id?: string;
  model_id?: string;
  sequence: number;
  route: Exclude<(typeof WEB_SEARCH_ROUTES)[number], "none">;
  backend_id: string | null;
  query_text: string;
  status: (typeof WEB_OPERATION_STATUSES)[number];
  result_count: number;
  evidence_status: WebEvidenceStatus;
  verified_source_count: number;
  evidence_manifest_hash?: string | null;
  provider_reference_id?: string | null;
  usage: Record<string, unknown>;
  error_code: string | null;
  sources: WebSource[];
}
export interface ProjectModelRef { provider_profile_id: string; model_id: string }
export interface ProjectModelScope {
  project_id: string;
  policy: "inherit_global" | "restricted";
  models: ProjectModelRef[];
}
export interface ModelRunEvent {
  run_id: string;
  sequence: number;
  event_type: "run.progress_recorded" | "run.state_changed" | "run.chunk" | "run.draft_checkpoint" | "run.completed" | "run.failed" | "run.cancelled" | "run.tool_requested" | "run.tool_status_changed" | "run.change_set_ready" | "run.waiting_approval" | "run.tool_completed" | "run.web_search_started" | "run.web_search_completed" | "run.web_search_failed" | "run.web_evidence_evaluated" | "run.grounding_fallback_started" | "run.grounding_fallback_completed" | "run.intent_follow_up_pending" | "run.context_compaction_started" | "run.context_compaction_completed" | "run.context_compaction_failed" | "run.waiting_context" | "run.planning_started" | "run.plan_ready" | "run.waiting_permission" | "run.waiting_budget" | "run.step_started" | "run.step_completed" | "run.step_failed" | "run.synthesis_started" | "run.invocation_prepared" | "run.invocation_dispatched" | "run.invocation_retrying" | "run.response_recovering" | "run.invocation_completed" | "run.invocation_failed";
  timestamp: string;
  data: Record<string, unknown>;
}
export interface ContextSnapshot {
  id: string;
  run_id: string;
  round_no: number;
  policy_version: string;
  context_generation: number;
  global_instruction_revision: number;
  global_instruction_hash: string | null;
  context_window: number;
  input_target: number;
  output_reserve: number;
  token_method: string;
  estimated_tokens: number;
  stable_prefix_hash: string;
  dynamic_suffix_hash: string;
  retrieval_confidence: "high" | "medium" | "low" | "none";
  compression_applied: number;
  omitted_items: number;
  compaction: {
    status: "queued" | "running" | "valid" | "failed" | "interrupted" | "stale";
    input_tokens: number | null;
    output_tokens: number | null;
    estimated_cost: number | null;
    error_code: string | null;
    finished_at: string | null;
  } | null;
  run_usage: {
    input_tokens: number | null;
    output_tokens: number | null;
    cached_input_tokens: number | null;
    estimated_cost: number | null;
  } | null;
  items: ContextItem[];
}
export interface ContextItem {
  id: string;
  source_type: string;
  source_id: string | null;
  source_start?: number | null;
  source_end?: number | null;
  source_hash?: string | null;
  section: "stable" | "checkpoint" | "recent_history" | "retrieval" | "task_state" | "current";
  representation: "original" | "checkpoint" | "reference" | "raw_replay" | "metadata";
  trust: "system_policy" | "user_authored" | "user_confirmed_project_data" | "external_untrusted" | "model_inference" | "system_observation";
  estimated_tokens: number;
  included: number;
  inclusion_reason: string | null;
  exclusion_reason: string | null;
  metadata: Record<string, unknown>;
}
export interface ContextSourcePreview {
  item_id: string;
  source_type: string;
  source: Record<string, unknown>;
}
export interface CompactionTargetRequest {
  provider_profile_id: string;
  model_id: string;
}
export interface ContextCompactionState {
  id: string;
  conversation_id: string;
  run_id: string | null;
  status: "queued" | "running" | "valid" | "failed" | "interrupted" | "stale";
  error_code: string | null;
  created_at: string;
  finished_at: string | null;
}
export interface ProjectKnowledge {
  id: string;
  project_id: string;
  knowledge_type: "fact" | "decision" | "constraint" | "summary" | "artifact_reference";
  title: string;
  content: string;
  pinned: number;
  status: "published" | "archived";
  source_conversation_id: string | null;
  source_deleted: number;
}

export interface ConversationFilter {
  kind?: ConversationKind;
  chatGroupId?: string;
  projectId?: string;
}

export interface ProjectGrant {
  project_id: string;
  directory: string;
  active: boolean;
  suspended_at: string | null;
  root_generation: number;
}

export type ApprovalMode = "human" | "ai_review" | "open";
export type OperationRisk = "routine" | "medium" | "high" | "forbidden";
export interface WorkPermission {
  conversation_id: string;
  project_id: string;
  root_generation: number;
  mode: ApprovalMode;
}
export interface FileMutation {
  kind: "create_text" | "patch_text" | "overwrite_text" | "mkdir" | "copy" | "move" | "delete";
  path: string;
  source_path?: string | null;
  content?: string | null;
  old_text?: string | null;
  new_text?: string | null;
  replace_all?: boolean;
  expected_hash?: string | null;
  expected_after_hash?: string | null;
}
export interface PlannedMutation {
  operation_id: string;
  mutation: FileMutation;
  depends_on: string[];
  preconditions: Record<string, unknown>;
}
export interface ChangeSetPlan {
  plan_id: string;
  plan_hash: string;
  policy_version: number;
  root_generation: number;
  steps: PlannedMutation[];
  effective_risk: OperationRisk;
}
export interface ChangeSetApproval {
  approve: boolean;
  plan_id: string;
  plan_hash: string;
  approved_operation_ids: string[];
}
export interface SearchPage {
  matches: string[];
  inspected_files: number;
  next_cursor: string | null;
  complete: boolean;
  truncated: boolean;
  stop_reason: "files" | "entries" | "depth" | "bytes" | "deadline" | "cancelled" | "authorization_changed" | null;
}
export type SearchCompleteness = Pick<SearchPage, "complete" | "truncated" | "stop_reason" | "inspected_files">;
export interface ChangeSet {
  plan?: ChangeSetPlan | null;
  execution_id?: string | null;
  recovery_error_code?: string | null;
  recovery_affected_paths?: string[];
  id: string;
  operation_id: string;
  run_id: string;
  conversation_id: string;
  project_id: string;
  title: string;
  risk: OperationRisk;
  approval_mode: ApprovalMode;
  mutations: FileMutation[];
  status: "ready" | "reviewing" | "waiting_user" | "completed" | "undone" | "rejected" | "expired" | "cancelled_scope_changed" | "recovery_required" | "rollback_conflicted" | "rolled_back";
  expires_at_epoch_ms: number;
  recovery_point_id?: string | null;
  created_at?: string | null;
  undo_state: "available" | "blocked_by_newer_change" | "blocked_by_pending_change" | "expired_time_window" | "expired_depth_window" | "conflicted" | "already_undone" | "unavailable";
  undo_sequence?: number | null;
  undo_expires_at?: string | null;
  undo_block_reason?: string | null;
}
export interface TerminalResult {
  stdout: string; stderr: string; exit_code: number | null; duration_ms: number;
  timed_out: boolean; cancelled: boolean; stdout_truncated: boolean; stderr_truncated: boolean;
}
export interface ToolOperation {
  id: string;
  run_id: string;
  sequence: number;
  tool_name: "fs.list" | "fs.search" | "fs.read" | "fs.propose_changes" | "terminal.run";
  arguments?: Record<string, unknown>;
  result_summary?: string | null;
  status: "requested" | "validating" | "reviewing" | "waiting_user" | "executing" | "completed" | "rejected" | "failed" | "cancelled" | "interrupted";
  error_code: string | null;
  search_completeness?: SearchCompleteness | null;
}

export type TrashEntityType = "project" | "conversation";
export interface TrashItem {
  entity_type: TrashEntityType;
  id: string;
  title: string;
  deleted_at: string;
  expires_at: string;
  work_count: number;
  message_count: number;
  conversation_kind: ConversationKind | null;
  project_id: string | null;
}
export interface ProjectDeleteImpact {
  project_id: string;
  title: string;
  work_count: number;
  message_count: number;
}
