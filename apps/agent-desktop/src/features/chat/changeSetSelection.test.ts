import { expect, it } from "vitest";
import type { ChangeSetPlan } from "../../types/domain";
import { changeSetSelection } from "./changeSetSelection";

it("marks a directly selected parent as required when its child is reselected", () => {
  const plan: ChangeSetPlan = {
    plan_id: "plan", plan_hash: "hash", policy_version: 1, root_generation: 1, effective_risk: "routine",
    steps: [
      { operation_id: "dir", mutation: { kind: "mkdir", path: "docs" }, depends_on: [], preconditions: {} },
      { operation_id: "write", mutation: { kind: "create_text", path: "docs/readme.txt", content: "ok" },
        depends_on: ["dir"], preconditions: {} },
    ],
  };
  expect(changeSetSelection(plan)?.required).toEqual(["dir"]);
  expect(changeSetSelection(plan, [])?.approved).toEqual([]);
  expect(changeSetSelection(plan, ["dir"])?.required).toEqual([]);
  const parentAndChild = changeSetSelection(plan, ["dir", "write"]);
  expect(parentAndChild?.approved).toEqual(["dir", "write"]);
  expect(parentAndChild?.required).toEqual(["dir"]);
  // Removing the child still preserves the user's independent parent choice.
  expect(changeSetSelection(plan, parentAndChild!.explicit.filter((id) => id !== "write")))
    .toEqual({ explicit: ["dir"], approved: ["dir"], required: [] });
});
