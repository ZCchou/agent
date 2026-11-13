import { beforeEach, describe, expect, it, vi } from "vitest";
vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn() }));
import { invoke } from "@tauri-apps/api/core";
import { IntentTaskStore, type IntentTask } from "./intentTasks";
const task = (patch: Partial<IntentTask> = {}): IntentTask => ({ id: "i1", scope: "c1", revision: 1,
  status: "waiting_user", attempts: 1, elapsed_seconds: 5, remaining_seconds: 80,
  budget_remaining_seconds: 260, timeout_seconds: 90, budget_seconds: 270, can_extend: true, can_retry: false,
  request: {}, clarification: { summary: "Choose", questions: [{ id: "q1", question: "Which?", options: ["A", "B"] }] },
  error: null, result: null, ...patch });
describe("persistent intent tasks", () => {
  beforeEach(() => { vi.mocked(invoke).mockReset(); });
  it("keeps acceptance pending until the same task completes", async () => {
    const store = new IntentTaskStore();
    let settled = false;
    const pending = store.wait(task()).then(() => { settled = true; });
    await Promise.resolve();
    expect(settled).toBe(false);
    store.update(task({ revision: 2, status: "completed", result: { status: "preflight_rejected", rejection: {} } as never }));
    await pending;
    expect(settled).toBe(true);
    store.dispose();
  });
  it("keeps resolved acceptance unconsumed until the scope handler acknowledges it", async () => {
    const store = new IntentTaskStore();
    const result = { status: "accepted", run: { id: "run1" }, conversation: { id: "accepted-chat" } } as never;
    const pending = store.wait(task());
    store.update(task({ status: "completed", result }));
    await pending;
    expect(store.isConsumed("i1")).toBe(false);
    expect(store.hasAcceptanceOwner("i1")).toBe(true);
    store.acknowledge("other", result);
    expect(store.isConsumed("i1")).toBe(false);
    store.acknowledge("c1", result);
    expect(store.isConsumed("i1")).toBe(true);
    expect(store.hasAcceptanceOwner("i1")).toBe(false);
    store.dispose();
  });
  it.each(["before registration", "while pending"])("releases stale ownership %s without consuming acceptance", async (when) => {
    const store = new IntentTaskStore();
    store.begin("c1");
    if (when === "before registration") store.releaseScope("c1");
    const pending = store.wait(task());
    if (when === "while pending") store.releaseScope("c1");
    const result = { status: "accepted", run: { id: "run1" }, conversation: { id: "accepted-chat" } } as never;
    store.update(task({ status: "completed", result }));
    expect(await pending).toBe(result);
    expect(store.hasAcceptanceOwner("i1")).toBe(false);
    expect(store.isConsumed("i1")).toBe(false);
    store.acknowledge("c1", result);
    expect(store.isConsumed("i1")).toBe(true);
    store.dispose();
  });
  it("guards older revisions and duplicate replies", async () => {
    const store = new IntentTaskStore();
    store.update(task({ revision: 3 }));
    store.update(task({ revision: 2, scope: "other" }));
    expect(store.get("c1")?.revision).toBe(3);
    let finish!: (value: unknown) => void;
    vi.mocked(invoke).mockImplementation(() => new Promise((resolve) => { finish = resolve; }) as never);
    const first = store.act("c1", "respond", { q1: "A" });
    await store.act("c1", "respond", { q1: "B" });
    expect(invoke).toHaveBeenCalledTimes(1);
    expect(invoke).toHaveBeenCalledWith("intent_request", { action: "respond", params: { task_id: "i1", revision: 3, answers: { q1: "A" } } });
    finish({ task: task({ revision: 4, status: "identifying" }) });
    await first;
    store.dispose();
  });
  it("restores only its requested scope", async () => {
    const store = new IntentTaskStore();
    vi.mocked(invoke).mockResolvedValue({ tasks: [task(), task({ id: "foreign", scope: "other" })] });
    await store.restore("c1");
    expect(store.get("other")).toBeUndefined();
    expect(store.get("c1")?.id).toBe("i1");
    store.dispose();
  });
  it("restores the newest task only and keeps an intervening new send", async () => {
    const store = new IntentTaskStore();
    vi.mocked(invoke).mockResolvedValue({ tasks: [task({ id: "newest" }), task({ id: "oldest" })] });
    await store.restore("c1");
    expect(store.get("c1")?.id).toBe("newest");
    let finish!: (value: unknown) => void;
    vi.mocked(invoke).mockImplementation(() => new Promise((resolve) => { finish = resolve; }) as never);
    const restoring = store.restore("c1");
    store.update(task({ id: "new-send" }));
    finish({ tasks: [task({ id: "stale-restore" })] });
    await restoring;
    expect(store.get("c1")?.id).toBe("new-send");
    store.dispose();
  });
});
