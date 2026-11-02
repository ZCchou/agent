import { describe, expect, it } from "vitest";
import { webSearchMenuOptions } from "./webSearchPolicy";

describe("web search menu policy", () => {
  it("does not show an inherit option in ordinary chats", () => {
    expect(webSearchMenuOptions(false)).toEqual(["off", "auto", "once"]);
  });

  it("keeps inherit available for project work", () => {
    expect(webSearchMenuOptions(true)).toEqual(["inherit", "off", "auto", "once"]);
  });
});
