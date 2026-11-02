import { Bot, ChevronDown, Sparkles } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import type { Conversation } from "../../types/domain";

export interface RoutingModelOption {
  value: string;
  providerId: string;
  modelId: string;
  label: string;
}

interface Props {
  conversation: Conversation | null;
  options: RoutingModelOption[];
  disabled: boolean;
  onSelectAutomatic(): void;
  onSelectFixed(option: RoutingModelOption): void;
}

export function ConversationRoutingControl({
  conversation, options, disabled, onSelectAutomatic, onSelectFixed,
}: Props) {
  const { t } = useTranslation("chat");
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const close = (event: MouseEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    window.addEventListener("mousedown", close);
    return () => window.removeEventListener("mousedown", close);
  }, [open]);
  useEffect(() => setOpen(false), [conversation?.id]);

  const automatic = !conversation || conversation.model_selection_mode === "automatic";
  const coordinator = conversation?.coordinator_model_id;
  const fixed = conversation?.model_id;
  const title = automatic
    ? t("routing.automatic")
    : t("routing.fixedShort", { model: fixed ?? t("routing.unselected") });
  return <div className="conversation-routing-control" ref={rootRef}>
    <button type="button" disabled={disabled || !conversation} aria-expanded={open}
      title={!conversation ? t("routing.availableAfterFirstMessage") : undefined}
      onClick={() => setOpen((value) => !value)}>
      {automatic ? <Sparkles size={14} /> : <Bot size={14} />}
      <span>{title}</span><ChevronDown size={13} />
    </button>
    {open && conversation ? <div className="conversation-routing-menu" role="menu">
      <button type="button" className={automatic ? "is-selected" : ""}
        onClick={() => { setOpen(false); onSelectAutomatic(); }}>
        <Sparkles size={15} /><span><strong>{t("routing.automatic")}</strong>
          <small>{coordinator
            ? `${t("routing.coordinator", { model: coordinator })} · ${t("routing.coordinatorResetHint")}`
            : t("routing.coordinatorPending")}</small></span>
      </button>
      <div className="conversation-routing-menu__separator" />
      {options.map((option) => <button type="button" key={option.value}
        className={!automatic && fixed === option.modelId
          && conversation.provider_profile_id === option.providerId ? "is-selected" : ""}
        onClick={() => { setOpen(false); onSelectFixed(option); }}>
        <Bot size={15} /><span><strong>{option.modelId}</strong><small>{option.label}</small></span>
      </button>)}
    </div> : null}
  </div>;
}
