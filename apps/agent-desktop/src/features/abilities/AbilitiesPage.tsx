import { RefreshCw, Sparkles, FolderPlus } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Button } from "../../components/ui/Button";
import { Card } from "../../components/ui/Card";
import { skillRepository } from "../../services/runtime";
import type { Skill, SkillDetail, SkillDraft } from "../../types/skills";
import "./skills.css";

export function AbilitiesPage() {
  const { t } = useTranslation("common");
  const [skills, setSkills] = useState<Skill[]>([]);
  const [detail, setDetail] = useState<SkillDetail | null>(null);
  const [draft, setDraft] = useState<SkillDraft | null>(null);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [refreshFailed, setRefreshFailed] = useState(false);
  const pending = useRef(false);
  useEffect(() => {
    let active = true;
    void skillRepository.list().then((items) => { if (active) setSkills(items); })
      .catch((reason: unknown) => { if (active) setError(String(reason)); })
      .finally(() => { if (active) setBusy(false); });
    return () => { active = false; };
  }, []);
  async function act(action: () => Promise<void>) {
    if (pending.current) return;
    pending.current = true; setBusy(true); setError(null); setRefreshFailed(false);
    try { await action(); } catch (reason) { setError(String(reason)); }
    finally { pending.current = false; setBusy(false); }
  }
  return <div className="settings-shell skills-page">
    <div className="page-heading-row">
      <div className="page-intro"><h2><Sparkles size={20} />{t("pages.abilities.title")}</h2>
        <p>{t("skills.description")}</p></div>
      <div className="skills-actions">
        <Button disabled={busy || draft !== null} onClick={() => setDraft({ name: "", description: "", content: "" })}>{t("skills.create")}</Button>
        <Button disabled={busy} icon={<RefreshCw size={14} />} onClick={() => void act(async () => {
          setSkills(await skillRepository.list()); setDetail(null);
        })}>{t("skills.refresh")}</Button>
        <Button disabled={busy} variant="primary" icon={<FolderPlus size={14} />} onClick={() => void act(async () => {
          const imported = await skillRepository.importDirectory();
          if (imported) { setSkills(await skillRepository.list()); setDetail(null); }
        })}>{t("skills.import")}</Button>
      </div>
    </div>
    <p className="skills-help">{t("skills.help")}</p>
    {draft ? <Card className="skill-card skill-editor">
      <h3>{t(draft.expected_revision ? "skills.edit" : "skills.create")}</h3>
      <label>{t("skills.name")}<input aria-label={t("skills.name")} value={draft.name} disabled={busy || Boolean(draft.expected_revision)} maxLength={64}
        onChange={(e) => setDraft({ ...draft, name: e.target.value })} /></label>
      <label>{t("skills.purpose")}<textarea aria-label={t("skills.purpose")} value={draft.description} disabled={busy} maxLength={2000}
        onChange={(e) => setDraft({ ...draft, description: e.target.value })} /></label>
      <label>{t("skills.instructions")}<textarea aria-label={t("skills.instructions")} className="skill-editor__body" value={draft.content} disabled={busy} maxLength={64000}
        onChange={(e) => setDraft({ ...draft, content: e.target.value })} /></label>
      <div className="skills-actions"><Button variant="primary" disabled={busy || !/^[a-z0-9][a-z0-9-]{0,63}$/.test(draft.name) || !draft.description.trim() || !draft.content.trim()}
        onClick={() => void act(async () => {
          const saved = await skillRepository.save(draft);
          setSkills((items) => [...items.filter((item) => item.id !== saved.id), saved]);
          setDetail(saved); setDraft(null);
          try { setSkills(await skillRepository.list()); }
          catch { setRefreshFailed(true); setError(t("buttonAudit.skillRefreshFailed")); }
        })}>{t("skills.save")}</Button>
        <Button disabled={busy} onClick={() => setDraft(null)}>{t("skills.cancel")}</Button></div>
      <p className="skills-help">{t("skills.editHelp")}</p>
    </Card> : null}
    {error ? <p role="alert" className="state-message state-message--error">{refreshFailed ? null : t("skills.error")} {error}</p> : null}
    {!skills.length && !busy ? <p>{t("skills.empty")}</p> : null}
    {skills.map((skill) => <Card key={skill.id} className="skill-card">
      <div className="skill-card__heading"><h3>{skill.name}</h3>
        <label><input type="checkbox" checked={skill.enabled} disabled={busy}
          aria-label={t("skills.enable", { name: skill.name })}
          onChange={(event) => { const enabled = event.target.checked; void act(async () => {
            setSkills(await skillRepository.setEnabled(skill.id, enabled)); setDetail(null);
          }); }} /> {t("skills.enabled")}</label></div>
      <p>{skill.description}</p><small className="skill-path">{skill.path}</small>
      <p className="skills-help">{t(skill.implicit ? "skills.automatic" : "skills.explicit", { name: skill.name })}</p>
      {skill.error ? <p role="alert">{skill.error}</p> : null}
      <div className="skills-actions">
        <Button disabled={busy || Boolean(skill.error) || draft !== null} onClick={() => void act(async () => {
          const current = await skillRepository.inspect(skill.name);
          setDraft({ name: current.name, description: current.description, content: current.content, expected_revision: current.revision });
        })}>{t("skills.edit")}</Button>
        <Button disabled={busy || Boolean(skill.error)} onClick={() => void act(async () => {
          setDetail(await skillRepository.inspect(skill.name));
        })}>{t("skills.view")}</Button>
        <Button disabled={busy || draft?.name === skill.name} title={draft?.name === skill.name ? t("buttonAudit.finishEditing") : undefined} onClick={() => void act(async () => {
          setSkills(await skillRepository.remove(skill.id)); setDetail(null);
        })}>{t("skills.remove")}</Button>
      </div>
      {detail?.id === skill.id ? <pre className="skill-source" tabIndex={0}>{detail.content}</pre> : null}
    </Card>)}
  </div>;
}
