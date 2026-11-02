import type {
  LucideIcon,
} from "lucide-react";

import { EmptyState } from "../ui/EmptyState";

interface PlaceholderPageProps {
  icon: LucideIcon;
  title: string;
  description: string;
}

export function PlaceholderPage({
  icon,
  title,
  description,
}: PlaceholderPageProps) {
  return (
    <div className="placeholder-page">
      <EmptyState
        icon={icon}
        title={title}
        description={description}
      />
    </div>
  );
}