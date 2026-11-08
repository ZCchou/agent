import { expect, it } from "vitest";
import { classifyImage, markdownUrlTransform } from "./markdownPolicy";
it("blocks remote images before rendering while allowing local bitmap transports", () => {
  expect(classifyImage("https://remote.test/tracker.png")).toBe("blocked");
  expect(markdownUrlTransform("https://remote.test/tracker.png", "src")).toBe("");
  expect(markdownUrlTransform("data:image/png;base64,YQ==", "src")).toBe("data:image/png;base64,YQ==");
  expect(classifyImage("blob:http://tauri.localhost/id")).toBe("local");
});
