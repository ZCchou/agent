import { describe, expect, it } from "vitest";
import {
  clampSidebarWidth,
  DEFAULT_SIDEBAR_WIDTH,
  MAX_SIDEBAR_WIDTH,
  MIN_SIDEBAR_WIDTH,
} from "./sidebarSizing";

describe("sidebar sizing", () => {
  it("keeps normal widths and clamps hard limits", () => {
    expect(clampSidebarWidth(DEFAULT_SIDEBAR_WIDTH, 1200)).toBe(DEFAULT_SIDEBAR_WIDTH);
    expect(clampSidebarWidth(100, 1200)).toBe(MIN_SIDEBAR_WIDTH);
    expect(clampSidebarWidth(900, 1400)).toBe(MAX_SIDEBAR_WIDTH);
  });

  it("leaves enough room for the workspace in a narrow window", () => {
    expect(clampSidebarWidth(480, 700)).toBe(336);
  });
});
