import {
  Bookmark, Document, ExternalHyperlink, HeadingLevel, InternalHyperlink, LevelFormat,
  Packer, Paragraph, Table, TableCell, TableRow, TextRun, WidthType,
  type INumberingOptions, type IParagraphOptions, type IRunOptions, type ParagraphChild,
} from "docx";

type Block = Paragraph | Table;
type TextStyle = Pick<IRunOptions, "bold" | "italics" | "strike" | "font" | "style" | "superScript" | "subScript">;
const headings = [HeadingLevel.HEADING_1, HeadingLevel.HEADING_2, HeadingLevel.HEADING_3,
  HeadingLevel.HEADING_4, HeadingLevel.HEADING_5, HeadingLevel.HEADING_6];
const blockTags = new Set(["ARTICLE", "SECTION", "DIV", "BLOCKQUOTE", "P", "PRE", "TABLE", "UL", "OL",
  "DETAILS", "SUMMARY", "H1", "H2", "H3", "H4", "H5", "H6", "DL", "DT", "DD", "HR", "FIGURE", "FIGCAPTION"]);
const pageWidth = 11906; // A4, in twips
const pageMargin = 960;

export async function exportWord(root: HTMLElement, title: string): Promise<Blob> {
  const bookmarks = new Map(Array.from(root.querySelectorAll("details[id]"), (item, index) =>
    [item.id, `Evidence_${index + 1}`] as const));
  const lists: INumberingOptions["config"][number][] = [];

  function inline(node: Node, style: TextStyle = {}): ParagraphChild[] {
    if (node.nodeType === 3) {
      return (node.textContent ?? "").split(/\r\n?|\n/).map((text, index) =>
        new TextRun({ ...style, text, break: index ? 1 : undefined }));
    }
    if (!(node instanceof Element)) return [];
    const tag = node.tagName;
    if (["SCRIPT", "STYLE", "NOSCRIPT"].includes(tag)) return [];
    if (tag === "BR") return [new TextRun({ break: 1 })];
    if (tag === "IMG") return [new TextRun(node.getAttribute("alt") ?? "")];
    const next = { ...style };
    if (tag === "STRONG" || tag === "B") next.bold = true;
    if (tag === "EM" || tag === "I") next.italics = true;
    if (tag === "DEL" || tag === "S") next.strike = true;
    if (tag === "SUP") next.superScript = true;
    if (tag === "SUB") next.subScript = true;
    if (tag === "CODE") next.font = { ascii: "Consolas", eastAsia: "Microsoft YaHei" };
    const href = tag === "A" ? node.getAttribute("href") : null;
    const anchor = href?.startsWith("#") ? bookmarks.get(href.slice(1)) : undefined;
    const external = !!href && /^(https?:\/\/|mailto:)/i.test(href);
    if (anchor || external) next.style = "Hyperlink";
    const children = Array.from(node.childNodes).flatMap(child => inline(child, next));
    if (anchor) return [new InternalHyperlink({ anchor, children })];
    if (external && href) return [new ExternalHyperlink({ link: href, children })];
    return children;
  }

  function blocks(nodes: Iterable<Node> | ArrayLike<Node>, depth = 0): Block[] {
    const result: Block[] = [];
    let pending: Node[] = [];
    function flush() {
      if (pending.some(n => n.nodeType === 1 || n.textContent?.trim())) {
        result.push(new Paragraph({ children: pending.flatMap(n => inline(n)) }));
      }
      pending = [];
    }
    for (const node of Array.from(nodes)) {
      if (node instanceof Element && blockTags.has(node.tagName)) {
        flush();
        result.push(...block(node, depth));
      } else pending.push(node);
    }
    flush();
    return result;
  }

  function table(element: HTMLTableElement): Block[] {
    const rows = Array.from(element.rows);
    if (!rows.length) return [];
    const count = Math.max(1, ...rows.map(row => row.cells.length));
    // Give prose more space than short identifiers while keeping every column visible.
    const weights = Array.from({ length: count }, (_, i) => {
      const longest = rows.reduce((max, row) => Math.max(max, row.cells[i]?.textContent?.trim().length ?? 0), 0);
      return Math.min(7, Math.max(2, Math.sqrt(longest)));
    });
    const total = weights.reduce((sum, value) => sum + value, 0);
    const width = pageWidth - pageMargin * 2;
    const columnWidths = weights.map(weight => Math.floor(width * weight / total));
    return [new Table({
      width: { size: width, type: WidthType.DXA }, columnWidths,
      margins: { top: 50, bottom: 50, left: 80, right: 80 },
      rows: rows.map(row => new TableRow({
        tableHeader: row.parentElement?.tagName === "THEAD",
        children: Array.from(row.cells, (cell, i) => {
          const children = blocks(cell.childNodes);
          if (!(children[children.length - 1] instanceof Paragraph)) children.push(new Paragraph(""));
          return new TableCell({
            children, width: { size: columnWidths[i], type: WidthType.DXA },
            shading: cell.tagName === "TH" ? { fill: "E4EBF2" } : undefined,
            columnSpan: cell.colSpan > 1 ? cell.colSpan : undefined,
          });
        }),
      })),
    })];
  }

  function list(element: Element, depth: number): Block[] {
    const reference = `List_${lists.length + 1}`;
    const ordered = element.tagName === "OL";
    const left = 360 * (depth + 1);
    const start = Number(element.getAttribute("start") ?? 1);
    lists.push({ reference, levels: [{ level: 0, start: Number.isInteger(start) ? start : 1,
      format: ordered ? LevelFormat.DECIMAL : LevelFormat.BULLET, text: ordered ? "%1." : "•",
      style: { paragraph: { indent: { left, hanging: 240 } } },
    }] });
    return Array.from(element.children).filter(item => item.tagName === "LI").flatMap(item => {
      const nodes = Array.from(item.childNodes);
      const firstBlock = nodes.findIndex(n => n instanceof Element && blockTags.has(n.tagName));
      const first = firstBlock < 0 ? nodes.length : firstBlock;
      let content = nodes.slice(0, first);
      let rest = nodes.slice(first);
      if (rest[0] instanceof Element && rest[0].tagName === "P" && !content.some(n => n.textContent?.trim())) {
        content = Array.from(rest[0].childNodes);
        rest = rest.slice(1);
      }
      return [new Paragraph({ numbering: { reference, level: 0 }, children: content.flatMap(n => inline(n)) }),
        ...blocks(rest, depth + 1)];
    });
  }

  function block(element: Element, depth: number): Block[] {
    const tag = element.tagName;
    if (tag === "TABLE") return table(element as HTMLTableElement);
    if (tag === "UL" || tag === "OL") return list(element, depth);
    if (tag === "PRE") {
      return (element.textContent ?? "").split(/\r\n?|\n/).map(line =>
        new Paragraph({ spacing: { after: 0 }, children: [new TextRun(line)] }));
    }
    if (tag === "DETAILS") {
      const summary = Array.from(element.children).find(n => n.tagName === "SUMMARY");
      const id = bookmarks.get(element.id);
      const label = summary ? Array.from(summary.childNodes).flatMap(n => inline(n)) : [new TextRun(element.id)];
      const children = id ? [new Bookmark({ id, children: label })] : label;
      return [new Paragraph({ heading: HeadingLevel.HEADING_3, children }),
        ...blocks(Array.from(element.childNodes).filter(n => n !== summary), depth)];
    }
    if (tag === "P" || /^H[1-6]$/.test(tag) || tag === "DT" || tag === "SUMMARY") {
      const heading: IParagraphOptions["heading"] = tag.startsWith("H") ? headings[Number(tag[1]) - 1] : undefined;
      return [new Paragraph({ heading, children: Array.from(element.childNodes).flatMap(n => inline(n)) })];
    }
    return blocks(element.childNodes, depth);
  }

  const children = blocks(root.childNodes);
  const document = new Document({
    title, numbering: { config: lists },
    styles: { default: { document: {
      run: { font: { ascii: "Calibri", eastAsia: "Microsoft YaHei" }, size: 21 },
      paragraph: { spacing: { after: 80, line: 270 } },
    } } },
    sections: [{ properties: { page: { size: { width: pageWidth, height: 16838 },
      margin: { left: pageMargin, right: pageMargin, top: pageMargin, bottom: pageMargin } } }, children }],
  });
  return Packer.toBlob(document);
}
