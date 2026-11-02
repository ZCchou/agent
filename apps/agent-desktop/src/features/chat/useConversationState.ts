import { useCallback, useEffect, useRef, useState, type Dispatch, type SetStateAction } from "react";

/** Async work may finish after navigation; its setters belong to the old scope. */
export function useConversationState<S>(scope: string, initial: S | (() => S)): [S, Dispatch<SetStateAction<S>>] {
  const liveScope = useRef({ key: scope, revision: 0 });
  const mounted = useRef(true);
  if (liveScope.current.key !== scope) liveScope.current = { key: scope, revision: liveScope.current.revision + 1 };
  const revision = liveScope.current.revision;
  const [value, setValue] = useState(initial);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);
  const setScopedValue = useCallback<Dispatch<SetStateAction<S>>>((next) => {
    if (mounted.current && liveScope.current.revision === revision) setValue(next);
  }, [revision]);
  return [value, setScopedValue];
}
