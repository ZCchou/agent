import { useState } from "react";
import { useTranslation } from "react-i18next";
import type { CompactionTargetRequest } from "../../types/domain";
import type { RoutingModelOption } from "./ConversationRoutingControl";

export function CompactionRecoveryControl({ options, disabled, needsNewTarget, onResume }: {
  options: RoutingModelOption[];
  disabled: boolean;
  needsNewTarget: boolean;
  onResume(target?: CompactionTargetRequest): void;
}) {
  const { t } = useTranslation("chat");
  const [value, setValue] = useState("");
  const option = options.find((item) => item.value === value);
  return <div className="context-compaction-recovery">
    <label>{t("context.recoveryModel")}
      <select aria-label={t("context.recoveryModel")} value={value} disabled={disabled}
        onChange={(event) => setValue(event.target.value)}>
        <option value="">{t("context.previousRecoveryModel")}</option>
        {options.filter(option => !option.unavailable).map((item) => <option key={item.value} value={item.value}>{item.label} · {item.modelId}</option>)}
      </select>
    </label>
    <button type="button" disabled={disabled || (needsNewTarget && !option)} onClick={() => onResume(option
      ? { provider_profile_id: option.providerId, model_id: option.modelId } : undefined)}>{t("context.resumeCompaction")}</button>
  </div>;
}
