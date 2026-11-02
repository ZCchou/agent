export interface ProjectNavigationState {
  viewingProjectId: string | null;
  newProjectRequested: boolean;
}

export type ProjectNavigationAction =
  | { type: "view"; projectId: string | null }
  | { type: "request_new" }
  | { type: "consume_new" };

export const INITIAL_PROJECT_NAVIGATION: ProjectNavigationState = {
  viewingProjectId: null,
  newProjectRequested: false,
};

export function projectNavigationReducer(
  state: ProjectNavigationState,
  action: ProjectNavigationAction,
): ProjectNavigationState {
  switch (action.type) {
    case "request_new":
      return { viewingProjectId: null, newProjectRequested: true };
    case "consume_new":
      return { ...state, newProjectRequested: false };
    case "view":
      return { viewingProjectId: action.projectId, newProjectRequested: false };
  }
}
