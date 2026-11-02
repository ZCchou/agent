import type { ChangeSetPlan } from "../../types/domain";

// This describes the visible approval only. Host remains the authority for the
// immutable plan, current filesystem preconditions and dependency validation.
export function changeSetSelection(plan: ChangeSetPlan | null | undefined, requested?: readonly string[]) {
  if (!plan?.plan_id || !plan.plan_hash || !Array.isArray(plan.steps) || !plan.steps.length) return null;
  const steps = new Map(plan.steps.map((step) => [step.operation_id, step]));
  if (steps.size !== plan.steps.length || plan.steps.some((step) => !step.operation_id || !Array.isArray(step.depends_on))) return null;
  const required = new Set(plan.steps.flatMap((step) => step.depends_on));
  const defaults = plan.steps.filter((step) => !required.has(step.operation_id)).map((step) => step.operation_id);
  const explicit = requested ? [...new Set(requested)] : defaults;
  const visited = new Set<string>();
  const visiting = new Set<string>();
  function visit(id: string): boolean {
    if (visited.has(id)) return true;
    if (visiting.has(id)) return false;
    const step = steps.get(id);
    if (!step) return false;
    visiting.add(id);
    if (!step.depends_on.every(visit)) return false;
    visiting.delete(id);
    visited.add(id);
    return true;
  }
  // Validate even unselected branches so a malformed plan cannot look usable.
  if (!plan.steps.every((step) => visit(step.operation_id))) return null;
  visited.clear();
  if (!explicit.every(visit)) return null;
  const dependencies = new Set([...visited].flatMap((id) => steps.get(id)!.depends_on));
  return { explicit, approved: [...visited].sort(), required: [...dependencies] };
}
