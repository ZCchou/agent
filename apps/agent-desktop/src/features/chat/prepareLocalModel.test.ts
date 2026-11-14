import { expect, it, vi } from "vitest";
import { prepareLocalModel, withPreparedLocalModel } from "./prepareLocalModel";

it("waits for readiness and releases the reservation after sending", async () => {
  const release = vi.fn(async () => undefined);
  const prepare = vi.fn().mockResolvedValueOnce({ preparation_id: "lease", state: "loading" })
    .mockResolvedValue({ preparation_id: "lease", state: "ready" });
  const finish = await prepareLocalModel({ prepare, release }, "model", () => false, () => undefined, async () => undefined);
  expect(prepare).toHaveBeenCalledTimes(2);
  expect(release).not.toHaveBeenCalled();
  await finish();
  expect(release).toHaveBeenCalledWith("lease");
});

it("reports the load task and verified reservation through readiness", async () => {
  const loading = { preparation_id: "lease", state: "loading" as const, execution_task_id: "load-root" };
  const ready = { ...loading, state: "ready" as const };
  const prepare = vi.fn().mockResolvedValueOnce(loading).mockResolvedValueOnce(ready);
  const progress = vi.fn();
  const finish = await prepareLocalModel({ prepare, release: vi.fn() }, "model", () => false, progress, async () => undefined);
  expect(progress.mock.calls.map((call) => call[2])).toEqual([loading, ready]);
  await finish();
});

it("cancellation releases the waiter and prevents sending", async () => {
  let cancelled = false;
  const release = vi.fn(async () => undefined);
  const prepare = vi.fn(async () => { cancelled = true; return { preparation_id: "lease", state: "loading" as const }; });
  await expect(prepareLocalModel({ prepare, release }, "model", () => cancelled, () => undefined, async () => undefined)).rejects.toThrow("cancelled");
  expect(release).toHaveBeenCalledWith("lease");
});

it("reports the original model load failure and releases the reservation", async () => {
  const release = vi.fn(async () => undefined);
  const prepare = vi.fn(async () => ({ preparation_id: "lease", state: "failed" as const, job: { id: "j", progress: 0, error: { code: "runtime_load_timeout", message: "加载超时" } } }));
  await expect(prepareLocalModel({ prepare, release }, "model", () => false, () => undefined)).rejects.toThrow("加载超时");
  expect(release).toHaveBeenCalledWith("lease");
});


it("prepares a cold model for retry and releases it when retry fails", async () => {
  const order: string[] = [];
  const repository = {
    prepare: async () => { order.push("ready"); return { preparation_id: "lease", state: "ready" as const }; },
    release: async () => { order.push("release"); },
  };
  await expect(withPreparedLocalModel(repository, "managed:model", "model", () => false, () => undefined, async () => {
    order.push("retry"); throw new Error("rejected");
  })).rejects.toThrow("rejected");
  expect(order).toEqual(["ready", "retry", "release"]);
});
