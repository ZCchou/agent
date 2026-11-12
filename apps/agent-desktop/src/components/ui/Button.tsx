import { forwardRef, useId, type ButtonHTMLAttributes, type ReactNode } from "react";
import { LoaderCircle } from "lucide-react";

interface ButtonProps
  extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: "primary" | "secondary" | "ghost" | "danger";
  size?: "regular" | "dense" | "composer";
  icon?: ReactNode;
  busy?: boolean;
  disabledReason?: string;
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button({
  variant = "secondary",
  size = "regular",
  icon,
  busy = false,
  disabledReason,
  disabled,
  type = "button",
  children,
  className = "",
  ...props
}, ref) {
  const reasonId = useId();
  const button = (
    <button
      {...props}
      ref={ref}
      type={type}
      disabled={disabled || busy}
      aria-busy={busy || undefined}
      aria-describedby={disabled && disabledReason ? [props['aria-describedby'], reasonId].filter(Boolean).join(' ') : props['aria-describedby']}
      className={`ui-button ui-button--${variant} ui-button--${size} ${className}`}
    >
      {(busy || icon) && (
        <span className={`ui-button__icon${busy ? ' ui-button__spinner' : ''}`} aria-hidden="true">
          {busy ? <LoaderCircle /> : icon}
        </span>
      )}

      {children}
    </button>
  );
  return disabled && disabledReason ? <span className="ui-button-with-reason">{button}<span id={reasonId} role="note" className="ui-button__reason">{disabledReason}</span></span> : button;
});

export const IconButton = forwardRef<HTMLButtonElement, ButtonProps & { label: string }>(function IconButton({ label, title = label, children, className = '', ...props }, ref) {
  return <Button {...props} ref={ref} icon={children} aria-label={label} title={title} className={`ui-icon-button ${className}`} />;
});

export function ButtonGroup({ label, children }: { label: string; children: ReactNode }) {
  return <div className="ui-button-group" role="group" aria-label={label}>{children}</div>;
}
