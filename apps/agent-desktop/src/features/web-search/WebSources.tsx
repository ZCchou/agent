import { ExternalLink } from "lucide-react";
import { openUrl } from "@tauri-apps/plugin-opener";
import type { WebSource } from "../../types/domain";
import { classifyLink } from "../chat/markdown/markdownPolicy";

export function WebSources({ sources }: { sources: WebSource[] }) {
  if (!sources.length) return null;
  return <ol className="web-sources">
    {sources.map((source) => <li key={source.id}>
      <ExternalLink size={13} />
      {classifyLink(source.url) === "external" ? <button type="button" title={source.url}
        onClick={() => void openUrl(source.url).catch(() => undefined)}>{source.title}</button>
        : <span>{source.title}</span>}
      <span>{source.domain}</span>
    </li>)}
  </ol>;
}
