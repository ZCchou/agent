import type { RagRepository } from "../../services/repositories/RagRepository";
import type { RagJob, RagLibrary, RagModelList } from "../../types/rag";

type PageSource = Pick<RagRepository, "listModels" | "listLibraries" | "listJobs">;
interface PageData {
  projectId: string;
  models: RagModelList;
  libraries: RagLibrary[];
  jobs: RagJob[];
}

// Display snapshots are scoped to one backend and project. Every action still
// reaches the backend for current authorization and version checks.
const snapshots = new WeakMap<PageSource, { revision: number; data: PageData | null }>();

export function readRagPageData(source: PageSource, projectId: string): PageData | null {
  const data = snapshots.get(source)?.data;
  return data?.projectId === projectId ? data : null;
}

export async function loadRagPageData(source: PageSource, projectId: string): Promise<PageData> {
  let state = snapshots.get(source);
  if (!state) {
    state = { revision: 0, data: null };
    snapshots.set(source, state);
  }
  const revision = ++state.revision;
  const [models, libraryList, jobList, bindings] = await Promise.all([
    source.listModels(), source.listLibraries(), source.listJobs(),
    projectId ? source.listLibraries(projectId) : Promise.resolve({ libraries: [] as RagLibrary[] }),
  ]);
  const data: PageData = {
    projectId, models, jobs: jobList.jobs,
    libraries: libraryList.libraries.map((library) => {
      const binding = bindings.libraries.find((item) => item.id === library.id && item.enabled);
      return { ...library, project_ids: binding ? [projectId] : [], bound_index_id: binding?.index_id ?? null };
    }),
  };
  if (state.revision === revision) state.data = data;
  return data;
}
