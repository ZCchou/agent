import { useEffect, useState } from "react";
import { Button } from "../../components/ui/Button";
import { Card } from "../../components/ui/Card";
import { ragRepository } from "../../services/runtime";
import type { OcrCatalog, OcrPage } from "../../types/rag";

export function OcrPanel() {
  const [catalog, setCatalog] = useState<OcrCatalog | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => { void ragRepository.listOcrModels().then(setCatalog).catch((reason) => setError(String(reason))); }, []);
  async function importModels() {
    setBusy(true); setError("");
    try {
      const selection = await ragRepository.selectPaths("model");
      if (selection) {
        try { await ragRepository.importOcrModel(selection); }
        finally { await ragRepository.revokeSelection(selection); }
        setCatalog(await ragRepository.listOcrModels());
      }
    } catch (reason) { setError(String(reason)); }
    finally { setBusy(false); }
  }
  return <Card className="rag-card"><h3>PDF 自动 OCR · 预览</h3>
    <p>导入 PDF 时自动补充图片文字和表格，识别结果保留原页定位。复杂表格需要核对行列、单位和脚注。</p>
    <p>运行组件 {catalog ? (catalog.runtime.available ? "可用" : "缺失") : "检查中"} · 已导入 {catalog?.models.length ?? 0} 个模型包</p>
    {catalog?.models.map((model) => <p className="rag-path" key={model.id}>{model.name ?? model.id}</p>)}
    <Button disabled={busy} onClick={() => void importModels()}>{busy ? "正在校验并复制…" : "导入本地 OCR 模型包"}</Button>
    {error && <p role="alert">{error}</p>}
  </Card>;
}

export function OcrPreview({ versionId, onClose }: { versionId: string; onClose: () => void }) {
  const [pages, setPages] = useState<OcrPage[]>([]);
  const [number, setNumber] = useState<number | null>(null);
  const [image, setImage] = useState("");
  const [selected, setSelected] = useState("");
  const [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    void ragRepository.ocrPages(versionId).then((result) => {
      if (active) { setPages(result.pages); setNumber(result.pages[0]?.page ?? null); }
    }).catch((reason) => { if (active) setError(String(reason)); });
    return () => { active = false; };
  }, [versionId]);
  useEffect(() => {
    if (!number) return;
    let active = true;
    setImage(""); setSelected("");
    void ragRepository.renderOcrPage(versionId, number).then((result) => {
      if (active) setImage(result.data_url);
    }).catch((reason) => { if (active) setError(String(reason)); });
    return () => { active = false; };
  }, [number, versionId]);
  const page = pages.find((item) => item.page === number);
  const region = page?.regions.find((item) => item.id === selected);
  return <div className="rag-modal-backdrop"><section className="rag-modal ocr-modal" role="dialog" aria-modal="true" aria-label="OCR 原页对照" onKeyDown={(event) => { if (event.key === "Escape") onClose(); }}>
    <div className="rag-actions"><h3>OCR 原页对照</h3><Button onClick={onClose}>关闭</Button></div>
    <p>机器识别结果。引用数值前请核对原图，复杂表格可能存在行列错位。</p>
    {error && <p role="alert">{error}</p>}
    {!pages.length && <p>本快照暂无 OCR 区域。识别可能尚在进行，或文档使用原生文字。</p>}
    {!!pages.length && <label>物理页码 <select value={number ?? ""} onChange={(event) => setNumber(Number(event.target.value))}>{pages.map((item) => <option key={item.page} value={item.page}>{item.page}</option>)}</select></label>}
    <div className="ocr-columns"><div className="ocr-image">{image && <img src={image} alt={`原件物理第 ${number} 页`} />}
      {image && region && <div className="ocr-highlight" style={{ left: `${region.bbox[0] * 100}%`, top: `${region.bbox[1] * 100}%`, width: `${(region.bbox[2] - region.bbox[0]) * 100}%`, height: `${(region.bbox[3] - region.bbox[1]) * 100}%` }} />}</div>
      <div className="ocr-regions">{page?.regions.map((item) => <article key={item.id}>
        <Button onClick={() => setSelected(item.id)}>{item.kind} · {item.review_status === "needs_review" ? "结构待核对" : "机器识别"}</Button>
        {item.quality_warning && <p role="status">复杂表格，请核对行列、单位和脚注。</p>}
        {item.table ? <div className="ocr-table"><table><tbody>{item.table.rows.map((row, r) => <tr key={r}>{row.map((cell, c) => <td key={c}>{cell}</td>)}</tr>)}</tbody></table></div> : <pre>{item.text}</pre>}
      </article>)}</div></div>
  </section></div>;
}
