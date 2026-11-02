import { renderToStaticMarkup } from "react-dom/server";
import { expect, it, vi } from "vitest";
import fixture from "../../../../../contracts/reliability-repair-fixtures.json";
import type { ChangeSet } from "../../types/domain";

vi.mock("../../services/runtime", () => ({ changeSetRepository: {} }));
vi.mock("react-i18next", () => ({ useTranslation: () => ({ t: (key: string) => key }) }));
import { ChangeSetCard } from "./ChangeSetCard";

it.each(["completed", "rolled_back", "recovery_required"] as const)(
  "%s displays only the approved execution subset while retaining the original fingerprint", (status) => {
    const changeSet = { ...fixture.change_set, status, recovery_affected_paths: [],
      mutations: [fixture.change_set.plan.steps[0].mutation] } as ChangeSet;
    const markup = renderToStaticMarkup(<ChangeSetCard changeSet={changeSet} onResolved={() => {}} expanded />);
    expect(markup).toContain("文档");
    expect(markup).not.toContain("文档/notes.txt");
    expect(markup).toContain(fixture.change_set.plan.plan_hash);
  },
);

it("waiting approval displays the full immutable plan", () => {
  const changeSet = { ...fixture.change_set, status: "waiting_user", recovery_affected_paths: [],
    mutations: [fixture.change_set.plan.steps[0].mutation] } as ChangeSet;
  const markup = renderToStaticMarkup(<ChangeSetCard changeSet={changeSet} onResolved={() => {}} expanded />);
  expect(markup).toContain("文档/notes.txt");
  expect(markup).toContain(fixture.change_set.plan.plan_hash);
});
