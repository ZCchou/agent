import { describe, expect, it } from "vitest";
import { classifyImage, classifyLink, markdownUrlTransform, parseProjectDirectoryTarget, parseProjectFileTarget, remoteImageHost } from "./markdownPolicy";

describe("markdown resource policy", () => {
  it("allows only absolute web links without embedded credentials", () => {
    expect(classifyLink("https://example.com/docs?q=1")).toBe("external");
    expect(classifyLink("http://example.com")).toBe("external");
    expect(classifyLink("javascript:alert(1)")).toBe("blocked");
    expect(classifyLink("file:///c:/secret.txt")).toBe("blocked");
    expect(classifyLink("/relative/path")).toBe("blocked");
    expect(classifyLink("https://user:secret@example.com/image")).toBe("blocked");
  });

  it("allows only credential-free HTTPS images", () => {
    expect(classifyImage("https://cdn.example.com/image.png")).toBe("remote_https");
    expect(classifyImage("http://cdn.example.com/image.png")).toBe("blocked");
    expect(classifyImage("data:image/png;base64,abc")).toBe("blocked");
    expect(classifyImage("https://user:secret@cdn.example.com/image.png")).toBe("blocked");
    expect(remoteImageHost("https://cdn.example.com/image.png")).toBe("cdn.example.com");
  });

  it("removes blocked URLs before React creates a resource element", () => {
    expect(markdownUrlTransform("https://cdn.example.com/a.png", "src")).toBe("https://cdn.example.com/a.png");
    expect(markdownUrlTransform("http://cdn.example.com/a.png", "src")).toBe("");
    expect(markdownUrlTransform("javascript:alert(1)", "href")).toBe("");
  });

  it("accepts only scoped project-relative file targets", () => {
    expect(parseProjectFileTarget("src/main.ts#L20-L35")).toEqual({
      path: "src/main.ts", lineStart: 20, lineEnd: 35,
    });
    expect(parseProjectFileTarget("README.md")).toEqual({ path: "README.md", lineStart: null, lineEnd: null });
    expect(parseProjectFileTarget("../secret.txt")).toBeNull();
    expect(parseProjectFileTarget("C:/secret.txt")).toBeNull();
    expect(parseProjectFileTarget("file:///secret.txt")).toBeNull();
    expect(parseProjectFileTarget("src/main.ts?raw=1")).toBeNull();
  });

  it("accepts only scoped project-relative directory targets", () => {
    expect(parseProjectDirectoryTarget("project-dir:src/components")).toEqual({ path: "src/components" });
    expect(parseProjectDirectoryTarget("project-dir:.")).toEqual({ path: "." });
    expect(parseProjectDirectoryTarget("project-dir:../secret")).toBeNull();
    expect(parseProjectDirectoryTarget("project-dir:C:/secret")).toBeNull();
    expect(parseProjectDirectoryTarget("project-dir:src\\components")).toBeNull();
    expect(parseProjectDirectoryTarget("project-dir:src/components?raw=1")).toBeNull();
    expect(markdownUrlTransform("project-dir:src/components", "href")).toBe("project-dir:src/components");
  });
});
