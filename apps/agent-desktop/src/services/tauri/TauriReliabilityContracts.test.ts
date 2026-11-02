import { beforeEach, expect, it, vi } from "vitest";
import fixture from "../../../../../contracts/reliability-repair-fixtures.json";
import type { ChangeSetPlan } from "../../types/domain";
import { changeSetSelection } from "../../features/chat/changeSetSelection";
import { safeSearchCompleteness, searchPresentation } from "../../features/chat/searchPresentation";

vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn() }));
vi.mock("@tauri-apps/api/event", () => ({ listen: vi.fn() }));
import { invoke } from "@tauri-apps/api/core";
import { TauriModelRunRepository } from "./TauriModelRunRepository";
import { TauriContextRepository } from "./TauriContextRepository";
import { TauriChangeSetRepository } from "./TauriChangeSetRepository";

beforeEach(() => vi.mocked(invoke).mockReset());

it("preserves shared identities and code-point ranges while stripping client authority", async () => {
  vi.mocked(invoke).mockResolvedValueOnce({ run: { id: "run-fixture" } }).mockResolvedValueOnce({ compaction: null });
  await new TauriModelRunRepository().start({
    conversationId: "conversation-fixture", content: "", providerProfileId: "allowed-local", modelId: "larger-context",
    attachmentReferences: fixture.attachment_references.map((ref) => ({ ...ref, storage_key: "forged" })),
  });
  expect(invoke).toHaveBeenLastCalledWith("start_model_run", { request: expect.objectContaining({
    attachment_references: fixture.attachment_references,
  }) });
  await new TauriContextRepository().resumeCompaction("conversation-fixture", {
    ...fixture.compaction_target, ...{ context_window: 999999 },
  });
  expect(invoke).toHaveBeenLastCalledWith("resume_context_compaction", {
    conversationId: "conversation-fixture", target: fixture.compaction_target,
  });
});

it("retains the shared plan and recovery fields and binds the approval envelope", async () => {
  vi.mocked(invoke).mockResolvedValueOnce([fixture.change_set]).mockResolvedValueOnce(undefined)
    .mockResolvedValueOnce(fixture.change_set);
  const repository = new TauriChangeSetRepository();
  expect((await repository.list())[0]).toEqual(fixture.change_set);
  const selection = changeSetSelection(fixture.change_set.plan as ChangeSetPlan)!;
  expect(selection.approved).toEqual(fixture.approval.approved_operation_ids);
  expect(selection.required).toEqual(["mkdir-docs"]);
  await repository.resolve(fixture.change_set.id, {
    approve: fixture.approval.approve, planId: fixture.approval.plan_id,
    planHash: fixture.approval.plan_hash, approvedOperationIds: selection.approved,
  });
  expect(invoke).toHaveBeenLastCalledWith("resolve_agent_change_set", {
    changeSetId: fixture.change_set.id, approve: true, planId: fixture.approval.plan_id,
    planHash: fixture.approval.plan_hash, approvedOperationIds: fixture.approval.approved_operation_ids,
  });
  expect(await repository.recover(fixture.change_set.id)).toEqual(fixture.change_set);
  expect(invoke).toHaveBeenLastCalledWith("recover_agent_change_set", { changeSetId: fixture.change_set.id });
});

it("interprets shared search completeness independently of matches and paging", () => {
  expect(fixture.search_pages.map(searchPresentation)).toEqual([
    { state: "incomplete", reason: "files", inspectedFiles: 10000 },
    { state: "complete", reason: null, inspectedFiles: 2 },
  ]);
  expect(Object.keys(safeSearchCompleteness(fixture.search_pages[0])!).sort())
    .toEqual(["complete", "inspected_files", "stop_reason", "truncated"]);
  expect(searchPresentation({ matches: [] }).state).toBe("unknown");
});
