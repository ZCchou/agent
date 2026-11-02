import type { MessageAttachment } from "../../types/domain";

export interface AttachmentCandidates {
  candidates: MessageAttachment[];
  automatic: boolean;
}

// This only proposes visible selections. Core must resolve every ID again in
// the current conversation and recheck integrity, permissions and capabilities.
export function attachmentReferenceCandidates(
  input: string,
  messages: ReadonlyArray<{ attachments?: MessageAttachment[] }>,
): AttachmentCandidates {
  const text = input.toLowerCase();
  const attachments = messages.flatMap((message) => message.attachments ?? []);
  const byId = new Map(attachments.map((attachment) => [attachment.id, attachment]));
  function sourceId(attachment: MessageAttachment): string {
    let id = attachment.id;
    const seen = new Set<string>();
    while (!seen.has(id)) {
      seen.add(id);
      const source = byId.get(id)?.source_attachment_id;
      if (!source) break;
      id = source;
    }
    return id;
  }
  function uniqueSources(values: MessageAttachment[]): MessageAttachment[] {
    return [...new Map(values.map((attachment) => [sourceId(attachment), attachment])).values()];
  }
  const named = uniqueSources(attachments).filter((attachment) => {
    const name = attachment.name.toLowerCase();
    if (!name || (name.length < 3 && text.trim() !== name)) return false;
    let start = text.indexOf(name);
    while (start >= 0) {
      const before = text[start - 1] ?? "";
      const after = text[start + name.length] ?? "";
      if (!/[a-z0-9_.-]/.test(before) && !/[a-z0-9_.-]/.test(after)) return true;
      start = text.indexOf(name, start + 1);
    }
    return false;
  });
  const recentReference = /(?:刚才|上次|之前)(?:上传的|的|这[个份张])?(?:文件|附件|图片)|\b(?:the\s+)?(?:previous|last|earlier)\s+(?:file|attachment|image)\b/i.test(input);
  const latest = [...messages].reverse().find((message) => message.attachments?.length)?.attachments ?? [];
  const candidates = named.length ? named : recentReference ? uniqueSources(latest) : [];
  return { candidates, automatic: candidates.length === 1 && candidates[0].kind === "text" };
}
