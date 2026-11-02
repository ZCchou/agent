import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from "react";
import type { ContextSnapshot } from "../types/domain";

export type ChatInspectorView =
  | { kind: "context"; runId: string; snapshot: ContextSnapshot | null; loading: boolean; error: string | null }
  | { kind: "project_file"; title: string; path: string; content: string | null; dataUrl: string | null; language: string | null; lineStart: number | null; lineEnd: number | null }
  | { kind: "attachment"; title: string; attachmentId: string; content: string | null; dataUrl: string | null };

export interface ChatInspectorEntry {
  open: boolean;
  view: ChatInspectorView | null;
}

interface ChatInspectorStore {
  get(key: string): ChatInspectorEntry;
  update(key: string, updater: (current: ChatInspectorEntry) => ChatInspectorEntry): void;
  close(key: string): void;
  clear(key: string): void;
}

const EMPTY: ChatInspectorEntry = { open: false, view: null };
const Context = createContext<ChatInspectorStore | null>(null);

export function ChatInspectorStateProvider({ children }: { children: ReactNode }) {
  const [entries, setEntries] = useState<Record<string, ChatInspectorEntry>>({});
  const update = useCallback((key: string, updater: (current: ChatInspectorEntry) => ChatInspectorEntry) => {
    setEntries((current) => ({ ...current, [key]: updater(current[key] ?? EMPTY) }));
  }, []);
  const close = useCallback((key: string) => update(key, (current) => ({ ...current, open: false })), [update]);
  const clear = useCallback((key: string) => setEntries((current) => {
    const next = { ...current };
    delete next[key];
    return next;
  }), []);
  const value = useMemo<ChatInspectorStore>(() => ({
    get: (key) => entries[key] ?? EMPTY,
    update,
    close,
    clear,
  }), [clear, close, entries, update]);
  return <Context.Provider value={value}>{children}</Context.Provider>;
}

export function useChatInspectorStore() {
  const value = useContext(Context);
  if (!value) throw new Error("useChatInspectorStore must be used inside ChatInspectorStateProvider");
  return value;
}
