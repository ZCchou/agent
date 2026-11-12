import { useCallback, useEffect, useRef, useState } from "react";
import { Button } from "../../components/ui/Button";
import { Card } from "../../components/ui/Card";
import { localModelRepository, modelCatalogRepository } from "../../services/runtime";
import type { LocalModel, LocalModelConfiguration, LocalModelError, LocalModelJob, LocalModelList, LocalPathSelection, LocalRuntimeCapabilities, LocalRuntimeStatus } from "../../types/localModels";

function formatLocalModelError(error: LocalModelError | string) {
  return typeof error === "string" ? error : `${error.message} (${error.code})`;
}

export function LocalModelConfigurationForm({ model, busy, onSave, onSaveAndLoad, blockedReason }: { model: LocalModel; busy: boolean; onSave(configuration: LocalModelConfiguration): void; onSaveAndLoad?(configuration: LocalModelConfiguration): void; blockedReason?: string }) {
  const contextInput = useRef<HTMLInputElement>(null);
  const declaredMaximum = model.declared_context_length;
  const hasDeclaredMaximum = typeof declaredMaximum === 'number' && Number.isSafeInteger(declaredMaximum) && declaredMaximum > 0;
  return <form className="local-model-config" onSubmit={event => {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    const configuration = { device: data.get("device") as LocalModelConfiguration["device"], context_length: Number(data.get("context")), threads: Number(data.get("threads")), gpu_layers: Number(data.get("layers")), source: String(data.get("source")), license: String(data.get("license")) };
    const submitter = (event.nativeEvent as SubmitEvent).submitter as HTMLButtonElement | null;
    if (submitter?.value === "reload" && onSaveAndLoad) onSaveAndLoad(configuration);
    else onSave(configuration);
  }}>
    <label>设备<select name="device" defaultValue={model.device || "auto"}><option value="cpu">CPU</option><option value="auto">自动（优先 GPU）</option><option value="cuda">CUDA</option><option value="vulkan">Vulkan</option></select></label>
    <label>上下文<input ref={contextInput} name="context" type="number" min="1" max={model.max_context_length || model.context_length || 8192} defaultValue={model.context_length || Math.min(8192, model.max_context_length || 8192)} required /></label>
    <Button type="button" disabled={busy || model.status === 'loading' || !hasDeclaredMaximum}
      title={!hasDeclaredMaximum ? '模型文件未声明最大上下文，无法使用此操作。' : blockedReason}
      onClick={() => { if (contextInput.current && hasDeclaredMaximum) contextInput.current.value = String(declaredMaximum); }}>使用模型声明的最大上下文</Button>
    <p>{hasDeclaredMaximum ? `模型声明上限 ${declaredMaximum}。填入后保存配置，重新加载生效。` : '模型文件未声明最大上下文，无法使用此操作。'}</p>
    <label>线程<input name="threads" type="number" min="1" max="1024" defaultValue={model.threads || 4} required /></label>
    <label>GPU 层数（-1 自动）<input name="layers" type="number" min="-1" max="10000" defaultValue={model.gpu_layers ?? -1} required /></label>
    <label>来源<input name="source" defaultValue={model.source || ""} /></label>
    <label>许可证<input name="license" defaultValue={model.license || ""} /></label>
    <p>自动模式依次尝试 CUDA、Vulkan、CPU。手动选择的设备加载失败时会显示原因。</p>
    {model.status === "loaded" && <p>模型已加载。修改设备后可点击“保存配置并重新加载”。</p>}
    {blockedReason && <p role="status">{blockedReason}</p>}
    <Button type="submit" disabled={busy || model.status === "loaded" || model.status === "loading"} title={model.status === "loaded" ? "已加载模型请使用保存配置并重新加载" : blockedReason}>保存配置</Button>
    <Button type="submit" value="reload" disabled={busy || model.status === "loading" || !onSaveAndLoad} title={blockedReason}>保存配置并重新加载</Button>
  </form>;
}
export function LocalModelJobs({ jobs, busy, onCancel }: { jobs: LocalModelJob[]; busy: boolean; onCancel(id: string): void }) {
  return <div aria-label="本地模型任务">{jobs.map(job => {
    const state = job.state ?? job.status ?? "queued";
    return <div key={job.id} className="local-model-job"><span>{job.kind || "模型任务"} · {state}</span><progress aria-label={`任务 ${job.id}`} value={job.progress ?? 0} max="1" />
      {job.error && <p role="alert">{formatLocalModelError(job.error)}</p>}
      {["queued", "running"].includes(state) && <Button disabled={busy} onClick={() => onCancel(job.id)}>取消任务</Button>}
    </div>;
  })}</div>;
}
export function LocalModelsPanel({ onChanged, onAuxiliary }: { onChanged(): Promise<void>; onAuxiliary(): void }) {
  const [catalog, setCatalog] = useState<LocalModelList>({ models: [], root: "" });
  const [runtime, setRuntime] = useState<LocalRuntimeStatus>({ state: "stopped" });
  const [jobs, setJobs] = useState<LocalModelJob[]>([]);
  const [selection, setSelection] = useState<LocalPathSelection | null>(null);
  const selectionRef = useRef<LocalPathSelection | null>(null);
  const [inspection, setInspection] = useState<LocalModel | null>(null);
  const [capabilities, setCapabilities] = useState<LocalRuntimeCapabilities | null>(null);
  const [probing, setProbing] = useState(false);
  const [busy, setBusy] = useState(false);
  const lock = useRef(false);
  const [error, setError] = useState<string | null>(null);
  const catalogRevision = useRef("");
  const refresh = useCallback(async () => {
    const [next, status, taskList] = await Promise.all([localModelRepository.list(), localModelRepository.status(), localModelRepository.jobs()]);
    setCatalog(next); setRuntime(status); setJobs(taskList.jobs);
    const revision = JSON.stringify(next.models.map(model => [model.id, model.status, model.is_default]));
    if (catalogRevision.current && revision !== catalogRevision.current) await onChanged();
    catalogRevision.current = revision;
  }, [onChanged]);
  useEffect(() => {
    let active = true;
    const update = () => { if (active) void refresh().catch(value => { if (active) setError(String(value)); }); };
    void localModelRepository.capabilities(false).then(setCapabilities).catch(value => setError(String(value)));
    update(); const timer = window.setInterval(update, 2000);
    return () => { active = false; window.clearInterval(timer); const selected = selectionRef.current; if (selected) void localModelRepository.revoke(selected).catch(() => undefined); };
  }, [refresh]);
  async function act(operation: () => Promise<unknown>) {
    if (lock.current) return;
    lock.current = true; setBusy(true); setError(null);
    try { await operation(); await refresh(); await onChanged(); }
    catch (value) { setError(String(value)); }
    finally { lock.current = false; setBusy(false); }
  }
  async function selectModel() {
    const picked = await localModelRepository.selectPath("model");
    if (!picked) return;
    if (selectionRef.current) await localModelRepository.revoke(selectionRef.current);
    selectionRef.current = picked; setSelection(picked); setInspection(null);
    const result = await localModelRepository.inspect(picked); setInspection(result.model);
  }
  async function probeResources() {
    setProbing(true); setError(null);
    try { setCapabilities(await localModelRepository.capabilities()); }
    catch (value) { setError(String(value)); }
    finally { setProbing(false); }
  }
  const runtimeBlocked = (runtime.active_run_count ?? 0) > 0 ? "对话正在使用模型，请先停止或等待回答完成。"
    : (runtime.preparation_count ?? 0) > 0 ? "正在为消息准备模型，请等待或取消该消息。"
    : runtime.operation ? "模型任务正在进行，请等待完成或取消任务。" : undefined;
  return <section className="local-models" aria-label="本地模型管理">
    <p className="models-alert" role="status">离线模式 · 模型文件由本机导入 · 公网服务、检索、远程图片和在线更新已停用</p>
    {error && <div role="alert"><p>{error}</p><p>运行中请先停止对话或卸载模型后重试。指定设备加载失败时可修改配置后重新加载。</p><Button onClick={() => void act(refresh)}>重试刷新</Button></div>}
    <Card><h2>主模型</h2><p>选择单个 GGUF 文件。分片模型选择主文件，系统会校验同目录分片。</p>
      <Button disabled={busy} onClick={() => void act(selectModel)}>选择 GGUF 文件</Button>
      {selection && <div><p>{selection.path}</p><pre>{JSON.stringify(inspection, null, 2)}</pre>
        <Button disabled={busy || !inspection} onClick={() => void act(async () => { await localModelRepository.importModel(selection); selectionRef.current = null; setSelection(null); setInspection(null); })}>导入所选模型</Button>
        <Button disabled={busy} onClick={() => void act(async () => { await localModelRepository.revoke(selection); selectionRef.current = null; setSelection(null); setInspection(null); })}>取消选择</Button>
      </div>}
      <p>模型存储目录 {catalog.root || "尚未导入"}</p><Button disabled={busy} onClick={() => void act(async () => { const root = await localModelRepository.selectPath("root"); if (root) await localModelRepository.setRoot(root); })}>更改模型存储目录</Button>
      {Boolean(catalog.storage_migration) && <div role="status"><p>存储迁移状态如下。旧目录保留用于恢复，请在确认后处理。</p><pre>{JSON.stringify(catalog.storage_migration, null, 2)}</pre></div>}
      {catalog.models.length === 0 && <p>尚无主模型。导入后点击加载，即可在聊天和项目中选择。</p>}
      {catalog.models.map(model => <article key={model.id} className="local-model-record"><h3>{model.name} {model.is_default ? "· 默认模型" : ""}</h3>
        <p>{model.architecture} · {model.quantization} · {(model.size_bytes / 1024 ** 3).toFixed(2)} GiB · {model.status}</p>
        <p>来源 {model.source || "未标注"} · 许可证 {model.license || "未标注"}</p><p className="local-model-digest">指纹 {model.digest || model.id}</p>
        {model.error && <p role="alert">{formatLocalModelError(model.error)}</p>}
        <LocalModelConfigurationForm key={`${model.id}:${model.device}:${model.context_length}:${model.threads}:${model.gpu_layers}`} model={model} busy={busy || Boolean(runtimeBlocked)} blockedReason={runtimeBlocked} onSave={config => void act(() => localModelRepository.configure(model.id, config))} onSaveAndLoad={config => void act(() => localModelRepository.configureAndLoad(model.id, config))} />
        <div className="local-model-actions"><Button disabled={busy || Boolean(runtimeBlocked) || model.status === "loading"} title={runtimeBlocked || (model.status === "loading" ? "正在加载，请等待或取消任务" : undefined)} onClick={() => void act(() => localModelRepository.load(model.id))}>{model.status === "loaded" ? "重新加载模型" : "加载模型"}</Button>
          <Button disabled={busy || Boolean(runtimeBlocked) || runtime.model_id !== model.id} title={runtimeBlocked || (runtime.model_id !== model.id ? "该模型当前未加载" : undefined)} onClick={() => void act(() => localModelRepository.unload())}>卸载模型</Button>
          <Button disabled={busy || model.status !== "loaded"} onClick={() => void act(() => modelCatalogRepository.probeTools(model.provider_profile_id, model.id))}>测试工具支持</Button>
          <Button disabled={busy || model.is_default} onClick={() => void act(() => modelCatalogRepository.updateDefault(model.provider_profile_id, model.id))}>设为默认</Button>
          <Button disabled={busy || model.status === "loaded" || model.status === "loading"} onClick={() => void act(() => localModelRepository.remove(model.id))}>删除模型</Button></div>
      </article>)}
      <LocalModelJobs jobs={jobs} busy={busy} onCancel={id => void act(() => localModelRepository.cancel(id))} />
    </Card>
    <Card><h2>辅助模型</h2><p>嵌入与重排模型沿用知识库中的本地模型配置。</p><Button onClick={onAuxiliary}>管理辅助模型</Button></Card>
    <Card><h2>设备与运行时</h2><p>新导入模型默认自动选择，优先 GPU。已有模型按保存的设备配置加载。检测会校验资源并探测可用设备。</p><p>本地模型首输出最多等待 5 分钟，输出中断等待 60 秒，单次调用最多 15 分钟。可在对话中取消。</p>
      <Button disabled={probing} onClick={() => void probeResources()}>{probing ? "正在校验资源与探测设备…" : "检测设备与运行时"}</Button>
      {capabilities && <pre>{JSON.stringify(capabilities, null, 2)}</pre>}
      <p role="status">状态 {runtime.state} · 实际后端 {runtime.backend || "未加载"} · GPU 层数 {runtime.gpu_layers_used ?? 0} · 实际上下文 {runtime.context_length ?? 0}</p>
      {runtime.preflight && <details><summary>内存与资源估算</summary>{runtime.preflight.physical_memory_pressure && <p role="status">物理内存余量较低，加载可能使用文件分页。</p>}<pre>{JSON.stringify(runtime.preflight, null, 2)}</pre></details>}
      {runtime.error && <p role="alert">{formatLocalModelError(runtime.error)}</p>}{Boolean(runtime.fallbacks?.length) && <pre>{JSON.stringify(runtime.fallbacks, null, 2)}</pre>}
      <Button disabled={busy || Boolean(runtimeBlocked) || !runtime.model_id} title={runtimeBlocked || (!runtime.model_id ? "当前没有已加载模型" : undefined)} onClick={() => void act(() => localModelRepository.unload())}>卸载当前模型</Button>
      {runtimeBlocked && <p role="status">{runtimeBlocked}</p>}
    </Card>
  </section>;
}
