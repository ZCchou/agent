import { useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";

interface OverlayProps {
  label: string;
  trigger: HTMLElement | null;
  onClose(): void;
  children: ReactNode;
}

export function ResourceContextMenu({ label, trigger, onClose, children, position, busy }: OverlayProps & {
  position: { x: number; y: number };
  busy: boolean;
}) {
  const ref = useRef<HTMLElement>(null);
  const [point, setPoint] = useState(position);
  useLayoutEffect(() => {
    const node = ref.current;
    if (!node) return;
    const reposition = () => {
      const rect = node.getBoundingClientRect();
      const x = Math.max(8, Math.min(position.x, window.innerWidth - rect.width - 8));
      const y = Math.max(8, Math.min(position.y, window.innerHeight - rect.height - 8));
      setPoint((current) => current.x === x && current.y === y ? current : { x, y });
    };
    reposition();
    const observer = new ResizeObserver(reposition);
    observer.observe(node);
    node.querySelector<HTMLButtonElement>("[role=menuitem]")?.focus({ preventScroll: true });
    return () => { observer.disconnect(); if (trigger?.isConnected) trigger.focus({ preventScroll: true }); };
  }, [position, trigger]);
  useLayoutEffect(() => {
    const dismiss = () => onClose();
    window.addEventListener("resize", dismiss);
    return () => {
      window.removeEventListener("resize", dismiss);
    };
  }, [onClose]);
  useLayoutEffect(() => {
    const node = ref.current;
    if (busy) node?.focus({ preventScroll: true });
    else if (node && (document.activeElement === node || !node.contains(document.activeElement))) node.querySelector<HTMLButtonElement>("[role=menuitem]")?.focus({ preventScroll: true });
  }, [busy]);
  return createPortal(<div className="resource-menu-dismiss" onMouseDown={onClose} onWheel={onClose}
    onContextMenu={(event) => { event.preventDefault(); onClose(); }}>
    <section ref={ref} className="resource-context-menu" role="menu" aria-label={label} tabIndex={-1} aria-busy={busy}
      style={{ left: point.x, top: point.y }} onMouseDown={(event) => event.stopPropagation()}
      onWheel={(event) => event.stopPropagation()}
      onContextMenu={(event) => { event.preventDefault(); event.stopPropagation(); }}
      onKeyDown={(event) => {
        if (event.key === "Escape" || event.key === "Tab") {
          event.preventDefault(); event.stopPropagation(); onClose(); return;
        }
        if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
        event.preventDefault();
        const items = [...event.currentTarget.querySelectorAll<HTMLButtonElement>("[role=menuitem]:not(:disabled)")];
        const current = items.indexOf(document.activeElement as HTMLButtonElement);
        const next = event.key === "Home" ? 0 : event.key === "End" ? items.length - 1
          : (current + (event.key === "ArrowDown" ? 1 : -1) + items.length) % items.length;
        items[next]?.focus({ preventScroll: true });
      }}>
      <h3 title={label}>{label}</h3>
      <div className="resource-action-menu">{children}</div>
    </section>
  </div>, document.body);
}

export function ResourceDialog({ label, trigger, onClose, children, busy }: OverlayProps & { busy: boolean }) {
  const ref = useRef<HTMLElement>(null);
  useLayoutEffect(() => {
    const node = ref.current;
    const editor = node?.querySelector<HTMLInputElement>("input");
    (editor ?? node?.querySelector<HTMLElement>("select,button"))?.focus();
    editor?.select();
    return () => { if (trigger?.isConnected) trigger.focus(); };
  }, [trigger]);
  useLayoutEffect(() => {
    const node = ref.current;
    if (busy) node?.focus({ preventScroll: true });
    else if (node && (document.activeElement === node || !node.contains(document.activeElement))) node.querySelector<HTMLElement>("input,select,button")?.focus();
  }, [busy]);
  return createPortal(<div className="resource-dialog-backdrop" onMouseDown={() => { if (!busy) onClose(); }}>
    <section ref={ref} className="resource-dialog" role="dialog" aria-modal="true" aria-label={label} aria-busy={busy} tabIndex={-1}
      onMouseDown={(event) => event.stopPropagation()} onKeyDown={(event) => {
        if (event.key === "Escape") {
          event.preventDefault(); event.stopPropagation(); if (!busy) onClose();
        }
        if (event.key === "Tab") {
          const controls = [...event.currentTarget.querySelectorAll<HTMLElement>("input:not(:disabled),select:not(:disabled),button:not(:disabled)")];
          const first = controls[0], last = controls[controls.length - 1];
          if (!first) { event.preventDefault(); return; }
          if (!controls.includes(document.activeElement as HTMLElement)) {
            event.preventDefault(); (event.shiftKey ? last : first)?.focus(); return;
          }
          if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
          else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
        }
      }}>
      {children}
    </section>
  </div>, document.body);
}
