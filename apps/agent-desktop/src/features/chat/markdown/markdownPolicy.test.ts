import { describe, expect, it } from "vitest";
import { classifyImage, classifyLink, markdownUrlTransform, parseProjectDirectoryTarget, parseProjectFileTarget, parseRagEvidenceTarget, remoteImageHost } from "./markdownPolicy";

describe("markdown resource policy", () => {
  it("retains a persisted evidence UUID only as a link target", () => {
    const target = "rag-evidence:rag-12345678-1234-4abc-8def-1234567890ab";
    expect(parseRagEvidenceTarget(target)).toBe("rag-12345678-1234-4abc-8def-1234567890ab");
    expect(markdownUrlTransform(target, "href")).toBe(target);
    expect(markdownUrlTransform(target, "src")).toBe("");
    expect(classifyLink(target)).toBe("blocked");
  });

  it.each([
    "rag-evidence:rag-test", "rag-evidence:rag-12345678-1234-4abc-8def-1234567890ab?x=1",
    "rag-evidence:rag-12345678-1234-4abc-8def-1234567890ab/", "rag-evidence:rag-12345678-1234-4abc-8def-1234567890ab#L1",
    " rag-evidence:rag-12345678-1234-4abc-8def-1234567890ab", "rag-evidence:rag-12345678-1234-4abc-8def-1234567890ab\n",
    "rag-evidence:rag-12345678-1234-4abc-8def-1234567890ab\u0000", "rag-evidence:rag-12345678-1234-4abc-8def-1234567890a%62",
    "\u0000rag-evidence:rag-12345678-1234-4abc-8def-1234567890ab",
  ])("rejects malformed evidence target %j", (target) => {
    expect(parseRagEvidenceTarget(target)).toBeNull();
    expect(markdownUrlTransform(target, "href")).toBe("");
  });
  it("allows only absolute web links without embedded credentials", () => {
    expect(classifyLink("https://example.com/docs?q=1")).toBe("external");
    expect(classifyLink("http://example.com")).toBe("external");
    expect(classifyLink("javascript:alert(1)")).toBe("blocked");
    expect(classifyLink("file:///c:/secret.txt")).toBe("blocked");
    expect(classifyLink("/relative/path")).toBe("blocked");
    expect(classifyLink("https://user:secret@example.com/image")).toBe("blocked");
  });

  it("blocks remote images and allows local bitmap data", () => {
    expect(classifyImage("https://cdn.example.com/image.png")).toBe("blocked");
    expect(classifyImage("http://cdn.example.com/image.png")).toBe("blocked");
    expect(classifyImage("data:image/png;base64,abc")).toBe("local");
    expect(classifyImage("https://user:secret@cdn.example.com/image.png")).toBe("blocked");
    expect(remoteImageHost("https://cdn.example.com/image.png")).toBeNull();
  });

  it("removes blocked URLs before React creates a resource element", () => {
    expect(markdownUrlTransform("https://cdn.example.com/a.png", "src")).toBe("");
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
