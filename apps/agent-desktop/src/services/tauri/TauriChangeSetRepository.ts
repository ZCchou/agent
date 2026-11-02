import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import type { ChangeSet } from "../../types/domain";
import type { ChangeSetRepository, ChangeSetResolution } from "../repositories/ChangeSetRepository";

export class TauriChangeSetRepository implements ChangeSetRepository {
  list(): Promise<ChangeSet[]> {
    return invoke("list_agent_change_sets");
  }

  resolve(changeSetId: string, resolution: ChangeSetResolution): Promise<void> {
    return invoke("resolve_agent_change_set", { changeSetId, approve: resolution.approve,
      planId: resolution.planId, planHash: resolution.planHash, approvedOperationIds: resolution.approvedOperationIds });
  }

  undo(changeSetId: string): Promise<string> {
    return invoke("undo_agent_change_set", { changeSetId });
  }

  recover(changeSetId: string): Promise<ChangeSet> {
    return invoke("recover_agent_change_set", { changeSetId });
  }

  subscribe(listener: (changeSet: ChangeSet) => void): Promise<() => void> {
    return listen<ChangeSet>("agent-change-set", (event) => listener(event.payload));
  }
}
