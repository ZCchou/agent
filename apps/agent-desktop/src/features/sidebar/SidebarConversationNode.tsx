import { CSS } from "@dnd-kit/utilities";
import { useSortable } from "@dnd-kit/sortable";
import { GripVertical, MessageCircle, MoreHorizontal } from "lucide-react";
import type { CSSProperties } from "react";
import type { Conversation } from "../../types/domain";
import type { DragResourceData } from "./useSidebarTreeController";
import { useTranslation } from "react-i18next";
import { useResourceActions } from "../resources/ResourceActionsProvider";

interface Props {
  conversation: Conversation;
  active: boolean;
  depth?: number;
  onOpen: () => void;
}

export function SidebarConversationNode({ conversation, active, depth = 1, onOpen }: Props) {
  const { t } = useTranslation("common");
  const actions = useResourceActions();
  const target = { kind: "conversation" as const, id: conversation.id };
  const containerId = conversation.kind === "standard_chat"
    ? conversation.chat_group_id : conversation.project_id;
  const data: DragResourceData = {
    entity: "conversation", kind: conversation.kind, id: conversation.id, containerId,
  };
  const sortable = useSortable({ id: conversation.id, data });
  const style = {
    transform: CSS.Transform.toString(sortable.transform),
    transition: sortable.transition,
    opacity: sortable.isDragging ? 0.45 : 1,
    "--tree-depth": depth,
  } as CSSProperties;
  return (
    <div
      ref={sortable.setNodeRef}
      style={style}
      className={`resource-item-row${active ? " resource-item-row--active" : ""}`}
      {...actions.menuHandlers(target)}
    >
      <button
        className="resource-drag-handle"
        {...sortable.attributes}
        {...sortable.listeners}
        aria-label={`Move ${conversation.title}`}
      ><GripVertical size={13} /></button>
      <button
        className="resource-item"
        onClick={onOpen}
      >{conversation.kind === "standard_chat" && <MessageCircle size={13} />}<span>{conversation.title}</span></button>
      <button className="resource-more" onClick={(event) => actions.openMenu(target, event)} aria-label={t("sidebar.moreActions")}>
        <MoreHorizontal size={14} />
      </button>
    </div>
  );
}
