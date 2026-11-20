import {it,expect} from "vitest";
import {appendDocumentPublicText} from "./documentPublicText";
import type {ModelRunEvent} from "../../types/domain";
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
