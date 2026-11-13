import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import type { RoutingPreferences } from "../../types/settings";
import { idleMinutes, intentTimeout } from "./routingDurations";

function ResidenceInput({ seconds, label, onChange }: { seconds: number; label: string; onChange(seconds: number): Promise<void> }) {
  const { t } = useTranslation("common");
  const presets = [300, 900, 1800, 3600];
  const [custom, setCustom] = useState(!presets.includes(seconds) && seconds !== 0);
  const [minutes, setMinutes] = useState(String(seconds / 60));
  const [busy, setBusy] = useState(false);
  useEffect(() => { setMinutes(String(seconds / 60)); }, [seconds]);
  const save = async (value: number) => {
    setBusy(true);
    try { await onChange(value); } finally { setBusy(false); }
  };
  return <label><span>{label}</span><select disabled={busy} value={custom ? "custom" : String(seconds)} onChange={(event) => {
    const value = event.target.value;
    setCustom(value === "custom");
    if (value === "custom") { setMinutes(String(Math.max(1, seconds / 60))); return; }
    void save(Number(value));
  }}>
    <option value="0">{t("routingDuration.manual")}</option>
    {presets.map((value) => <option key={value} value={value}>{t("routingDuration.minutes", { count: value / 60 })}</option>)}
    <option value="custom">{t("routingDuration.custom")}</option>
  </select>{custom ? <input aria-label={t("routingDuration.customMinutes")} type="number" min={1} max={1440} step={1}
    disabled={busy} value={minutes} onChange={(event) => setMinutes(event.target.value)} onBlur={() => {
      const value = idleMinutes(minutes);
      if (value === null || value === 0) { setMinutes(String(Math.max(1, seconds / 60))); return; }
      void save(value);
    }} onKeyDown={(event) => { if (event.key === "Enter") event.currentTarget.blur(); }} /> : null}</label>;
}

export function RoutingDurationSettings({ value, onChange }: {
  value: RoutingPreferences; onChange(next: Partial<RoutingPreferences>): Promise<void>;
}) {
  const { t } = useTranslation("common");
  const timeout = value.intent_timeout_seconds ?? 90;
  const [input, setInput] = useState(String(timeout));
  useEffect(() => { setInput(String(timeout)); }, [timeout]);
  return <>
    <label><span>{t("routingDuration.timeout")}</span><input type="number" min={30} max={180} step={1} value={input}
      onChange={(event) => setInput(event.target.value)} onBlur={() => {
        const seconds = intentTimeout(input);
        if (seconds === null) { setInput(String(timeout)); return; }
        void onChange({ intent_timeout_seconds: seconds });
      }} onKeyDown={(event) => { if (event.key === "Enter") event.currentTarget.blur(); }} />
      <small>{t("routingDuration.budget", { seconds: 3 * timeout })}</small></label>
    <ResidenceInput seconds={value.main_model_idle_seconds ?? 1800} label={t("routingDuration.main")}
      onChange={(seconds) => onChange({ main_model_idle_seconds: seconds })} />
    <ResidenceInput seconds={value.auxiliary_model_idle_seconds ?? 1800} label={t("routingDuration.auxiliary")}
      onChange={(seconds) => onChange({ auxiliary_model_idle_seconds: seconds })} />
    <p>{t("routingDuration.residenceHint")}</p>
  </>;
}
