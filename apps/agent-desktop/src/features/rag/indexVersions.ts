import type { RagDocument } from "../../types/rag";

export type IndexVersionChoices = Record<string, string | null>;

export function resolveIndexVersions(documents: RagDocument[], choices: IndexVersionChoices) {
  const groups = new Map<string, RagDocument[]>();
  for (const document of documents) {
    if (document.status !== "confirmed") continue;
    const versions = groups.get(document.id) ?? [];
    versions.push(document);
    groups.set(document.id, versions);
  }
  const versionIds: string[] = [];
  const unresolvedDocumentIds: string[] = [];
  for (const [documentId, versions] of groups) {
    const choice = choices[documentId];
    if (choice === null) continue;
    if (choice && versions.some((version) => version.version_id === choice)) {
      versionIds.push(choice);
    } else if (choice === undefined && versions.length === 1) {
      versionIds.push(versions[0].version_id);
    } else {
      unresolvedDocumentIds.push(documentId);
    }
  }
  return { groups, versionIds, unresolvedDocumentIds };
}
