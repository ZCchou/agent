import { describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { ReviewOperationStream } from "./ReviewOperationStream";
import { AssistantRunMessage } from "./AssistantRunMessage";
import type { MessageAttachment, ToolOperation } from "../../types/domain";
vi.mock("react-i18next", () => ({useTranslation: () => ({ t: (key:string) => key })}));
describe("public activity before the first document checkpoint", () => {
  it("shows inspect and active review without counts or invented hits", () => {
    const ops = [
      {id:"1",run_id:"r",tool_name:"document.inspect_template",status:"completed",sequence:1,arguments:{attachment_id:"a"}},
      {id:"2",run_id:"r",tool_name:"document.review_targets",status:"executing",sequence:2},
      {id:"3",run_id:"another",tool_name:"rag.search",status:"completed",sequence:3},
    ] as unknown as ToolOperation[];
    const html=renderToStaticMarkup(<ReviewOperationStream operations={ops} runId="r" attachments={[{id:"a",name:"短时工作低温试验步骤.docx"} as MessageAttachment]}/>);
    for (const value of ["短时工作低温试验步骤.docx","publicOps.inspect","publicOps.review","operationStatus.executing"]) expect(html).toContain(value);
    expect(html).not.toContain("publicOps.search");
    expect(html).not.toContain("<details");
  });
  it("shows an executed discovery or write operation with its actual status", () => {
    const operations = [
      { id:"op1", run_id:"r", tool_name:"tools.discover", status:"completed", sequence:1 },
      { id:"op2", run_id:"r", tool_name:"fs.propose_changes", status:"executing", sequence:2 },
    ] as unknown as ToolOperation[];
    const html = renderToStaticMarkup(<ReviewOperationStream operations={operations} runId="r" attachments={[]} />);
    for (const value of ["tools.discover", "operationStatus.completed", "fs.propose_changes", "operationStatus.executing"])
      expect(html).toContain(value);
  });
  it("renders an arriving public answer without an expansion click", () => {
    expect(renderToStaticMarkup(<AssistantRunMessage content="依据仍需核验。" status="generating"/>)).toContain("依据仍需核验。");
  });
});