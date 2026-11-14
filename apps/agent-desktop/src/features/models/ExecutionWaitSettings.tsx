import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import type { ExecutionWaitPolicy } from "../../types/settings";
import { defaultExecutionWaitPolicy, executionWaitNumber } from "./executionWaitPolicy";

function WaitNumber({ value, label, minimum, maximum, disabled, onChange }: {
  value: number; label: string; minimum: number; maximum: number; disabled: boolean;
  onChange(value: number): Promise<void>;
}) {
  const [input, setInput] = useState(String(value));
  useEffect(() => setInput(String(value)), [value]);
  return <label><span>{label}</span><input aria-label={label} type="number" min={minimum} max={maximum}
    step={1} disabled={disabled} value={input} onChange={(event) => setInput(event.target.value)}
    onBlur={() => {
      const next = executionWaitNumber(input, minimum, maximum);
      if (next === null) setInput(String(value));
      else if (next !== value) void onChange(next);
    }} onKeyDown={(event) => { if (event.key === "Enter") event.currentTarget.blur(); }} /></label>;
}

export function ExecutionWaitSettings({ value, onChange }: {
  value?: ExecutionWaitPolicy; onChange(value: ExecutionWaitPolicy): Promise<void>;
}) {
  const { t } = useTranslation("common");
  const policy = value ?? defaultExecutionWaitPolicy();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(false);
  async function save(patch: Partial<ExecutionWaitPolicy>) {
    setBusy(true); setError(false);
    try { await onChange({ ...policy, ...patch }); }
    catch { setError(true); }
    finally { setBusy(false); }
  }
  return <fieldset className="execution-wait-settings"><legend>{t("executionWait.title")}</legend>
    <label><span>{t("executionWait.mode")}</span><select disabled={busy} value={policy.mode}
      onChange={(event) => void save({ mode: event.target.value as ExecutionWaitPolicy["mode"] })}>
      <option value="adaptive">{t("executionWait.adaptive")}</option>
      <option value="custom">{t("executionWait.custom")}</option>
    </select></label>
    <WaitNumber value={policy.long_running_notice_seconds} label={t("executionWait.notice")} minimum={30}
      maximum={3600} disabled={busy} onChange={(seconds) => save({ long_running_notice_seconds: seconds })} />
    <WaitNumber value={policy.no_progress_probe_seconds} label={t("executionWait.probe")} minimum={60}
      maximum={3600} disabled={busy} onChange={(seconds) => save({ no_progress_probe_seconds: seconds })} />
    {policy.mode === "custom" ? <>
      <label><input type="checkbox" disabled={busy} checked={policy.max_total_seconds === null}
        onChange={(event) => void save({ max_total_seconds: event.target.checked ? null : 1800 })} />
        {t("executionWait.unlimited")}</label>
      {policy.max_total_seconds !== null ? <WaitNumber value={policy.max_total_seconds / 60}
        label={t("executionWait.total")} minimum={1} maximum={1440} disabled={busy}
        onChange={(minutes) => save({ max_total_seconds: minutes * 60 })} /> : null}
    </> : null}
    <WaitNumber value={policy.max_auto_recoveries} label={t("executionWait.recoveries")} minimum={0}
      maximum={2} disabled={busy} onChange={(count) => save({ max_auto_recoveries: count })} />
    <p>{t("executionWait.hint")}</p>
    {error ? <p role="alert">{t("executionWait.saveError")}</p> : null}
  </fieldset>;
}
