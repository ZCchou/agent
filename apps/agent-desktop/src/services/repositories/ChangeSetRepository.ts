import type { ChangeSet } from "../../types/domain";

export interface ChangeSetResolution {
  approve: boolean;
  planId: string;
  planHash: string;
  approvedOperationIds: string[];
}

export interface ChangeSetRepository {
  list(): Promise<ChangeSet[]>;
  resolve(changeSetId: string, resolution: ChangeSetResolution): Promise<void>;
  undo(changeSetId: string): Promise<string>;
  recover(changeSetId: string): Promise<ChangeSet>;
  subscribe(listener: (changeSet: ChangeSet) => void): Promise<() => void>;
}
