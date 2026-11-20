import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import type { ToolOperation } from "../../types/domain";
import { RunActivityGroup } from "./RunActivityGroup";

vi.mock("react-i18next", () => ({ useTranslation: () => ({
  t: (key: string, args?: Record<string, number>) => key === "tools.ragCapacityFailures"
    ? "search=" + args?.searches + " read=" + args?.reads : key,
}) }));

// Anonymous snapshot of the first 33 persisted tools; IDs and content are synthetic.
const names = { s: "rag.search", r: "rag.read", p: "task.update_plan",
  d: "tools.discover", o: "tools.read_result" };
const record = "s s o o o d s s p p s r r s s p s s s r r r p s s r p s s p d s s".split(" ");
const operations = record.map((key, i) => {
  const name = names[key as keyof typeof names];
  const code = i > 0 && (key === "s" || key === "r") ? "rag_context_too_small"
    : [9, 23].includes(i + 1) ? "task_plan_invalid" : null;
  return { id: "op-" + i, run_id: "run", tool_name: name, sequence: i + 1,
    status: code ? "failed" : "completed", error_code: code } as ToolOperation;
});
function render(rows: ToolOperation[]) {
  return renderToStaticMarkup(<RunActivityGroup onResolved={() => {}} item={{
    key: "activity", kind: "activity", runId: "run", events: [], operations: rows,
    changeSets: [], executionSummary: null, webOperations: [], progress: [],
  }} />);
}
describe("RAG capacity activity replay", () => {
  it.each([[31, 13], [33, 15]])("counts the first %i operations", (length, searches) => {
    const rows = operations.slice(0, length);
    expect(render(rows)).toContain("search=" + searches + " read=6");
    // A pending approval expands the activity; duplicate event delivery must not add details.
    const pending = { ...operations[0], id: "pending", status: "waiting_user" } as ToolOperation;
    const html = render([...rows, rows[1], pending]);
    expect(html).toContain("search=" + searches + " read=6");
    expect([...html.matchAll(/<li>/g)]).toHaveLength(length + 1);
    expect([...html.matchAll(/<code>(.*?)<\/code>/g)].map(m => m[1])).toEqual(
      [...rows, pending].map(op => op.tool_name));
  });
});

it("shows saved public model prose without opening execution details", () => {
  const html=renderToStaticMarkup(<RunActivityGroup onResolved={() => {}} item={{
    key:"public",kind:"activity",runId:"run",events:[],operations:[],changeSets:[],executionSummary:null,webOperations:[],
    progress:[{message:{id:"msg",role:"system_event",content:"",event_type:"assistant_progress",conversation_id:"chat",source_message_id:null,created_at:"now"},
      summary:{version:1,run_id:"run",attempt_id:"attempt",round_no:1,content:"Ordinary public model paragraph"}}],
  }} />);
  expect(html).toContain("Ordinary public model paragraph");
});
