import { useEffect, useRef, useState } from "react";

/** A pending action owns its original UI scope, including failures and cleanup. */
export function useScopedAction(scope: string, failureMessage: string) {
  const live = useRef({ scope, revision: 0 });
  if (live.current.scope !== scope) live.current = { scope, revision: live.current.revision + 1 };
  const revision = live.current.revision;
  const mounted = useRef(true);
  const pending = useRef<symbol | null>(null);
  const [state, setState] = useState({ revision, busy: false, error: null as string | null });
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  useEffect(() => { pending.current = null; setState({ revision, busy: false, error: null }); }, [revision]);
  async function run(operation: (isCurrent: () => boolean) => Promise<void>) {
    if (pending.current) return false;
    const token = Symbol();
    pending.current = token;
    const isCurrent = () => mounted.current && live.current.revision === revision;
    setState({ revision, busy: true, error: null });
    try { await operation(isCurrent); return true; }
    catch { if (isCurrent()) setState({ revision, busy: true, error: failureMessage }); return false; }
    finally {
      if (pending.current === token) {
        pending.current = null;
        if (isCurrent()) setState(current => ({ ...current, busy: false }));
      }
    }
  }
  return { run, busy: state.revision === revision && state.busy, error: state.revision === revision ? state.error : null };
}
