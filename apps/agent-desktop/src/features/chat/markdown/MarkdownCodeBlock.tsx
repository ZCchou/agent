import { Check, Copy } from "lucide-react";
import {
  Children, isValidElement, useEffect, useState,
  type ComponentPropsWithoutRef, type ReactNode,
} from "react";
import { Highlight, themes } from "prism-react-renderer";
import { useTranslation } from "react-i18next";
import type { AssistantMessageState } from "./AssistantMessageBody";
import { copyText } from "./copyText";
import { MermaidDiagram } from "./MermaidDiagram";

const MAX_HIGHLIGHT_BYTES = 100 * 1024;
const LANGUAGE_ALIASES: Record<string, string> = {
  js: "javascript", javascript: "javascript", ts: "typescript", typescript: "typescript",
  jsx: "jsx", tsx: "tsx", py: "python", python: "python", rs: "rust", rust: "rust",
  go: "go", java: "java", c: "c", cpp: "cpp", "c++": "cpp", cs: "csharp", csharp: "csharp",
  json: "json", yaml: "yaml", yml: "yaml", toml: "toml", sql: "sql", bash: "bash",
  sh: "bash", shell: "bash", powershell: "powershell", ps1: "powershell", html: "markup",
  xml: "markup", css: "css", md: "markdown", markdown: "markdown",
};

function nodeText(node: ReactNode): string {
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(nodeText).join("");
  if (isValidElement<{ children?: ReactNode }>(node)) return nodeText(node.props.children);
  return "";
}

export function MarkdownCodeBlock({ children, state }: ComponentPropsWithoutRef<"pre"> & { state: AssistantMessageState }) {
  const { t } = useTranslation("chat");
  const [copied, setCopied] = useState(false);
  const child = Children.toArray(children)[0];
  const className = isValidElement<{ className?: string }>(child) ? child.props.className ?? "" : "";
  const declaredLanguage = className.match(/language-([^\s]+)/)?.[1]?.toLowerCase() ?? "";
  const language = declaredLanguage || t("markdown.code");
  const content = nodeText(child).replace(/\n$/, "");
  const grammar = LANGUAGE_ALIASES[declaredLanguage];

  useEffect(() => {
    if (!copied) return;
    const timer = window.setTimeout(() => setCopied(false), 1_600);
    return () => window.clearTimeout(timer);
  }, [copied]);

  if (declaredLanguage === "mermaid") return <MermaidDiagram source={content} state={state} />;

  return (
    <div className="markdown-code-block">
      <div className="markdown-code-block__header">
        <span>{language}</span>
        <button type="button" onClick={() => void copyText(content).then(() => setCopied(true)).catch(() => undefined)}>
          {copied ? <Check size={13} /> : <Copy size={13} />}
          {copied ? t("markdown.copied") : t("markdown.copyCode")}
        </button>
      </div>
      {grammar && new Blob([content]).size <= MAX_HIGHLIGHT_BYTES ? (
        <Highlight theme={themes.vsLight} code={content} language={grammar}>
          {({ className: highlightedClassName, style, tokens, getLineProps, getTokenProps }) => (
            <pre className={highlightedClassName} style={style}><code>
              {tokens.map((line, lineIndex) => (
                <span key={lineIndex} {...getLineProps({ line })}>
                  {line.map((token, tokenIndex) => <span key={tokenIndex} {...getTokenProps({ token })} />)}
                  {lineIndex < tokens.length - 1 ? "\n" : null}
                </span>
              ))}
            </code></pre>
          )}
        </Highlight>
      ) : <pre><code>{content}</code></pre>}
    </div>
  );
}
