import type { KeyboardEvent as ReactKeyboardEvent, PointerEvent as ReactPointerEvent } from "react";
import { useCallback, useEffect, useRef, useState } from "react";
import { settingsRepository } from "../../services/runtime";
import { clampSidebarWidth, DEFAULT_SIDEBAR_WIDTH } from "./sidebarSizing";

export function useResizableSidebar(onSaveError: () => void) {
  const [width, setWidth] = useState(DEFAULT_SIDEBAR_WIDTH);
  const persistedWidth = useRef(DEFAULT_SIDEBAR_WIDTH);
  const revision = useRef(0);
  const pending = useRef(0);
  const dragCleanup = useRef<(() => void) | null>(null);

  useEffect(() => {
    let active = true;
    const request = revision.current;
    void settingsRepository.getAppSettings().then((settings) => {
      if (!active || request !== revision.current) return;
      const loaded = clampSidebarWidth(settings.sidebar.width_px);
      persistedWidth.current = loaded;
      setWidth(loaded);
    }).catch(() => undefined);
    return () => { active = false; };
  }, []);

  useEffect(() => {
    document.documentElement.style.setProperty("--sidebar-width", `${width}px`);
  }, [width]);

  useEffect(() => () => dragCleanup.current?.(), []);

  const persist = useCallback(async (nextWidth: number, request: number) => {
    pending.current += 1;
    try {
      const settings = await settingsRepository.updateSidebarWidth(nextWidth);
      const saved = clampSidebarWidth(settings.sidebar.width_px);
      persistedWidth.current = saved;
      if (request === revision.current) setWidth(saved);
    } catch {
      if (request === revision.current) { setWidth(persistedWidth.current); onSaveError(); }
    } finally { pending.current -= 1; }
  }, [onSaveError]);

  const onPointerDown = useCallback((event: ReactPointerEvent<HTMLDivElement>) => {
    if (event.button !== 0) return;
    event.preventDefault();
    dragCleanup.current?.();
    const request = ++revision.current;
    const startX = event.clientX;
    const startWidth = width;
    let latestWidth = width;

    const onMove = (moveEvent: PointerEvent) => {
      latestWidth = clampSidebarWidth(startWidth + moveEvent.clientX - startX);
      setWidth(latestWidth);
    };
    const cleanup = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onCancel);
      document.body.classList.remove("sidebar-is-resizing");
      dragCleanup.current = null;
    };
    const onUp = () => {
      cleanup();
      if (latestWidth !== persistedWidth.current || pending.current > 0) void persist(latestWidth, request);
    };
    const onCancel = () => {
      cleanup();
      setWidth(persistedWidth.current);
    };

    document.body.classList.add("sidebar-is-resizing");
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onCancel);
    dragCleanup.current = cleanup;
  }, [persist, width]);

  const applyAndPersist = useCallback((candidate: number) => {
    const request = ++revision.current;
    const nextWidth = clampSidebarWidth(candidate);
    setWidth(nextWidth);
    if (nextWidth !== persistedWidth.current || pending.current > 0) void persist(nextWidth, request);
  }, [persist]);

  const onKeyDown = useCallback((event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    const step = event.shiftKey ? 24 : 8;
    applyAndPersist(width + (event.key === "ArrowRight" ? step : -step));
  }, [applyAndPersist, width]);

  const reset = useCallback(() => applyAndPersist(DEFAULT_SIDEBAR_WIDTH), [applyAndPersist]);

  return { width, onPointerDown, onKeyDown, reset };
}
