const countedProgressKeys = new Map([
  ["reranking", "attachments.processing.reranking"],
  ["model_loading", "attachments.processing.modelLoading"],
  ["template_planning", "attachments.processing.templatePlanning"],
  ["template_filling", "attachments.processing.templateFilling"],
]);

export function documentProcessingLabel(
  data: Record<string, unknown>,
  translate: (key: string, values: Record<string, string | number>) => string,
): string | null {
  if (data.phase === "parsing") {
    return translate("attachments.processing.parsing", { name: String(data.name ?? "") });
  }
  if (data.phase === "ocr") {
    return translate("attachments.processing.ocr", {
      page: Number(data.page ?? 0), total: Number(data.total_pages ?? 0),
    });
  }
  const progressKey = countedProgressKeys.get(String(data.phase));
  if (progressKey) {
    return translate(progressKey, {
      completed: Number(data.completed ?? 0), total: Number(data.total ?? 0),
    });
  }
  return null;
}
