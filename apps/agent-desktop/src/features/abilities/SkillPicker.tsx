import { Sparkles } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { skillRepository } from "../../services/runtime";
import type { Skill } from "../../types/skills";
import "./skills.css";

export function SkillPicker({ disabled, value, onChange }: { disabled: boolean; value: string; onChange(value: string): void }) {
  const { t } = useTranslation("common");
  const [open, setOpen] = useState(false);
  const [skills, setSkills] = useState<Skill[]>([]);
  const [error, setError] = useState(false);
  const [loading, setLoading] = useState(false);
  const [full, setFull] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open || disabled) return;
    let active = true;
    setLoading(true); setError(false);
    void skillRepository.list().then((items) => { if (active) setSkills(items.filter((s) => s.enabled && !s.error)); })
      .catch(() => { if (active) setError(true); })
      .finally(() => { if (active) setLoading(false); });
    const dismiss = (event: MouseEvent) => { if (!root.current?.contains(event.target as Node)) setOpen(false); };
    document.addEventListener("mousedown", dismiss);
    return () => { active = false; document.removeEventListener("mousedown", dismiss); };
  }, [open, disabled]);
  return <div className="skill-picker" ref={root} onKeyDown={(event) => { if (event.key === "Escape") setOpen(false); }}>
    <button type="button" className="chat-composer__icon" disabled={disabled} aria-label={t("skills.choose")}
      title={t("skills.choose")} aria-expanded={open && !disabled} onClick={() => setOpen(!open)}><Sparkles size={18} /></button>
    {open && !disabled ? <div className="skill-picker__menu">
      {loading ? <p>{t("skills.loading")}</p> : error ? <p role="alert">{t("skills.loadError")}</p> : !skills.length ? <p>{t("skills.pickerEmpty")}</p> : skills.map((skill) => (
        <button type="button" key={skill.id} aria-label={skill.name} onClick={() => {
          const token = `$${skill.name}`;
          if (!value.split(/\s+/).includes(token)) {
            if (value.length + token.length + 1 > 20000) { setFull(true); return; }
            onChange(`${token} ${value}`);
          }
          setFull(false);
          setOpen(false);
          root.current?.closest("form")?.querySelector("textarea")?.focus();
        }}><strong>{skill.name}</strong><small>{skill.description}</small></button>
      ))}
      {full ? <p role="alert">{t("skills.draftFull")}</p> : null}
    </div> : null}
  </div>;
}
