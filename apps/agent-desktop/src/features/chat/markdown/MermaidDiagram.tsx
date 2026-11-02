import { Copy, Maximize2, RefreshCw } from "lucide-react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import type { AssistantMessageState } from "./AssistantMessageBody";
import { copyText } from "./copyText";

const MAX_MERMAID_BYTES = 20 * 1024;

interface Props {
  source: string;
  state: AssistantMessageState;
}

export function MermaidDiagram({ source, state }: Props) {
  const { t } = useTranslation("chat");
  const [url, setUrl] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [revision, setRevision] = useState(0);
  const [expanded, setExpanded] = useState(false);

  useEffect(() => {
    let disposed = false;
    let objectUrl: string | null = null;
    setUrl(null);
    setError(null);
    if (state === "streaming") return () => { disposed = true; };
    if (new Blob([source]).size > MAX_MERMAID_BYTES) {
      setError(t("markdown.mermaidTooLarge"));
      return () => { disposed = true; };
    }
    void Promise.all([import("mermaid"), import("dompurify")]).then(async ([mermaidModule, purifyModule]) => {
      const mermaid = mermaidModule.default;
      mermaid.initialize({
        startOnLoad: false,
        securityLevel: "strict",
        htmlLabels: false,
        suppressErrorRendering: true,
        maxTextSize: MAX_MERMAID_BYTES,
      });
      const id = `agent-mermaid-${crypto.randomUUID()}`;
      const rendered = await mermaid.render(id, source);
      if (disposed) return;
      const DOMPurify = purifyModule.default;
      const svg = DOMPurify.sanitize(rendered.svg, {
        USE_PROFILES: { svg: true, svgFilters: true },
        FORBID_TAGS: ["script", "foreignObject", "iframe", "image", "a"],
        FORBID_ATTR: ["href", "xlink:href", "onclick", "onload", "onerror"],
      });
      objectUrl = URL.createObjectURL(new Blob([svg], { type: "image/svg+xml" }));
      setUrl(objectUrl);
    }).catch(() => {
      if (!disposed) setError(t("markdown.mermaidFailed"));
    });
    return () => {
      disposed = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [revision, source, state, t]);

  if (state === "streaming") return <pre className="mermaid-source"><code>{source}</code></pre>;
  return <figure className={`mermaid-diagram${expanded ? " mermaid-diagram--expanded" : ""}`}>
    <figcaption>
      <span>Mermaid</span>
      <div>
        <button type="button" onClick={() => void copyText(source)}><Copy size={13} />{t("markdown.copyCode")}</button>
        {error ? <button type="button" onClick={() => setRevision((value) => value + 1)}><RefreshCw size={13} />{t("markdown.retryDiagram")}</button> : null}
        {url ? <button type="button" onClick={() => setExpanded((value) => !value)}><Maximize2 size={13} />{t(expanded ? "markdown.closeDiagram" : "markdown.expandDiagram")}</button> : null}
      </div>
    </figcaption>
    {url ? <img src={url} alt={t("markdown.mermaidAlt")} /> : null}
    {error ? <div className="mermaid-diagram__error"><p>{error}</p><pre><code>{source}</code></pre></div> : null}
  </figure>;
}
