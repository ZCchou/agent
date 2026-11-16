import { renderToStaticMarkup } from "react-dom/server";
import { expect, it, vi } from "vitest";
vi.mock("react-i18next", () => ({ useTranslation: () => ({ t: (key: string) => key }) }));
vi.mock("../abilities/SkillPicker", () => ({ SkillPicker: () => null }));
import { ChatComposer } from "./ChatComposer";

it("renders uploaded DOCX files without assigning a purpose selector", () => {
  const markup = renderToStaticMarkup(<ChatComposer value="" disabled={false}
    onChange={() => {}} onSend={() => {}}
    attachments={[{ id: "a", kind: "text", name: "template.docx", media_type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
      byte_size: 10, sha256: "a", width: null, height: null }]}
    rejectedAttachments={[]} previews={{}} attachmentBusy={false} attachmentError={null}
    onAttach={() => {}} onPasteImages={() => {}} onRemoveAttachment={() => {}} onRemoveRejectedAttachment={() => {}}
    {...{ onAttachmentPurpose: () => {} }} />);
  expect(markup).toContain("template.docx");
  expect(markup).not.toContain("<select");
});
