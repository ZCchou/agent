import { AlertTriangle, X } from "lucide-react";
import { useEffect, useRef, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Button } from "./Button";

interface ConfirmDialogProps {
  open: boolean;
  title: string;
  description: string;
  confirmLabel: string;
  busy?: boolean;
  tone?: "default" | "danger";
  details?: ReactNode;
  onCancel(): void;
  onConfirm(): void;
}

export function ConfirmDialog({
  open, title, description, confirmLabel, busy = false, tone = "default", details, onCancel, onConfirm,
}: ConfirmDialogProps) {
  const { t } = useTranslation("common");
  const cancelButton = useRef<HTMLButtonElement>(null);
  const busyRef = useRef(busy);
  const cancelRef = useRef(onCancel);
  busyRef.current = busy;
  cancelRef.current = onCancel;

  useEffect(() => {
    if (!open) return;
    const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    cancelButton.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.stopPropagation();
      if (!busyRef.current) cancelRef.current();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      window.requestAnimationFrame(() => previousFocus?.focus());
    };
  }, [open]);

  if (!open) return null;
  return <div className="confirm-dialog-backdrop" role="presentation" onMouseDown={() => { if (!busy) onCancel(); }}>
    <section className={`confirm-dialog confirm-dialog--${tone}`} role="alertdialog" aria-modal="true"
      aria-labelledby="confirm-dialog-title" aria-describedby="confirm-dialog-description"
      onMouseDown={(event) => event.stopPropagation()}>
      <header>
        <span className="confirm-dialog__icon"><AlertTriangle size={20} /></span>
        <div><h2 id="confirm-dialog-title">{title}</h2><p id="confirm-dialog-description">{description}</p></div>
        <button type="button" aria-label={t("actions.close")} disabled={busy} onClick={onCancel}><X size={18} /></button>
      </header>
      {details ? <div className="confirm-dialog__details">{details}</div> : null}
      <footer>
        <Button ref={cancelButton} type="button" variant="ghost" disabled={busy} onClick={onCancel}>{t("actions.cancel")}</Button>
        <Button type="button" variant={tone === "danger" ? "secondary" : "primary"}
          className={tone === "danger" ? "ui-button--danger" : ""} disabled={busy} onClick={onConfirm}>
          {busy ? t("actions.saving") : confirmLabel}
        </Button>
      </footer>
    </section>
  </div>;
}
