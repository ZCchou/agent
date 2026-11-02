import { Component, memo, useMemo, type ErrorInfo, type ReactNode } from "react";
import ReactMarkdown, { type Components, type Options } from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import type { AssistantMessageState } from "./AssistantMessageBody";
import { MarkdownCodeBlock } from "./MarkdownCodeBlock";
import { MarkdownImage } from "./MarkdownImage";
import { MarkdownLink } from "./MarkdownLink";
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

const remarkPlugins: Options["remarkPlugins"] = [remarkGfm, remarkMath];
const rehypePlugins: Options["rehypePlugins"] = [[rehypeKatex, {
  trust: false, strict: "warn", maxExpand: 1000, throwOnError: false,
}]];

export const MarkdownRenderer = memo(function MarkdownRenderer({ content, state, onProjectFile, onProjectDirectory }: {
  content: string; state: AssistantMessageState;
  onProjectFile?: (target: ProjectFileTarget) => void;
  onProjectDirectory?: (target: ProjectDirectoryTarget) => void;
}) {
  // Stable component types retain code/image/link state as streamed text grows.
  const components = useMemo<Components>(() => ({
    a: (props) => <MarkdownLink {...props} onProjectFile={onProjectFile} onProjectDirectory={onProjectDirectory} />,
    img: MarkdownImage,
    pre: (props) => <MarkdownCodeBlock {...props} state={state} />,
    table: ({ children, ...props }) => (
      <div className="markdown-table-scroll"><table {...props}>{children}</table></div>
    ),
  }), [onProjectFile, onProjectDirectory, state]);
  return (
    <MarkdownErrorBoundary content={content}>
      <div className="markdown-body">
        <ReactMarkdown
          remarkPlugins={remarkPlugins}
          rehypePlugins={rehypePlugins}
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
