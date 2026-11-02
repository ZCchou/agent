import { ArrowLeft, BriefcaseBusiness, MessageSquare, RotateCcw, Trash2 } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Card } from "../../components/ui/Card";
import { useApp } from "../../contexts/AppContext";
import { trashRepository } from "../../services/runtime";
import type { TrashItem } from "../../types/domain";

interface TrashSettingsPageProps {
  onBack: () => void;
}

export function TrashSettingsPage({ onBack }: TrashSettingsPageProps) {
  const { t } = useTranslation("common");
  const { refreshWorkspace } = useApp();
  const [items, setItems] = useState<TrashItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const initialLoadStarted = useRef(false);
  const mutationLock = useRef(false);
  const [mutating, setMutating] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setItems(await trashRepository.list());
      setError(false);
    } catch {
      setError(true);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (initialLoadStarted.current) return;
    initialLoadStarted.current = true;
    void load();
  }, [load]);

  async function restore(item: TrashItem) {
    if (mutationLock.current) return;
    mutationLock.current = true; setMutating(true);
    try {
      await trashRepository.restore(item.entity_type, item.id);
      await Promise.all([load(), refreshWorkspace()]);
    } catch {
      setError(true);
      window.alert(t("settings.trash.restoreFailed"));
    } finally { mutationLock.current = false; setMutating(false); }
  }

  async function permanentlyDelete(item: TrashItem) {
    if (mutationLock.current) return;
    if (!window.confirm(t("settings.trash.confirmPermanent", { title: item.title }))) return;
    mutationLock.current = true; setMutating(true);
    try {
      await trashRepository.deletePermanently(item.entity_type, item.id);
      await load();
    } catch {
      setError(true);
    } finally { mutationLock.current = false; setMutating(false); }
  }

  async function empty() {
    if (mutationLock.current) return;
    if (!window.confirm(t("settings.trash.confirmEmpty"))) return;
    mutationLock.current = true; setMutating(true);
    try {
      await trashRepository.empty();
      await Promise.all([load(), refreshWorkspace()]);
    } catch {
      setError(true);
    } finally { mutationLock.current = false; setMutating(false); }
  }

  return (
    <div className="settings-shell settings-trash-page">
      <div className="settings-subpage-heading">
        <button type="button" onClick={onBack} aria-label={t("actions.back")} title={t("actions.back")}>
          <ArrowLeft size={19} />
        </button>
        <div className="page-intro">
          <h2>{t("settings.trash.title")}</h2>
          <p>{t("settings.trash.description")}</p>
        </div>
      </div>

      <Card className="settings-trash-card">
        <div className="settings-trash-toolbar">
          <div className="settings-trash-summary">
            <span className="settings-trash-summary__icon"><Trash2 size={22} /></span>
            <div>
              <strong>{t("settings.trash.contents")}</strong>
              <span>{t("settings.trash.itemCount", { count: items.length })}</span>
            </div>
          </div>
          {!loading && items.length > 0 && (
            <button className="danger-button" type="button" disabled={mutating} onClick={() => void empty()}>
              {t("settings.trash.emptyAction")}
            </button>
          )}
        </div>

        {loading ? (
          <div className="settings-trash-state">{t("settings.trash.loading")}</div>
        ) : error ? (
          <button className="resource-inline-error" type="button" onClick={() => void load()}>
            {t("settings.trash.error")}
          </button>
        ) : items.length === 0 ? (
          <div className="settings-trash-state settings-trash-state--empty">
            <Trash2 size={28} />
            <strong>{t("settings.trash.empty")}</strong>
            <span>{t("settings.trash.emptyDescription")}</span>
          </div>
        ) : (
          <div className="settings-trash-list">
            {items.map((item) => {
              const ItemIcon = item.entity_type === "project" ? BriefcaseBusiness : MessageSquare;
              return (
                <article key={`${item.entity_type}:${item.id}`}>
                  <span className="settings-trash-item__icon"><ItemIcon size={18} /></span>
                  <div className="settings-trash-item__copy">
                    <strong title={item.title}>{item.title}</strong>
                    <span>{item.entity_type === "project"
                      ? t("settings.trash.project", { workCount: item.work_count, messageCount: item.message_count })
                      : t("settings.trash.conversation", {
                        messageCount: item.message_count,
                        kind: t(`conversation.${item.conversation_kind === "project_work" ? "projectWork" : "chatOnly"}`),
                      })}</span>
                    <small>{t("settings.trash.expires", { date: new Date(item.expires_at).toLocaleDateString() })}</small>
                  </div>
                  <div className="settings-trash-item__actions">
                    <button type="button" disabled={mutating} onClick={() => void restore(item)}>
                      <RotateCcw size={14} />{t("actions.restore")}
                    </button>
                    <button type="button" className="danger-button" disabled={mutating} onClick={() => void permanentlyDelete(item)}>
                      <Trash2 size={14} />{t("settings.trash.permanent")}
                    </button>
                  </div>
                </article>
              );
            })}
          </div>
        )}
      </Card>
    </div>
  );
}
