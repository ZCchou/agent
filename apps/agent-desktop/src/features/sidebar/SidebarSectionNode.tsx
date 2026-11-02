import type { ReactNode } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";

interface Props {
  title: ReactNode;
  collapsed: boolean;
  onToggle: () => void;
  actions?: ReactNode;
  children: ReactNode;
}

export function SidebarSectionNode({ title, collapsed, onToggle, actions, children }: Props) {
  return (
    <section className="resource-section">
      <div className="resource-section__header">
        <button className="resource-section__toggle" onClick={onToggle} aria-expanded={!collapsed}>
          {collapsed ? <ChevronRight size={14} /> : <ChevronDown size={14} />}{title}
        </button>
        <div>{actions}</div>
      </div>
      {!collapsed && children}
    </section>
  );
}
