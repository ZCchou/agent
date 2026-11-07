export type LinkClassification = "external" | "blocked";
export type ImageClassification = "remote_https" | "blocked";
export interface ProjectFileTarget { path: string; lineStart: number | null; lineEnd: number | null }
export interface ProjectDirectoryTarget { path: string }

export function parseRagEvidenceTarget(value: string): string | null {
  const match = /^rag-evidence:(rag-[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$/.exec(value);
  return match && match[0] === value ? match[1] : null;
}

function parseExternalUrl(value: string): URL | null {
  try {
    const parsed = new URL(value);
    if (parsed.username || parsed.password) return null;
    return parsed;
  } catch {
    return null;
  }
}

export function classifyLink(value: string): LinkClassification {
  const parsed = parseExternalUrl(value);
  return parsed && (parsed.protocol === "http:" || parsed.protocol === "https:")
    ? "external"
    : "blocked";
}

export function classifyImage(value: string): ImageClassification {
  const parsed = parseExternalUrl(value);
  return parsed?.protocol === "https:" ? "remote_https" : "blocked";
}

export function parseProjectFileTarget(value: string): ProjectFileTarget | null {
  if (!value || value.includes("?") || value.startsWith("/") || value.startsWith("\\") || /^[a-zA-Z][a-zA-Z0-9+.-]*:/.test(value)) return null;
  const hashIndex = value.indexOf("#");
  const encodedPath = hashIndex < 0 ? value : value.slice(0, hashIndex);
  const fragment = hashIndex < 0 ? "" : value.slice(hashIndex + 1);
  let path: string;
  try { path = decodeURIComponent(encodedPath); } catch { return null; }
  if (!path || path.includes("\\") || path.split("/").some((part) => part === ".." || part === "")) return null;
  let lineStart: number | null = null;
  let lineEnd: number | null = null;
  if (fragment) {
    const match = fragment.match(/^L([1-9]\d*)(?:-L?([1-9]\d*))?$/);
    if (!match) return null;
    lineStart = Number(match[1]);
    lineEnd = match[2] ? Number(match[2]) : lineStart;
    if (lineEnd < lineStart || lineEnd - lineStart > 500) return null;
  }
  return { path, lineStart, lineEnd };
}

export function parseProjectDirectoryTarget(value: string): ProjectDirectoryTarget | null {
  const prefix = "project-dir:";
  if (!value.startsWith(prefix)) return null;
  const encodedPath = value.slice(prefix.length);
  if (!encodedPath || encodedPath.includes("?") || encodedPath.includes("#")) return null;
  let path: string;
  try { path = decodeURIComponent(encodedPath); } catch { return null; }
  if (path !== "." && (!path || path.includes("\\") || path.startsWith("/")
      || /^[a-zA-Z]:[\\/]/.test(path)
      || path.split("/").some((part) => part === ".." || part === ""))) return null;
  return { path };
}

export function remoteImageHost(value: string): string | null {
  if (classifyImage(value) === "blocked") return null;
  return new URL(value).hostname;
}

export function markdownUrlTransform(url: string, key: string): string {
  if (/^[\s\u0000-\u001f\u007f]*rag-evidence:/i.test(url)) {
    return key === "href" && parseRagEvidenceTarget(url) ? url : "";
  }
  if (key === "src") return classifyImage(url) === "remote_https" ? url : "";
  if (key === "href") return classifyLink(url) === "external" || parseProjectFileTarget(url) || parseProjectDirectoryTarget(url) ? url : "";
  return "";
}
