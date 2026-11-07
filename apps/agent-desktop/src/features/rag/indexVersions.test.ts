import { expect, it } from "vitest";
import type { RagDocument } from "../../types/rag";
import { resolveIndexVersions } from "./indexVersions";

function version(id: string, versionId: string, status = "confirmed"): RagDocument {
  return { id, version_id: versionId, title: id, revision: versionId, status, warnings: [], chunk_count: 1 };
}
it("defaults to a sole confirmed revision and requires a decision for duplicate revisions", () => {
  const result = resolveIndexVersions([version("a", "a1"), version("b", "b1"), version("b", "b2"), version("c", "c1", "pending")], {});
  expect(result.versionIds).toEqual(["a1"]);
  expect(result.unresolvedDocumentIds).toEqual(["b"]);
});
it("selects at most one revision per document and supports explicit exclusion", () => {
  const documents = [version("a", "a1"), version("b", "b1"), version("b", "b2")];
  expect(resolveIndexVersions(documents, { a: null, b: "b2" })).toMatchObject({ versionIds: ["b2"], unresolvedDocumentIds: [] });
  expect(resolveIndexVersions(documents, { b: null })).toMatchObject({ versionIds: ["a1"], unresolvedDocumentIds: [] });
});
it("requires review again when an explicitly selected revision is withdrawn", () => {
  const result = resolveIndexVersions([version("a", "a1", "withdrawn"), version("a", "a2")], { a: "a1" });
  expect(result.versionIds).toEqual([]);
  expect(result.unresolvedDocumentIds).toEqual(["a"]);
});
