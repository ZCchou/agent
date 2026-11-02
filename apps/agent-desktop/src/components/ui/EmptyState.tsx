import type {
  LucideIcon,
} from "lucide-react";

interface EmptyStateProps {
  icon: LucideIcon;
  title: string;
  description: string;
}

export function EmptyState({
  icon: Icon,
  title,
  description,
}: EmptyStateProps) {
  return (
    <div className="empty-state">
      <div className="empty-state__icon">
        <Icon size={28} />
      </div>

      <h2>{title}</h2>

      <p>{description}</p>
    </div>
  );
}