import { Globe2 } from "lucide-react";
import type { WebSearchController } from "./useWebSearchController";
export function WebSearchControl(_props: { controller: WebSearchController; disabled?: boolean; onOpenProjectSettings?(): void }) {
  return <div className="web-search-control"><button type="button" className="chat-composer__icon" disabled aria-label="联网检索已停用" title="离线模式已停用联网检索"><Globe2 size={19} /></button></div>;
}
