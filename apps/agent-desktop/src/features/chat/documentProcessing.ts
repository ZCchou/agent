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
  return null;
}
