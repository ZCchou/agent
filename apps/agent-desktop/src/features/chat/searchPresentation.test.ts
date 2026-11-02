import { describe, expect, it } from "vitest";
import { searchPresentation } from "./searchPresentation";

describe("searchPresentation", () => {
  it("distinguishes a complete scan from a budget-limited scan", () => {
    expect(searchPresentation({ complete: true, truncated: false, stop_reason: null, inspected_files: 12 }))
      .toEqual({ state: "complete", reason: null, inspectedFiles: 12 });
    expect(searchPresentation({ complete: false, truncated: true, stop_reason: "bytes", inspected_files: 12 }))
      .toEqual({ state: "incomplete", reason: "bytes", inspectedFiles: 12 });
  });

  it("treats missing, contradictory, and forged scan metadata as unknown", () => {
    for (const value of [undefined, { complete: true },
      { complete: true, truncated: true, stop_reason: "bytes", inspected_files: 1 },
      { complete: true, truncated: false, stop_reason: "bytes", inspected_files: 1 },
      { complete: false, truncated: true, stop_reason: "invented", inspected_files: 1 },
      { complete: true, truncated: false, stop_reason: null, inspected_files: -1 },
      { complete: true, truncated: false, stop_reason: null, inspected_files: 10001 }]) {
      expect(searchPresentation(value)).toEqual({ state: "unknown", reason: null, inspectedFiles: null });
    }
  });

  it("copies only displayable audit fields", () => {
    expect(searchPresentation({ complete: false, truncated: true, stop_reason: "deadline", inspected_files: 4,
      matches: ["secret-path"], next_cursor: "secret-cursor" }))
      .toEqual({ state: "incomplete", reason: "deadline", inspectedFiles: 4 });
  });
});
