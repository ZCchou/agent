import type { Element, ElementContent, Root } from "hast";

const tones: Record<string, string> = {
  "符合": "pass", "不符合": "issue", "待补充": "missing", "无法判断": "unknown",
  "Conforms": "pass", "Does not conform": "issue", "Needs information": "missing", "Undetermined": "unknown",
};

function plain(children: ElementContent[]): string {
  return children.map(child => child.type === "text" ? child.value : child.type === "element" ? plain(child.children) : "").join("");
}

function badge(value: string): Element {
  return { type: "element", tagName: "span", properties: { className: ["review-verdict", "review-verdict--" + tones[value]] },
    children: [{ type: "text", value }] };
}

// Only decorate a stated decision, never infer one from a recommendation or a quoted passage.
export function reviewVerdicts(options: { labels?: Readonly<Record<string, string>>; scope: string; committed: boolean }) {
  const { labels, scope, committed } = options;
  const sourceIds = new Set<string>();
  function collectSources(tree: Root) {
    const children = tree.children;
    for (let i = 0; i < children.length; i++) {
      const heading = children[i];
      if (heading.type !== "element" || heading.tagName !== "h5") continue;
      const number = /^审阅出处 ([1-9]\d*)$/.exec(plain(heading.children).trim())?.[1];
      if (!number || sourceIds.has(number)) continue;
      let idx = i + 1;
      const nextElement = () => {
        while (children[idx]?.type === "text" && !String((children[idx] as { value?: string }).value).trim()) idx++;
        return children[idx++];
      };
      const source = nextElement();
      const quote = nextElement();
      if (source?.type !== "element" || source.tagName !== "p" || !/^来源[\s：:]+\S/.test(plain(source.children))
        || quote?.type !== "element" || quote.tagName !== "blockquote") continue;
      sourceIds.add(number);
      const summary: Element = { type: "element", tagName: "summary", properties: {},
        children: [{ type: "text", value: "[" + number + "] " + plain(source.children).replace(/^来源[\s：:]+/, "") }] };
      children.splice(i, idx - i, { type: "element", tagName: "details",
        properties: { id: scope + "-quote-" + number, className: ["review-source"] }, children: [summary, quote] });
    }
  }
  function visit(node: Element | Root) {
    if (node.type === "element") {
      if (["pre", "code", "blockquote"].includes(node.tagName)) return;
      if (node.tagName === "a" && typeof node.properties.href === "string") {
        let href = node.properties.href;
        try { href = decodeURIComponent(href); } catch { /* Leave malformed links to the ordinary URL policy. */ }
        const number = /^#审阅出处-([1-9]\d*)$/.exec(href)?.[1];
        if (number && sourceIds.has(number) && plain(node.children) === number) {
          node.tagName = "button";
          node.properties = { type: "button", dataReviewSource: scope + "-quote-" + number,
            ariaControls: [scope + "-quote-" + number], ariaLabel: "审阅出处 " + number, className: ["markdown-link", "markdown-link--citation"] };
        }
      }
      if (/^h[1-6]$/.test(node.tagName) && node.children.length === 1) {
        const id = plain(node.children).trim();
        const label = labels?.[id];
        if (typeof label === "string" && /^(?:p-\d+|t-\d+-r\d+-c\d+)$/.test(id)) {
          node.properties.title = id;
          node.children = [{ type: "text", value: label }];
        }
      }
      if (committed && node.tagName === "p") {
        const [label, next] = node.children;
        if (label?.type === "element" && label.tagName === "strong"
          && /^(判断|Verdict)[:：]?$/.test(plain(label.children).trim()) && next?.type === "text") {
          const match = /^(\s*[:：]?\s*)(不符合|符合|待补充|无法判断|Does not conform|Conforms|Needs information|Undetermined)(?=$|[\s。；;，,.])/.exec(next.value);
          if (match) node.children.splice(1, 1, { type: "text", value: match[1] }, badge(match[2]),
            { type: "text", value: next.value.slice(match[0].length) });
        }
      }
      if (committed && node.tagName === "table") {
        const sections = node.children.filter((child): child is Element => child.type === "element");
        const head = sections.find(child => child.tagName === "thead")?.children.find((child): child is Element => child.type === "element" && child.tagName === "tr");
        const column = head?.children.filter((child): child is Element => child.type === "element" && child.tagName === "th")
          .findIndex(cell => /^(判断|审阅判断|Verdict)$/i.test(plain(cell.children).trim())) ?? -1;
        for (const row of sections.find(child => child.tagName === "tbody")?.children ?? []) {
          if (row.type !== "element" || column < 0) continue;
          const cell = row.children.filter((child): child is Element => child.type === "element" && child.tagName === "td")[column];
          if (!cell) continue;
          const value = plain(cell.children).trim();
          if (Object.prototype.hasOwnProperty.call(tones, value)) cell.children = [badge(value)];
        }
      }
    }
    for (const child of node.children) if (child.type === "element") visit(child);
  }
  return (tree: Root) => { if (committed) collectSources(tree); visit(tree); };
}
