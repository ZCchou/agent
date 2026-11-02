import { ChevronDown } from "lucide-react";
import { useId, useState } from "react";
import { useTranslation } from "react-i18next";
import { AssistantMessageBody, type AssistantMessageBodyProps } from "./markdown/AssistantMessageBody";
import { pauseProcessFollowing } from "./useConversationScroll";

export function WorkProcessDisclosure(props: AssistantMessageBodyProps) {
  const { t } = useTranslation("chat");
  const [open, setOpen] = useState(false);
  const id = useId();
  return <section className="work-process">
    <button type="button" className="work-process__toggle" aria-expanded={open} aria-controls={id}
      onClick={(event) => {
        if (!open) pauseProcessFollowing(event.currentTarget);
        setOpen((value) => !value);
      }}>
      <span>{t("process.title")}</span><ChevronDown size={14} className={open ? "work-process__chevron--open" : ""} />
    </button>
    {open ? <div id={id} className="work-process__body"><AssistantMessageBody {...props} /></div> : null}
  </section>;
}
