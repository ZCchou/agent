interface StatusDotProps {
  status:
    | "online"
    | "offline"
    | "warning";
}

export function StatusDot({
  status,
}: StatusDotProps) {
  return (
    <span
      className={`status-dot status-dot--${status}`}
      aria-hidden="true"
    />
  );
}