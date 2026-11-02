import { describe, expect, it } from "vitest";
import type { MessageAttachment } from "../../types/domain";
import { attachmentReferenceCandidates } from "./attachmentReferences";

function attachment(id: string, name: string, kind: "text" | "image" = "text"): MessageAttachment {
  return { id, name, kind, media_type: kind === "text" ? "text/plain" : "image/png",
    byte_size: 20, sha256: id, width: null, height: null };
}

describe("visible historical attachment candidates", () => {
  it("selects a uniquely named text attachment and leaves other history out", () => {
    const notes = attachment("notes", "notes.txt");
    const result = attachmentReferenceCandidates("请根据 notes.txt 解释", [
      { attachments: [notes, attachment("budget", "budget.csv")] },
    ]);
    expect(result).toEqual({ candidates: [notes], automatic: true });
  });

  it("does not choose among different attachments with the same name", () => {
    const a = attachment("a", "notes.txt");
    const b = attachment("b", "notes.txt");
    expect(attachmentReferenceCandidates("Read notes.txt", [
      { attachments: [a] }, { attachments: [b] },
    ])).toEqual({ candidates: [a, b], automatic: false });
  });

  it("uses the most recent attachment message for an explicit previous-file reference", () => {
    const recent = attachment("recent", "current.txt");
    const history = [{ attachments: [attachment("old", "old.txt")] }, { attachments: [recent] }, {}];
    expect(attachmentReferenceCandidates("刚才这个文件的结论是什么？", history))
      .toEqual({ candidates: [recent], automatic: true });
    expect(attachmentReferenceCandidates("Summarize the previous file", history).candidates).toEqual([recent]);
  });

  it("keeps a recent multi-file reference ambiguous and images explicitly selected", () => {
    const text = attachment("text", "data.txt");
    const image = attachment("image", "chart.png", "image");
    expect(attachmentReferenceCandidates("刚才这个文件", [{ attachments: [text, image] }]))
      .toEqual({ candidates: [text, image], automatic: false });
    expect(attachmentReferenceCandidates("请看 chart.png", [{ attachments: [image] }]))
      .toEqual({ candidates: [image], automatic: false });
  });

  it("does not infer a reference from generic follow-up text or a filename substring", () => {
    const history = [{ attachments: [attachment("a", "a.txt")] }];
    expect(attachmentReferenceCandidates("Continue", history).candidates).toEqual([]);
    expect(attachmentReferenceCandidates("Read data.txt", history).candidates).toEqual([]);
  });

  it("coalesces a re-used source into the latest in-conversation attachment ID", () => {
    const original = attachment("original", "notes.txt");
    const clone = { ...attachment("clone", "notes.txt"), source_attachment_id: "original" };
    expect(attachmentReferenceCandidates("notes.txt", [{ attachments: [original] }, { attachments: [clone] }]))
      .toEqual({ candidates: [clone], automatic: true });
  });
});
