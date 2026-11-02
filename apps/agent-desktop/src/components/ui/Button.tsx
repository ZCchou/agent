import { forwardRef, type ButtonHTMLAttributes, type ReactNode } from "react";

interface ButtonProps
  extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: "primary" | "secondary" | "ghost";
  icon?: ReactNode;
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button({
  variant = "secondary",
  icon,
  children,
  className = "",
  ...props
}, ref) {
  return (
    <button
      ref={ref}
      className={`ui-button ui-button--${variant} ${className}`}
      {...props}
    >
      {icon && (
        <span className="ui-button__icon">
          {icon}
        </span>
      )}

      {children}
    </button>
  );
});
