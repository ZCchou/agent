import type { LocalModelPreparation } from "../../types/localModels";

interface ModelPreparationRepository {
  prepare(modelId: string, preparationId?: string): Promise<LocalModelPreparation>;
  release(preparationId: string): Promise<unknown>;
}

export async function prepareLocalModel(
  repository: ModelPreparationRepository,
  modelId: string,
  cancelled: () => boolean,
  progress: (state: string, elapsed: number) => void,
  pause = () => new Promise<void>((resolve) => window.setTimeout(resolve, 500)),
): Promise<() => Promise<void>> {
  let preparationId: string | undefined;
  const started = Date.now();
  const release = async () => {
    if (preparationId) {
      const id = preparationId;
      preparationId = undefined;
      await repository.release(id);
    }
  };
  try {
    while (true) {
      if (cancelled()) throw new Error("model_loading_cancelled");
      const result = await repository.prepare(modelId, preparationId);
      preparationId = result.preparation_id;
      if (cancelled()) throw new Error("model_loading_cancelled");
      if (result.state === "failed") {
        const error = result.job?.error;
        throw new Error(typeof error === "object" && error ? `${error.code} · ${error.message}` : String(error ?? "model_loading_failed"));
      }
      progress(result.state, Math.floor((Date.now() - started) / 1000));
      if (result.state === "ready") return release;
      await pause();
    }
  } catch (error) {
    await release().catch(() => undefined);
    throw error;
  }
}


export async function withPreparedLocalModel<T>(
  repository: ModelPreparationRepository,
  providerId: string,
  modelId: string,
  cancelled: () => boolean,
  progress: (state: string, elapsed: number) => void,
  operation: () => Promise<T>,
): Promise<T> {
  const release = providerId.startsWith("managed:")
    ? await prepareLocalModel(repository, modelId, cancelled, progress) : undefined;
  try {
    if (cancelled()) throw new Error("model_loading_cancelled");
    return await operation();
  } finally {
    await release?.().catch(() => undefined);
  }
}
