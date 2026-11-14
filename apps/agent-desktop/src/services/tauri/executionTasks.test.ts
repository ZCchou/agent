import { beforeEach, expect, it, vi } from "vitest";
vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn() }));
import { invoke } from "@tauri-apps/api/core";
import { acceptExecutionSnapshot, controlExecutionTask, type ExecutionTaskSnapshot } from "./executionTasks";

const task = (patch: Partial<ExecutionTaskSnapshot> = {}): ExecutionTaskSnapshot => ({
  id: "root-1", revision: 1, attempt: 2, sequence: 10, status: "running", run_id: "run-1",
  intent_task_id: "intent-1", stage: "reranking", auto_recoveries: 0, created_at: 100, updated_at: 120,
  ...patch,
});
beforeEach(() => { vi.mocked(invoke).mockReset(); });
it("rejects another owner and older revision, attempt or sequence", () => {
  const current = task();
  const owner = { kind: "run" as const, id: "run-1" };
  expect(acceptExecutionSnapshot(current, task({ run_id: "run-2" }), owner)).toBe(false);
  expect(acceptExecutionSnapshot(current, task({ revision: 0, sequence: 100 }), owner)).toBe(false);
  expect(acceptExecutionSnapshot(current, task({ attempt: 1, sequence: 100 }), owner)).toBe(false);
  expect(acceptExecutionSnapshot(current, task({ sequence: 9 }), owner)).toBe(false);
  expect(acceptExecutionSnapshot(current, task({ sequence: 11 }), owner)).toBe(true);
});
it("uses one idempotency key for concurrent duplicate controls", async () => {
  let finish!: (value: unknown) => void;
  vi.mocked(invoke).mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
  const first = controlExecutionTask(task(), "retry");
  const duplicate = controlExecutionTask(task(), "retry");
  expect(invoke).toHaveBeenCalledTimes(1);
  finish({ task: task({ revision: 2 }) });
  await expect(first).resolves.toMatchObject({ revision: 2 });
  await expect(duplicate).resolves.toMatchObject({ revision: 2 });
});
it("queries current status after an uncertain control response", async () => {
  vi.mocked(invoke).mockRejectedValueOnce(new Error("communication timeout"))
    .mockResolvedValueOnce({ task: task({ status: "cancelled", revision: 2 }) });
  await expect(controlExecutionTask(task(), "cancel")).resolves.toMatchObject({ status: "cancelled" });
  expect(vi.mocked(invoke).mock.calls[1][1]).toMatchObject({ action: "status", params: { task_id: "root-1" } });
});
