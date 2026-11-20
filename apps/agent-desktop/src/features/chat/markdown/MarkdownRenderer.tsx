import { Component, memo, useId, useMemo, type ErrorInfo, type ReactNode } from "react";
import ReactMarkdown, { type Components, type Options } from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import type { AssistantMessageBodyProps, AssistantMessageState } from "./AssistantMessageBody";
import { MarkdownCodeBlock } from "./MarkdownCodeBlock";
import { MarkdownImage } from "./MarkdownImage";
import { MarkdownLink } from "./MarkdownLink";
import { reviewVerdicts } from "./reviewVerdicts";
import { markdownUrlTransform } from "./markdownPolicy";
import type { ProjectDirectoryTarget, ProjectFileTarget } from "./markdownPolicy";

class MarkdownErrorBoundary extends Component<
  { content: string; children: ReactNode },
  { failed: boolean }
> {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  componentDidCatch(_error: Error, _info: ErrorInfo) {
    // Rendering falls back to inert text. Message contents are intentionally not logged.
  }

  componentDidUpdate(previous: Readonly<{ content: string; children: ReactNode }>) {
    if (this.state.failed && previous.content !== this.props.content) {
      this.setState({ failed: false });
    }
  }

  render() {
    if (this.state.failed) return <p className="markdown-fallback">{this.props.content}</p>;
    return this.props.children;
  }
}

// HTML-shaped model prose remains visible, without executing or trusting HTML.
function preserveHtmlText() {
  type Node = { type: string; children?: Node[] };
  return (tree: Node) => {
    function walk(node: Node) {
      if (node.type === "html") node.type = "text";
      node.children?.forEach(walk);
    }
    walk(tree);
  };
}
const remarkPlugins: Options["remarkPlugins"] = [remarkGfm, remarkMath, preserveHtmlText];
const rehypePlugins: Options["rehypePlugins"] = [[rehypeKatex, {
  trust: false, strict: "warn", maxExpand: 1000, throwOnError: false,
}]];

export const MarkdownRenderer = memo(function MarkdownRenderer({ content, state, onProjectFile, onProjectDirectory, verifiedEvidenceIds, documentReview, targetLabels }: {
  content: string; state: AssistantMessageState;
  documentReview?: AssistantMessageBodyProps["documentReview"];
  targetLabels?: Readonly<Record<string, string>>;
  verifiedEvidenceIds?: readonly string[];
  onProjectFile?: (target: ProjectFileTarget) => void;
  onProjectDirectory?: (target: ProjectDirectoryTarget) => void;
}) {
  const scope = useId();
  // Stable component types retain code/image/link state as streamed text grows.
  const components = useMemo<Components>(() => ({
    a: (props) => <MarkdownLink {...props} verifiedEvidenceIds={verifiedEvidenceIds} onProjectFile={onProjectFile} onProjectDirectory={onProjectDirectory} />,
    button: ({ children, node, ...props }) => <button {...props} onClick={() => {
      const targetId = node?.properties.dataReviewSource;
      if (typeof targetId !== "string" || !targetId.startsWith(scope + "-quote-")) return;
      const target = document.getElementById(targetId);
      if (!(target instanceof HTMLDetailsElement)) return;
      target.open = true;
      target.querySelector("summary")?.focus();
    }}>{children}</button>,
    img: MarkdownImage,
    pre: (props) => <MarkdownCodeBlock {...props} state={state} />,
    table: ({ children, ...props }) => (
      <div className="markdown-table-scroll"><table {...props}>{children}</table></div>
    ),
  }), [scope, onProjectFile, onProjectDirectory, state, verifiedEvidenceIds]);
  const savedReport = state === "complete" && (content.startsWith("# 文档审阅正文\n") || content.startsWith("# 文档审阅报告\n"));
  const review = documentReview ?? (savedReport ? "committed" : undefined);
  const plugins = useMemo<Options["rehypePlugins"]>(() => review
    ? [...rehypePlugins, [reviewVerdicts, { labels: targetLabels, scope, committed: review === "committed" }]] : rehypePlugins,
    [review, targetLabels, scope]);
  return (
    <MarkdownErrorBoundary content={content}>
      <div className="markdown-body">
        <ReactMarkdown
          remarkPlugins={remarkPlugins}
          rehypePlugins={plugins}
          skipHtml
          urlTransform={markdownUrlTransform}
          components={components}
        >
          {content}
        </ReactMarkdown>
      </div>
    </MarkdownErrorBoundary>
  );
});
