import { describe, expect, it } from "vitest";
import { INITIAL_PROJECT_NAVIGATION, projectNavigationReducer } from "./projectNavigation";

describe("project navigation intent", () => {
  it("consumes a new-project request instead of replaying it after remount", () => {
    const requested = projectNavigationReducer(INITIAL_PROJECT_NAVIGATION, { type: "request_new" });
    const consumed = projectNavigationReducer(requested, { type: "consume_new" });

    expect(requested.newProjectRequested).toBe(true);
    expect(consumed.newProjectRequested).toBe(false);
    expect(projectNavigationReducer(consumed, { type: "view", projectId: "project-1" }))
      .toEqual({ viewingProjectId: "project-1", newProjectRequested: false });
  });

  it("explicit navigation clears an unconsumed stale request", () => {
    const requested = projectNavigationReducer(INITIAL_PROJECT_NAVIGATION, { type: "request_new" });

    expect(projectNavigationReducer(requested, { type: "view", projectId: "project-2" }))
      .toEqual({ viewingProjectId: "project-2", newProjectRequested: false });
  });
});
