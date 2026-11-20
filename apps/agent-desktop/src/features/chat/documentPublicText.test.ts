import {it,expect,vi} from "vitest";
import {appendDocumentPublicText, createDocumentTextBuffer, reconcileDocumentReceipt, type DocumentPublicText} from "./documentPublicText";
import type {ChatMessage,ModelRunEvent} from "../../types/domain";
const event:ModelRunEvent={run_id:"run",sequence:1,event_type:"run.document_processing",timestamp:"now",data:{phase:"public_text",public_text:true,stream_id:"batch-1",delta:"Public document text"}};
it("shows first delta independently and deduplicates the same sequence",()=>{
 const first=appendDocumentPublicText(null,event,"run");expect(first?.streams[0].content).toBe("Public document text");
 expect(appendDocumentPublicText(first,event,"run")).toBe(first);
 expect(appendDocumentPublicText(first,{...event,sequence:2,data:{...event.data,delta:" continues"}},"run")?.streams[0].content).toBe("Public document text continues");
});
it("does not mix another run or private activity into public document text",()=>{
 expect(appendDocumentPublicText(null,event,"another")).toBeNull();
 expect(appendDocumentPublicText(null,{...event,data:{...event.data,public_text:false}},"run")).toBeNull();
});

it("retains public text on cancellation and rejects late deltas until an explicit resumed run state",()=>{
 const first=appendDocumentPublicText(null,event,"run");
 const cancelled=appendDocumentPublicText(first,{...event,event_type:"run.cancelled",sequence:2},"run");
 expect(cancelled?.streams[0].content).toBe("Public document text");
 expect(appendDocumentPublicText(cancelled,{...event,sequence:3},"run")).toBe(cancelled);
 const resumed=appendDocumentPublicText(cancelled,{...event,event_type:"run.state_changed",data:{status:"generating"},sequence:4},"run");
 expect(resumed?.closed).toBe(false);
 expect(appendDocumentPublicText(resumed,{...event,sequence:5,data:{...event.data,stream_id:"new-attempt"}},"run")?.streams).toHaveLength(2);
});

const receipt = (content: string, conversation_id = "c"): ChatMessage => ({
  id: "official", role: "assistant", conversation_id, content, source_message_id: null, event_type: null, created_at: "now",
});
const preview = (...entries: string[]): DocumentPublicText => ({
  runId: "run", streams: entries.map((content, i) => ({ id: String(i), sequence: i + 1, content })),
});

it("retires only sections actually present in the final assistant receipt from the same run and conversation", () => {
  const current = preview("完整第1批", "仍需保留的第2批", "完整第3批", "完整第3批");
  const final = receipt("# 正式报告\n\n完整第1批\n\n完整第3批");
  expect(reconcileDocumentReceipt(current,"other","c",final)).toBe(current);
  expect(reconcileDocumentReceipt(current,"run","draft",final)).toBe(current);
  expect(reconcileDocumentReceipt(current,"run","c",{...final, role:"system_event"})).toBe(current);
  expect(reconcileDocumentReceipt(current,"run","c",null)).toBe(current);
  const merged = reconcileDocumentReceipt(current,"run","c",final);
  expect(merged?.streams.map(item => item.id)).toEqual(["1","3"]);
});

it("coalesces bursty output and flushes undisplayed text before a completion receipt", () => {
  vi.useFakeTimers();
  try {
    let state: DocumentPublicText | null = null;
    const publish = vi.fn((update: (previous: DocumentPublicText|null) => DocumentPublicText|null) => {state=update(state);});
    const buffer = createDocumentTextBuffer(publish);
    for(let i=1;i<=40;i++) buffer.event({...event,sequence:i,data:{...event.data,delta:String(i)+" "}},"run");
    expect(publish).not.toHaveBeenCalled();
    vi.advanceTimersByTime(60);
    expect(publish).toHaveBeenCalledTimes(1);
    const expected=Array.from({length:40},(_,i)=>String(i+1)+" ").join("");
    const current=()=>state as DocumentPublicText|null;
    expect(current()?.streams[0].content).toBe(expected);
    buffer.event({...event,sequence:42,data:{...event.data,stream_id:"other",delta:"保留中的草稿"}},"run");
    buffer.event({...event,event_type:"run.completed",sequence:43},"run");
    buffer.receipt("run","c",receipt(expected));
    expect(current()?.streams.map(s => s.content)).toEqual(["保留中的草稿"]);
    vi.advanceTimersByTime(500);
    expect(current()?.streams).toHaveLength(1);
  } finally { vi.useRealTimers(); }
});

it("preserves buffered text on interruption and disarms pending updates after navigation", () => {
  vi.useFakeTimers();
  try {
    let state: DocumentPublicText|null = null;
    const buffer=createDocumentTextBuffer(update=>{state=update(state);});
    const current=()=>state as DocumentPublicText|null;
    buffer.event(event,"run");
    buffer.event({...event,event_type:"run.cancelled",sequence:2},"run");
    expect(current()?.closed).toBe(true);
    expect(current()?.streams[0].content).toBe(event.data.delta);
    buffer.event({...event,run_id:"other",sequence:3},"other");
    buffer.dispose();
    vi.advanceTimersByTime(500);
    expect(current()?.runId).toBe("run");
  } finally { vi.useRealTimers(); }
});
