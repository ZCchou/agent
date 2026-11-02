import { ChevronDown } from "lucide-react";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";
import type { ProviderModelSettings } from "../../types/settings";
import { Button } from "../../components/ui/Button";

interface Props {
  model: ProviderModelSettings;
  onSave(contextLength: number | null, maxOutputTokens: number | null): Promise<void>;
  onSaveRouting(inputPrice: string | null, outputPrice: string | null,
    qualityScore: number | null, priority: number): Promise<void>;
}

function optionalPositiveInteger(value: string): number | null {
  if (!value.trim()) return null;
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed > 0 ? parsed : Number.NaN;
}

export function ModelContextLimits({ model, onSave, onSaveRouting }: Props) {
  const { t } = useTranslation("common");
  const [open, setOpen] = useState(false);
  const [windowValue, setWindowValue] = useState(model.capabilities.context_length?.toString() ?? "");
  const [outputValue, setOutputValue] = useState(model.capabilities.max_output_tokens?.toString() ?? "");
  const [saving, setSaving] = useState(false);
  const saveLock = useRef(false);
  const [error, setError] = useState(false);
  const [inputPrice, setInputPrice] = useState(model.capabilities.input_price_per_million ?? "");
  const [outputPrice, setOutputPrice] = useState(model.capabilities.output_price_per_million ?? "");
  const [qualityScore, setQualityScore] = useState(model.capabilities.routing_quality_score?.toString() ?? "");
  const [priority, setPriority] = useState(model.capabilities.routing_priority?.toString() ?? "0");

  useEffect(() => {
    if (open || saveLock.current) return;
    setWindowValue(model.capabilities.context_length?.toString() ?? "");
    setOutputValue(model.capabilities.max_output_tokens?.toString() ?? "");
    setInputPrice(model.capabilities.input_price_per_million ?? "");
    setOutputPrice(model.capabilities.output_price_per_million ?? "");
    setQualityScore(model.capabilities.routing_quality_score?.toString() ?? "");
    setPriority(model.capabilities.routing_priority?.toString() ?? "0");
  }, [open, model.id, model.capabilities.context_length, model.capabilities.max_output_tokens,
    model.capabilities.input_price_per_million, model.capabilities.output_price_per_million,
    model.capabilities.routing_quality_score, model.capabilities.routing_priority]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (saveLock.current) return;
    const contextLength = optionalPositiveInteger(windowValue);
    const maxOutputTokens = optionalPositiveInteger(outputValue);
    const quality = qualityScore.trim() ? Number(qualityScore) : null;
    const preference = Number(priority || 0);
    const priceValid = [inputPrice, outputPrice].every((value) =>
      !value.trim() || (Number.isFinite(Number(value)) && Number(value) >= 0));
    if (Number.isNaN(contextLength) || Number.isNaN(maxOutputTokens) || !priceValid
      || (quality !== null && (!Number.isInteger(quality) || quality < 0 || quality > 100))
      || !Number.isInteger(preference) || preference < -10 || preference > 10) return;
    saveLock.current = true; setSaving(true); setError(false);
    try {
      await onSave(contextLength, maxOutputTokens);
      await onSaveRouting(inputPrice.trim() || null, outputPrice.trim() || null, quality, preference);
      setOpen(false);
    } catch {
      setError(true);
    } finally {
      saveLock.current = false; setSaving(false);
    }
  }

  return <div className="model-context-limits">
    <button type="button" disabled={saving} onClick={() => setOpen((value) => !value)} aria-expanded={open}>
      <span><ChevronDown size={15} aria-hidden="true" />{t("models.advancedParameters")}</span>
      <small>{model.capabilities.context_length
        ? t("models.contextKnown", { count: model.capabilities.context_length })
        : t("models.contextUnknown")} · {t(`models.contextSource.${model.capabilities.context_length_source}`)}</small>
    </button>
    {open ? <form onSubmit={(event) => void submit(event)}>
      <label>{t("models.contextLength")}<input disabled={saving} inputMode="numeric" value={windowValue}
        placeholder="8192" onChange={(event) => setWindowValue(event.target.value)} /></label>
      <label>{t("models.maxOutputTokens")}<input disabled={saving} inputMode="numeric" value={outputValue}
        placeholder="2048" onChange={(event) => setOutputValue(event.target.value)} /></label>
      <label>{t("models.inputPrice")}<input disabled={saving} inputMode="decimal" value={inputPrice}
        placeholder="0.00" onChange={(event) => setInputPrice(event.target.value)} /></label>
      <label>{t("models.outputPrice")}<input disabled={saving} inputMode="decimal" value={outputPrice}
        placeholder="0.00" onChange={(event) => setOutputPrice(event.target.value)} /></label>
      <label>{t("models.qualityScore")}<input disabled={saving} type="number" min={0} max={100} value={qualityScore}
        placeholder="50" onChange={(event) => setQualityScore(event.target.value)} /></label>
      <label>{t("models.routingPriority")}<input disabled={saving} type="number" min={-10} max={10} value={priority}
        onChange={(event) => setPriority(event.target.value)} /></label>
      <small>{t("models.contextLimitHint")}</small>
      {error ? <p role="alert">{t("buttonAudit.saveFailed")}</p> : null}
      <Button type="submit" disabled={saving}>{saving ? t("actions.saving") : t("actions.save")}</Button>
    </form> : null}
  </div>;
}
