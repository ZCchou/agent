import { describe, expect, it } from "vitest";
import { defaultExecutionWaitPolicy, executionWaitNumber } from "./executionWaitPolicy";

describe("execution wait settings", () => {
  it("uses adaptive unbounded waiting with two recoveries by default", () => {
    expect(defaultExecutionWaitPolicy()).toEqual({
      mode: "adaptive", long_running_notice_seconds: 90, no_progress_probe_seconds: 300,
      max_total_seconds: null, max_auto_recoveries: 2,
    });
  });
  it("returns independent defaults for each edit", () => {
    const first = defaultExecutionWaitPolicy();
    first.max_auto_recoveries = 0;
    expect(defaultExecutionWaitPolicy().max_auto_recoveries).toBe(2);
  });
  it("accepts bounded integer settings and rejects incomplete or fractional inputs", () => {
    expect(executionWaitNumber("60", 60, 3600)).toBe(60);
    expect(executionWaitNumber("3600", 60, 3600)).toBe(3600);
    for (const value of ["", " ", "59", "3601", "60.5", "NaN", "Infinity"])
      expect(executionWaitNumber(value, 60, 3600)).toBeNull();
  });
});
