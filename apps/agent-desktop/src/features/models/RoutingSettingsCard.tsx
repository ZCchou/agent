import { RoutingDurationSettings } from "./RoutingDurationSettings";
import { Route } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Card } from "../../components/ui/Card";
import type { RoutingPreferences } from "../../types/settings";

export function RoutingSettingsCard({ value, onChange }: {
  value: RoutingPreferences;
  onChange(next: Partial<RoutingPreferences>): Promise<void>;
}) {
  const { t } = useTranslation("common");
  const [calls, setCalls] = useState(String(value.max_model_calls_per_message));
  const [specialists, setSpecialists] = useState(String(value.max_specialist_steps));
  const [cost, setCost] = useState(value.max_extra_online_cost_usd);
  const [saving, setSaving] = useState(false);
  const saveLock = useRef(false);

  useEffect(() => { setCalls(String(value.max_model_calls_per_message)); }, [value.max_model_calls_per_message]);
  useEffect(() => { setSpecialists(String(value.max_specialist_steps)); }, [value.max_specialist_steps]);
  useEffect(() => { setCost(value.max_extra_online_cost_usd); }, [value.max_extra_online_cost_usd]);

  const commit = async (field: "calls" | "specialists" | "cost") => {
    if (saveLock.current) return;
    const callBudget = Number(calls);
    const specialistBudget = Number(specialists);
    const costBudget = Number(cost);
    if (!Number.isInteger(callBudget) || callBudget < 1 || callBudget > 6
      || !Number.isInteger(specialistBudget) || specialistBudget < 0 || specialistBudget > 2
      || !Number.isFinite(costBudget) || costBudget < 0 || costBudget > 5) {
      setCalls(String(value.max_model_calls_per_message));
      setSpecialists(String(value.max_specialist_steps));
      setCost(value.max_extra_online_cost_usd);
      return;
    }
    saveLock.current = true; setSaving(true);
    try {
      await onChange(field === "calls" ? { max_model_calls_per_message: callBudget }
        : field === "specialists" ? { max_specialist_steps: specialistBudget }
          : { max_extra_online_cost_usd: costBudget.toFixed(2) });
    } finally { saveLock.current = false; setSaving(false); }
  };

  return <Card className="models-routing-card">
    <div className="models-default-card__copy">
      <span className="models-default-card__icon"><Route size={21} /></span>
      <div><h2>{t("models.routing.title")}</h2><p>{t("models.routing.description")}</p></div>
    </div>
    <div className="models-routing-card__controls">
      <RoutingDurationSettings value={value} onChange={onChange} />
      <label><span>{t("models.routing.preset")}</span><select value={value.default_preset}
        onChange={(event) => void onChange({ default_preset: event.target.value as RoutingPreferences["default_preset"] })}>
        <option value="balanced">{t("models.routing.balanced")}</option>
        <option value="quality">{t("models.routing.quality")}</option>
        <option value="economy">{t("models.routing.economy")}</option>
        <option value="local_first">{t("models.routing.localFirst")}</option>
      </select></label>
      <label><span>{t("models.routing.callBudget")}</span><input type="number" min={1} max={6}
        disabled={saving} value={calls} onChange={(event) => setCalls(event.target.value)} onBlur={() => void commit("calls")}
        onKeyDown={(event) => { if (event.key === "Enter") event.currentTarget.blur(); }} /></label>
      <label><span>{t("models.routing.specialistBudget")}</span><input type="number" min={0} max={2}
        disabled={saving} value={specialists} onChange={(event) => setSpecialists(event.target.value)} onBlur={() => void commit("specialists")}
        onKeyDown={(event) => { if (event.key === "Enter") event.currentTarget.blur(); }} /></label>
      <label><span>{t("models.routing.extraCost")}</span><input type="number" min={0} max={5} step="0.01"
        disabled={saving} value={cost} onChange={(event) => setCost(event.target.value)} onBlur={() => void commit("cost")}
        onKeyDown={(event) => { if (event.key === "Enter") event.currentTarget.blur(); }} /></label>
      <label className="settings-switch"><input type="checkbox" checked={value.allow_unknown_price_automatic}
        onChange={(event) => void onChange({ allow_unknown_price_automatic: event.target.checked })} />
        <span>{t("models.routing.allowUnknownPrice")}</span></label>
    </div>
  </Card>;
}
