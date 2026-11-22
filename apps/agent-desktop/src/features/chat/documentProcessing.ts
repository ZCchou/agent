const countedProgressKeys = new Map([
  ["reranking", "attachments.processing.reranking"],
  ["template_planning", "attachments.processing.templatePlanning"],
  ["template_filling", "attachments.processing.templateFilling"],
]);

export function documentProcessingLabel(
  data: Record<string, unknown>,
  translate: (key: string, values: Record<string, string | number>) => string,
): string | null {
  const publicationKey = new Map([
    ['review_report_generated', 'attachments.processing.reportGenerated'],
    ['review_report_publishing', 'attachments.processing.reportPublishing'],
    ['review_report_verified', 'attachments.processing.reportVerified'],
  ]).get(String(data.phase));
  if (publicationKey) return translate(publicationKey, { name: typeof data.file_name === 'string' ? data.file_name : '' }).trim();
  if (data.phase === 'preparing_rerank') return translate('routing.status.preparing_rerank', {});
  if (data.phase === 'model_loading') {
    const role = data.model_role;
    const key = role === 'embedding' ? 'loadingEmbeddingModel'
      : role === 'reranker' ? 'loadingRerankerModel' : 'loadingAuxiliaryModel';
    return translate(`routing.status.${key}`, { model: String(data.model_name ?? '') });
  }
  if (data.phase === "parsing") {
    return translate("attachments.processing.parsing", { name: String(data.name ?? "") });
  }
  if (data.phase === "ocr") {
    return translate("attachments.processing.ocr", {
      page: Number(data.page ?? 0), total: Number(data.total_pages ?? 0),
    });
  }
  const phase = data.document_phase ?? data.phase;
  if (phase === 'scope_selection' || phase === 'document_review') {
    const { completed, total } = data;
    if (typeof completed !== 'number' || typeof total !== 'number' || !Number.isSafeInteger(completed)
        || !Number.isSafeInteger(total) || completed < 0 || total < 1 || completed > total) return null;
    return translate(phase === 'scope_selection' ? 'attachments.processing.scopeSelection'
      : 'attachments.processing.documentReview', { completed, total });
  }
  const progressKey = countedProgressKeys.get(String(data.phase));
  if (progressKey) {
    return translate(progressKey, {
      completed: Number(data.completed ?? 0), total: Number(data.total ?? 0),
    });
  }
  return null;
}
