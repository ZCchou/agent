import { Gauge, Layers3 } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Button } from "../../components/ui/Button";
import type { ExecutionPlan } from "../../types/domain";

interface Props {
  plan: ExecutionPlan;
  busy: boolean;
  onApprove(): void;
  onReject(): void;
}

export function RoutingBudgetPrompt({ plan, busy, onApprove, onReject }: Props) {
  const { t } = useTranslation("chat");
  const extraSteps = plan.steps.filter((step) => step.phase !== "synthesis");
  return <section className="routing-budget-prompt" role="status">
    <div className="routing-budget-prompt__icon"><Gauge size={18} /></div>
    <div className="routing-budget-prompt__body">
      <strong>{t("routing.budgetTitle")}</strong>
      <p>{t("routing.budgetDescription", { count: extraSteps.length })}</p>
      <ul>
        {extraSteps.map((step) => <li key={step.id}>
          <Layers3 size={13} />
          <span>{t(`routing.steps.${step.step_type}`)}</span>
          <small>{step.model_id}</small>
        </li>)}
      </ul>
      <p className="routing-budget-prompt__cost">{plan.estimated_extra_online_cost_usd === null
        ? t("routing.unknownCost")
        : t("routing.estimatedExtraCost", { value: plan.estimated_extra_online_cost_usd })}</p>
    </div>
    <div className="routing-budget-prompt__actions">
      <Button variant="ghost" disabled={busy} onClick={onReject}>{t("routing.reject")}</Button>
      <Button variant="primary" disabled={busy} onClick={onApprove}>
        {busy ? t("routing.resuming") : t("routing.approve")}
      </Button>
    </div>
  </section>;
}
