import { useCallback, useMemo, useState } from "react";
import type { AttachmentReference, ChatMessage, MessageAttachment } from "../../types/domain";
import { attachmentReferenceCandidates } from "./attachmentReferences";

export interface SelectedAttachmentReference {
  attachment: MessageAttachment;
  reference: AttachmentReference;
}

export function useAttachmentReferences(scope: string, input: string, messages: ChatMessage[]) {
  const [state, setState] = useState<{
    scope: string; selected: SelectedAttachmentReference[]; dismissed: string[];
  }>({ scope, selected: [], dismissed: [] });
  const proposals = useMemo(() => attachmentReferenceCandidates(input, messages), [input, messages]);
  const explicit = state.scope === scope ? state.selected : [];
  const dismissed = state.scope === scope ? state.dismissed : [];
  const selected = [...explicit];
  if (proposals.automatic) {
    const attachment = proposals.candidates[0];
    if (!dismissed.includes(attachment.id) && !selected.some((item) => item.attachment.id === attachment.id)) {
      selected.push({ attachment, reference: { attachment_id: attachment.id } });
    }
  }
  const candidates = proposals.automatic ? [] : proposals.candidates.filter((attachment) =>
    !dismissed.includes(attachment.id) && !selected.some((item) => item.attachment.id === attachment.id));
  const requiresChoice = candidates.length > 0
    && !selected.some((item) => proposals.candidates.some((candidate) => candidate.id === item.attachment.id));

  const choose = useCallback((attachment: MessageAttachment, range?: { source_start: number; source_end: number }) => {
    setState((current) => ({
      scope,
      selected: [...(current.scope === scope ? current.selected : []).filter((item) => item.attachment.id !== attachment.id),
        { attachment, reference: { attachment_id: attachment.id, ...range } }],
      dismissed: (current.scope === scope ? current.dismissed : []).filter((id) => id !== attachment.id),
    }));
  }, [scope]);
  const remove = useCallback((id: string) => {
    setState((current) => ({ scope,
      selected: (current.scope === scope ? current.selected : []).filter((item) => item.attachment.id !== id),
      dismissed: [...new Set([...(current.scope === scope ? current.dismissed : []), id])],
    }));
  }, [scope]);
  const dismissCandidates = () => setState((current) => ({ scope,
    selected: current.scope === scope ? current.selected : [],
    dismissed: [...new Set([...(current.scope === scope ? current.dismissed : []), ...candidates.map((item) => item.id)])],
  }));
  const clear = () => setState({ scope, selected: [], dismissed: [] });
  const restore = useCallback((selected: SelectedAttachmentReference[]) => setState({ scope, selected, dismissed: [] }), [scope]);
  return { restore, selected, candidates, requiresChoice, choose, remove, dismissCandidates, clear };
}
