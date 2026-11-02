import { Brain, RotateCcw, Save } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "../../components/ui/Button";
import { Card } from "../../components/ui/Card";
import { memoryRepository } from "../../services/runtime";
import type { GlobalInstructions } from "../../types/memory";

const MAX_GLOBAL_INSTRUCTION_CHARS = 8_000;

export function MemoryPage() {
  const { t } = useTranslation("common");
  const [saved, setSaved] = useState<GlobalInstructions | null>(null);
  const [content, setContent] = useState("");
  const [enabled, setEnabled] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const mounted = useRef(false);
  const pending = useRef(false);

  const load = useCallback(async () => {
    if (pending.current) return;
    pending.current = true; setBusy(true); setError(null);
    try {
      const value = await memoryRepository.getGlobalInstructions();
      if (mounted.current) { setSaved(value); setContent(value.content); setEnabled(value.enabled); }
    } catch { if (mounted.current) setError("load"); }
    finally { pending.current = false; if (mounted.current) setBusy(false); }
  }, []);
  useEffect(() => { mounted.current = true; void load(); return () => { mounted.current = false; }; }, [load]);

  const dirty = saved !== null && (content !== saved.content || enabled !== saved.enabled);

  async function save() {
    if (pending.current) return;
    pending.current = true;
    setBusy(true);
    setError(null);
    try {
      const value = await memoryRepository.updateGlobalInstructions(content, enabled);
      setSaved(value);
      setContent(value.content);
      setEnabled(value.enabled);
    } catch {
      setError("save");
    } finally {
      pending.current = false;
      setBusy(false);
    }
  }

  async function reset() {
    if (pending.current) return;
    if (!window.confirm(t("pages.memory.globalInstructions.resetConfirm"))) return;
    pending.current = true;
    setBusy(true);
    setError(null);
    try {
      const value = await memoryRepository.resetGlobalInstructions();
      setSaved(value);
      setContent(value.content);
      setEnabled(value.enabled);
    } catch {
      setError("reset");
    } finally {
      pending.current = false;
      setBusy(false);
    }
  }

  return (
    <div className="settings-shell global-instructions-page">
      <div className="page-heading-row">
        <div className="page-intro">
          <h2><Brain size={20} />{t("pages.memory.title")}</h2>
          <p>{t("pages.memory.description")}</p>
        </div>
      </div>

      <Card className="global-instructions-card">
        <div className="global-instructions-card__heading">
          <div>
            <h3>{t("pages.memory.globalInstructions.title")}</h3>
            <p>{t("pages.memory.globalInstructions.description")}</p>
          </div>
          <span className={`status-chip ${enabled ? "" : "status-chip--muted"}`}>
            {enabled
              ? t("pages.memory.globalInstructions.enabled")
              : t("pages.memory.globalInstructions.disabled")}
          </span>
        </div>

        <label className="global-instructions-toggle">
          <input
            type="checkbox"
            checked={enabled}
            disabled={saved === null || busy}
            onChange={(event) => setEnabled(event.target.checked)}
          />
          <span>{t("pages.memory.globalInstructions.enabledLabel")}</span>
        </label>

        <textarea
          className="global-instructions-editor"
          value={content}
          maxLength={MAX_GLOBAL_INSTRUCTION_CHARS}
          disabled={saved === null || busy}
          placeholder={t("pages.memory.globalInstructions.placeholder")}
          onChange={(event) => setContent(event.target.value)}
          aria-label={t("pages.memory.globalInstructions.title")}
        />

        <div className="global-instructions-meta">
          <span>{t("pages.memory.globalInstructions.scope")}</span>
          <span>{t("pages.memory.globalInstructions.characters", {
            count: content.length, max: MAX_GLOBAL_INSTRUCTION_CHARS,
          })}</span>
          <span>{saved ? t("pages.memory.globalInstructions.revision", { revision: saved.revision }) : ""}</span>
        </div>

        <p className="global-instructions-boundary">
          {t("pages.memory.globalInstructions.boundary")}
        </p>

        <div className="global-instructions-actions">
          <Button
            icon={<RotateCcw size={14} />}
            disabled={saved === null || busy}
            onClick={() => void reset()}
          >
            {t("pages.memory.globalInstructions.reset")}
          </Button>
          <Button
            variant="primary"
            icon={<Save size={14} />}
            disabled={saved === null || busy || !dirty}
            onClick={() => void save()}
          >
            {busy ? t("actions.saving") : t("actions.save")}
          </Button>
        </div>
      </Card>

      {error ? (
        <p role="alert" className="state-message state-message--error">
          {t(`pages.memory.globalInstructions.errors.${error}`)}
          {error === "load" ? <Button disabled={busy} onClick={() => void load()}>{t("actions.retry")}</Button> : null}
        </p>
      ) : null}
    </div>
  );
}
