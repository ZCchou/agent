import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { initializeInterfaceStyle, readInterfaceStyle, saveInterfaceStyle } from "./interfaceStyle";

describe("desktop interface style", () => {
  const dataset: Record<string, string> = {};
  const values = new Map<string, string>();
  beforeEach(() => {
    values.clear();
    delete dataset.interfaceStyle;
    vi.stubGlobal("document", { documentElement: { dataset } });
    vi.stubGlobal("localStorage", {
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => values.set(key, value),
    });
  });
  afterEach(() => vi.unstubAllGlobals());

  it("uses ragent for a new profile or an unknown preference", () => {
    expect(readInterfaceStyle()).toBe("ragent");
    values.set("agent.interfaceStyle", "unknown");
    initializeInterfaceStyle();
    expect(dataset.interfaceStyle).toBe("ragent");
  });
  it("restores each selection at startup", () => {
    for (const style of ["classic", "ragent"] as const) {
      saveInterfaceStyle(style);
      expect(dataset.interfaceStyle).toBe(style);
      delete dataset.interfaceStyle;
      initializeInterfaceStyle();
      expect(dataset.interfaceStyle).toBe(style);
    }
  });
  it("keeps the active style when storage rejects a write", () => {
    initializeInterfaceStyle();
    vi.stubGlobal("localStorage", {
      setItem: () => { throw Error("Storage unavailable"); },
    });
    expect(() => saveInterfaceStyle("classic")).toThrow();
    expect(dataset.interfaceStyle).toBe("ragent");
  });
});
