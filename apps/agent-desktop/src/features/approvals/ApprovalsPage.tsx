import { ShieldCheck } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { EmptyState } from "../../components/ui/EmptyState";
import { useApp } from "../../contexts/AppContext";
import { changeSetRepository } from "../../services/runtime";
import type { ChangeSet } from "../../types/domain";
import { ChangeSetCard } from "../chat/ChangeSetCard";
import { Button } from "../../components/ui/Button";

export function ApprovalsPage() {
  const { t } = useTranslation("common");
  const { openConversation, projects, conversations } = useApp();
  const [changeSets, setChangeSets] = useState<ChangeSet[]>([]);
  const [error, setError] = useState(false);
  const [loading, setLoading] = useState(true);
  const revision = useRef(0);
  const mounted = useRef(false);

  const load = useCallback(async () => {
    const request = ++revision.current;
    setLoading(true);
    try {
      const next = await changeSetRepository.list();
      if (mounted.current && request === revision.current) { setChangeSets(next); setError(false); }
    } catch {
      if (mounted.current && request === revision.current) setError(true);
    } finally { if (mounted.current && request === revision.current) setLoading(false); }
  }, []);

  useEffect(() => {
    let unsubscribe: (() => void) | undefined;
    let active = true;
    mounted.current = true;
    void load();
    void changeSetRepository.subscribe(() => { void load(); })
      .then((dispose) => { if (active) unsubscribe = dispose; else dispose(); }).catch(() => undefined);
    return () => { active = false; mounted.current = false; revision.current += 1; unsubscribe?.(); };
  }, [load]);

  return (
    <div className="approvals-page">
      <div className="page-intro">
        <h2>{t("pages.approvals.title")}</h2>
        <p>{t("pages.approvals.description")}</p>
      </div>
      {error ? <p className="state-message state-message--error">{t("approvals.error")}</p> : null}
      <Button disabled={loading} onClick={() => void load()}>{t(error ? "actions.retry" : "buttonAudit.refresh")}</Button>
      {!error && !loading && changeSets.length === 0 ? (
        <EmptyState icon={ShieldCheck} title={t("approvals.empty")} description={t("approvals.emptyDescription")} />
      ) : null}
      <div className="approval-list">
        {changeSets.map((item) => <article key={item.id}>
          <ChangeSetCard changeSet={item} projectName={projects.find((project) => project.id === item.project_id)?.name}
            conversationTitle={conversations.find((conversation) => conversation.id === item.conversation_id)?.title}
            onResolved={() => void load()} />
          <Button onClick={() => openConversation(item.conversation_id)}>{t("buttonAudit.openConversation")}</Button>
        </article>)}
      </div>
    </div>
  );
}
