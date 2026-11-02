import type { CSSProperties, ReactNode } from "react";
import { CSS } from "@dnd-kit/utilities";
import { useSortable } from "@dnd-kit/sortable";
import { ChevronDown, ChevronRight, Folder, GripVertical, MessagesSquare } from "lucide-react";
import type { DragResourceData } from "./useSidebarTreeController";
import { useResourceActions } from "../resources/ResourceActionsProvider";

interface Props {
  id: string;
  kind: "chat_group" | "project";
  name: string;
  collapsed: boolean;
  onToggle: () => void;
  onOpen?: () => void;
  actions?: ReactNode;
  children: ReactNode;
}

export function SidebarContainerNode(props: Props) {
  const resourceActions = useResourceActions();
  const ContainerIcon = props.kind === "chat_group" ? MessagesSquare : Folder;
  const data: DragResourceData = { entity: "container", containerKind: props.kind, id: props.id };
  const sortable = useSortable({ id: props.id, data });
  return (
    <div
      ref={sortable.setNodeRef}
      className={`resource-group${sortable.isOver ? " resource-group--drop" : ""}`}
      style={{ transform: CSS.Transform.toString(sortable.transform), transition: sortable.transition,
        "--tree-depth": 1 } as CSSProperties}
    >
      <div className="resource-group__title" {...(props.kind === "project" ? resourceActions.menuHandlers({ kind: "project", id: props.id }) : {})}>
        <button
          className="resource-chevron"
          onClick={props.onToggle}
          aria-expanded={!props.collapsed}
          aria-label={props.collapsed ? "Expand" : "Collapse"}
        >{props.collapsed ? <ChevronRight size={14} /> : <ChevronDown size={14} />}</button>
        <button className="resource-drag-handle" {...sortable.attributes} {...sortable.listeners} aria-label={`Move ${props.name}`}>
          <GripVertical size={12} />
        </button>
        <button className="resource-container-open" onClick={props.onOpen ?? props.onToggle} title={props.name}>
          <ContainerIcon className="resource-container-icon" size={18} strokeWidth={1.8} />
          <span>{props.name}</span>
        </button>
        <div className="resource-container-actions">{props.actions}</div>
      </div>
      {!props.collapsed && props.children}
    </div>
  );
}
