const semanticTags = new Set(
  "h1 h2 h3 h4 h5 h6 p blockquote table caption thead tbody tfoot tr th td ul ol li strong em b i del s sup sub pre code br hr details summary figure figcaption dl dt dd".split(" "),
);

// Build a detached document from text and known elements. No application attributes or resources are copied.
export function exportMarkup(root: HTMLElement): HTMLElement {
  const doc = root.ownerDocument.implementation.createHTMLDocument();
  const article = doc.createElement("article");
  const sources = new Map<string, string>();
  const used = new Set<string>();
  for (const detail of Array.from(root.querySelectorAll("details.review-source[id]"))) {
    const number = /-quote-([1-9]\d*)$/.exec(detail.id)?.[1];
    if (number && !used.has(number)) {
      sources.set(detail.id, "evidence-" + number);
      used.add(number);
    }
  }

  function copy(node: Node, parent: Node) {
    if (node.nodeType === 3) {
      parent.appendChild(doc.createTextNode(node.textContent ?? ""));
      return;
    }
    if (node.nodeType !== 1) return;
    const element = node as Element;
    if (element.matches("script,style,template,svg,iframe,object,embed,form,textarea,select,[hidden],.markdown-code-block__header,.katex-html")) return;
    const tag = element.localName;
    if (tag === "img") {
      parent.appendChild(doc.createTextNode(element.getAttribute("alt") ?? ""));
      return;
    }
    if (element.classList.contains("katex")) {
      const tex = element.querySelector('annotation[encoding="application/x-tex"]');
      if (tex) {
        parent.appendChild(doc.createTextNode(tex.textContent ?? ""));
        return;
      }
    }
    if (tag === "input") {
      if (element.getAttribute("type") === "checkbox") {
        parent.appendChild(doc.createTextNode(element.hasAttribute("checked") ? "☑ " : "☐ "));
      }
      return;
    }
    let target = parent;
    if (tag === "button") {
      const id = sources.get(element.getAttribute("data-review-source") ?? "");
      if (id) {
        const link = doc.createElement("a");
        link.setAttribute("href", "#" + id);
        target = parent.appendChild(link);
      } else if (!element.classList.contains("markdown-link")) return;
    } else if (semanticTags.has(tag)) {
      const out = doc.createElement(tag);
      if (tag === "details") {
        out.setAttribute("open", "");
        const id = sources.get(element.id);
        if (id && !article.querySelector("#" + id)) out.id = id;
      } else if (tag === "ol" && /^-?\d+$/.test(element.getAttribute("start") ?? "")) {
        out.setAttribute("start", element.getAttribute("start")!);
      } else if (tag === "td" || tag === "th") {
        for (const attr of ["colspan", "rowspan"]) {
          const value = element.getAttribute(attr);
          if (value && /^\d+$/.test(value)) out.setAttribute(attr, value);
        }
      }
      target = parent.appendChild(out);
    }
    for (const child of Array.from(element.childNodes)) copy(child, target);
  }

  for (const child of Array.from(root.childNodes)) copy(child, article);
  return article;
}

const reportStyle = `
  :root { color-scheme: light; }
  body { max-width: 60rem; margin: 2.5rem auto; padding: 0 1.5rem; color: #20262d;
    font: 16px/1.65 system-ui, "Microsoft YaHei", sans-serif; overflow-wrap: anywhere; }
  h1, h2, h3, h4, h5, h6 { line-height: 1.35; break-after: avoid; }
  table { width: 100%; border-collapse: collapse; margin: 1em 0; }
  td, th { border: 1px solid #c9cfd4; padding: .4em .65em; text-align: start; vertical-align: top; }
  th { background: #f2f4f6; }
  blockquote { margin: .6em 0; border-inline-start: 3px solid #aebcc9; padding-inline-start: 1em; }
  pre { white-space: pre-wrap; background: #f4f5f6; padding: .8em; }
  details { margin: 1em 0; }
  summary { font-weight: 600; }
  a { color: #135b98; }
  :target { outline: 2px solid #47769d; outline-offset: 3px; }
  @media print { body { margin: 0; padding: 0; } tr { break-inside: avoid; } }
`;

export function exportHtml(normalized: HTMLElement, title: string): Blob {
  const doc = normalized.ownerDocument.implementation.createHTMLDocument(title);
  const lang = document.documentElement.lang;
  if (lang) doc.documentElement.lang = lang;
  const charset = doc.createElement("meta");
  charset.setAttribute("charset", "utf-8");
  doc.head.prepend(charset);
  const policy = doc.createElement("meta");
  policy.httpEquiv = "Content-Security-Policy";
  policy.content = "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'";
  doc.head.appendChild(policy);
  const style = doc.createElement("style");
  style.textContent = reportStyle;
  doc.head.appendChild(style);
  doc.body.appendChild(doc.importNode(normalized, true));
  return new Blob(["<!doctype html>\n", doc.documentElement.outerHTML], { type: "text/html;charset=utf-8" });
}
