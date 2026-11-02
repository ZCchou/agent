import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { appUpdateRepository } from "../../services/runtime";
import type { RestartDraft, RestoredUpdateDraft, UpdateChannel, UpdateView } from "../../types/updater";
import { version as appVersion } from "../../../package.json";
const CHANNEL_KEY = "agent:update-channel";
const AUTO_KEY = "agent:update-auto-check";
function preference(key: string) { try { return localStorage.getItem(key); } catch { return null; } }
function save(key: string, value: string) { try { localStorage.setItem(key, value); } catch { /* In-memory preferences remain usable. */ } }
const initial: UpdateView = { state: "idle", current_version: appVersion, channel: "preview", update_id: null,
  version: null, notes: null, downloaded_bytes: 0, total_bytes: null, error_code: null };
function errorCode(value: unknown) {
  const raw = value instanceof Error ? value.message : String(value);
  const code = raw === "app_update_busy" ? raw : raw.replace(/^app_update_/, "");
  return ["app_update_busy", "network", "signature_invalid", "feed_invalid", "not_ready", "stale", "core_unavailable", "draft_failed", "install_failed"].includes(code) ? code : "unknown";
}
type ContextValue = {
  view: UpdateView; channel: UpdateChannel; autoCheck: boolean; working: boolean; installing: boolean; error: string | null;
  setChannel: (channel: UpdateChannel) => void; setAutoCheck: (enabled: boolean) => void;
  check: () => Promise<void>; download: () => Promise<void>; install: () => Promise<void>; retry: () => Promise<void>;
  registerDraft: (getter: () => RestartDraft | null) => () => void;
  restored: RestoredUpdateDraft | null; restorationError: boolean; restoreConflict: boolean; setRestoreConflict: (value: boolean) => void;
  missingAttachments: number; restoreRequest: number; requestRestore: () => void; finishRestore: (updateId: string) => Promise<void>;
  retryRestoration: () => Promise<void>; restoring: boolean;
};
const Context = createContext<ContextValue | null>(null);
export function AppUpdateProvider({ children }: { children: ReactNode }) {
  const [view, setView] = useState(initial);
  const [channel, setChannelState] = useState<UpdateChannel>(() => preference(CHANNEL_KEY) === "stable" ? "stable" : "preview");
  const [autoCheck, setAutoState] = useState(() => preference(AUTO_KEY) !== "false");
  const autoRef = useRef(autoCheck); autoRef.current = autoCheck;
  const [working, setWorking] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [restored, setRestored] = useState<RestoredUpdateDraft | null>(null);
  const [restorationError, setRestorationError] = useState(false);
  const [restoring, setRestoring] = useState(false);
  const restorationLock = useRef(false);
  const [restoreConflict, setRestoreConflict] = useState(false);
  const [restoreRequest, setRestoreRequest] = useState(0);
  const [missingAttachments, setMissingAttachments] = useState(0);
  const stateRevision = useRef(0);
  const getter = useRef<(() => RestartDraft | null) | null>(null);
  const lock = useRef(false);
  const lastAction = useRef<"check" | "download" | "install">("check");
  const channelRef = useRef(channel); channelRef.current = channel;
  const apply = useCallback((next: UpdateView) => {
    if (next.channel !== channelRef.current) return;
    stateRevision.current += 1; const code = next.error_code ? errorCode(next.error_code) : null;
    setView({ ...next, error_code: code }); setError(code);
  }, []);
  const perform = useCallback(async (action: "check" | "download" | "install", updateId?: string | null) => {
    if (lock.current) return;
    stateRevision.current += 1;
    lock.current = true; setWorking(true); setError(null); lastAction.current = action;
    try {
      if (action === "check") {
        setView((current) => ({ ...current, state: "checking", channel: channelRef.current, error_code: null }));
        apply(await appUpdateRepository.check(channelRef.current));
      } else if (updateId) {
        if (action === "download") apply(await appUpdateRepository.download(updateId));
        else {
          // The native service makes the global admission/busy decision atomically.
          const draft = getter.current?.() ?? null;
          await appUpdateRepository.install(updateId, draft);
        }
      }
    } catch (value) {
      const code = errorCode(value); setError(code);
      // Installation failures preserve the verified ready download for a later retry.
      if (action !== "install") setView((current) => ({ ...current, state: "error", error_code: code }));
      else setView((current) => ({ ...current, state: "ready", error_code: code }));
    } finally { lock.current = false; setWorking(false); }
  }, [apply]);
  const check = useCallback(() => perform("check"), [perform]);
  const download = useCallback(() => perform("download", view.update_id), [perform, view.update_id]);
  const install = useCallback(() => perform("install", view.update_id), [perform, view.update_id]);
  const registerDraft = useCallback((next: () => RestartDraft | null) => {
    getter.current = next; return () => { if (getter.current === next) getter.current = null; };
  }, []);
  const finishRestore = useCallback(async (updateId: string) => {
    if (restorationLock.current) return;
    restorationLock.current = true; setRestoring(true);
    setMissingAttachments(restored?.missing_attachments ?? 0);
    try {
      await appUpdateRepository.ackDraft(updateId); setRestored(null); setRestoreConflict(false); setRestorationError(false);
    } catch { setRestorationError(true); }
    finally { restorationLock.current = false; setRestoring(false); }
  }, [restored]);
  const retryRestoration = useCallback(async () => {
    // An applied draft only needs acknowledgment; never apply it over newer typing.
    if (restored) { await finishRestore(restored.update_id); return; }
    if (restorationLock.current) return;
    restorationLock.current = true; setRestoring(true);
    try { setRestored(await appUpdateRepository.loadDraft()); setRestorationError(false); }
    catch { setRestorationError(true); }
    finally { restorationLock.current = false; setRestoring(false); }
  }, [restored, finishRestore]);
  useEffect(() => {
    let alive = true; let dispose: (() => void) | undefined;
    void appUpdateRepository.subscribe((next) => { if (alive) apply(next); }).then((stop) => { if (alive) dispose = stop; else stop(); }).catch(() => undefined);
    const initialRevision = stateRevision.current;
    void appUpdateRepository.status().then((next) => { if (alive && stateRevision.current === initialRevision && !lock.current) apply(next); }).catch(() => undefined);
    void appUpdateRepository.loadDraft().then((draft) => { if (alive) setRestored(draft); }).catch(() => { if (alive) setRestorationError(true); });
    // Yield the initial render; checking never blocks chat or waits for Settings.
    const timer = window.setTimeout(() => { if (autoRef.current) void check(); }, 1200);
    return () => { alive = false; dispose?.(); window.clearTimeout(timer); };
  }, [apply, check]);
  return <Context.Provider value={{ view, channel, autoCheck, working, installing: (working && lastAction.current === "install") || view.state === "installing", error,
    setChannel: (next) => { stateRevision.current += 1; channelRef.current = next; setChannelState(next); save(CHANNEL_KEY, next); setError(null); setView((current) => ({ ...initial, current_version: current.current_version, channel: next })); },
    setAutoCheck: (enabled) => { setAutoState(enabled); save(AUTO_KEY, String(enabled)); }, check, download, install,
    retry: () => perform(!view.update_id || ["signature_invalid", "stale", "feed_invalid"].includes(error ?? "") ? "check" : lastAction.current, view.update_id), registerDraft, restored, restorationError, restoreConflict, setRestoreConflict,
    missingAttachments, restoreRequest, requestRestore: () => { setRestoreConflict(false); setRestoreRequest((n) => n + 1); }, finishRestore, retryRestoration, restoring,
  }}>{children}</Context.Provider>;
}
export function useAppUpdate() { const value = useContext(Context); if (!value) throw new Error("AppUpdateProvider missing"); return value; }
