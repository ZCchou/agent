import type { ModelRunEvent } from "../../types/domain";
export interface DocumentPublicText { runId: string; closed?: boolean; streams: Array<{id:string; content:string; sequence:number}> }
export function appendDocumentPublicText(current: DocumentPublicText | null, event: ModelRunEvent, activeRunId: string | null): DocumentPublicText | null {
  if (event.run_id !== activeRunId) return current;
  if (["run.failed","run.cancelled","run.interrupted","run.completed"].includes(event.event_type))
    return {...(current?.runId === event.run_id ? current : {runId:event.run_id,streams:[]}),closed:true};
  if (event.event_type === "run.state_changed" && event.data.status === "generating")
    return {...(current?.runId === event.run_id ? current : {runId:event.run_id,streams:[]}),closed:false};
  if (current?.runId === event.run_id && current.closed) return current;
  if (event.event_type !== "run.document_processing"
    || event.data.phase !== "public_text" || event.data.public_text !== true
    || typeof event.data.stream_id !== "string" || typeof event.data.delta !== "string") return current;
  const next: DocumentPublicText = current?.runId === event.run_id ? current : {runId:event.run_id,streams:[]};
  const old = next.streams.find(stream => stream.id === event.data.stream_id);
  if (old && old.sequence >= event.sequence) return current;
  const entry={id:event.data.stream_id,content:(old?.content ?? "")+event.data.delta,sequence:event.sequence};
  return {...next,streams:old ? next.streams.map(stream => stream.id === entry.id ? entry : stream) : [...next.streams,entry]};
}
