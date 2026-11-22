import { describe, it, expect, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { DocumentProgressPanel } from "./DocumentProgressPanel";
import { retainDocumentProgress, type ExecutionTaskSnapshot } from "../../services/tauri/executionTasks";
vi.mock("react-i18next", () => ({ useTranslation: () => ({ t: (key: string, values?: Record<string, unknown>) =>
  key + (values?.total ? " " + values.completed + "/" + values.total : "") }) }));
const start: ExecutionTaskSnapshot = { id: "t", run_id: "r", revision: 1, attempt: 1, status: "running",
  auto_recoveries: 0, created_at: 0, updated_at: 0, document_progress: { phase: "scope_selection",
    completed: 859, total: 2409, recent: [{target_id:"p-4", decision:"exclude", reason:"属于示例段落", excerpt:"示例正文"}] }};
describe("document progress replay", () => {
  it("retains the last saved scope decision during a model heartbeat", () => {
    const {document_progress: _, ...base} = start;
    const heartbeat = {...base, stage:"model_output", current_stage:{completed:null,total:null}};
    const html=renderToStaticMarkup(<DocumentProgressPanel progress={retainDocumentProgress(start,heartbeat).document_progress}/>);
    for(const label of ["859/2409","p-4","属于示例段落","示例正文"]) expect(html).toContain(label);
    expect(retainDocumentProgress(start,{...heartbeat, attempt:2}).document_progress).toBeUndefined();
    expect(retainDocumentProgress(start,{...heartbeat, document_progress:null}).document_progress).toBeNull();
  });
  it("shows the actual reason for a corrective call", () => {
    const html=renderToStaticMarkup(<DocumentProgressPanel progress={{phase:"document_review",event:"correcting",
      current:[{target_id:"p-7"}],correction:{attempt:2,reason:"引用未支持该参数"}}}/>);
    for(const value of ["p-7","publicOps.events.correcting","publicOps.correctionAttempt","引用未支持该参数"])
      expect(html).toContain(value);
  });
  it("renders the current target and confirmed search hit count before a decision exists", () => {
    const html=renderToStaticMarkup(<DocumentProgressPanel progress={{phase:"document_review",event:"search_results",
      current:[{target_id:"p-2",excerpt:"短时工作低温暴露"}],query:"低温运行试验条件",hit_count:0}}/>);
    for(const text of ["publicOps.events.search_results","p-2","短时工作低温暴露","低温运行试验条件","publicOps.hits"]) expect(html).toContain(text);
    expect(html).not.toContain("docProgress.finding");
  });
  it("shows an intermediate finding with source references outside details", () => {
    const html=renderToStaticMarkup(<DocumentProgressPanel progress={{phase:"document_review",completed:1,total:4,
      search_count:2, latest_review:{target_id:"t-30-r2-c2",summary:"压力参数需要核对工况。",citations:["项目依据 · 第42页"]}}}/>);
    for(const text of ["1/4","t-30-r2-c2","压力参数需要核对工况。","项目依据 · 第42页"]) expect(html).toContain(text);
    expect(html).not.toContain("<details");
  });
});