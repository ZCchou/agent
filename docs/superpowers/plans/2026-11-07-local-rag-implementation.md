# 本地模型文件驱动的适航 RAG 实施记录

用户批准的设计覆盖模型目录与 ONNX 包导入、独立推理进程、DOCX 与文本 PDF 入库、混合检索、项目对话与 Skill、证据校验及本地执行约束。

## 交付顺序

- [x] 模型存储、四种原始模型适配、两种 ONNX 适配与独立 worker
- [x] 文档解析、资料库、后台任务与混合检索
- [x] 辅助模型及资料库 UI、原生文件选择
- [x] 对话、工具、Skill、引用及版本迁移整合
- [x] 自动化回归、CPU 组件构建、代码复核及修复
- [ ] 完整官方权重、CUDA 实机、十万条实际维度端到端性能和安装器验收
- [ ] 独立 CUDA 运行组件的图形化导入、解析进程级内存隔离

## 模块接口

`rag.models.ModelStore(data_dir: Path)` 提供 `list_models()`, `set_root(path)`, `inspect(path, profile=None)`, `async import_model(path, profile, device='cpu')`, `set_device(model_id, device)`, `remove(model_id)`, `async probe(model_id)`, `async embed(model_id, texts, query=False)`, `async rerank(model_id, query, texts)`, `async close()`。

模型记录字段为 `id, name, role, backend, profile, device, status, digest, dimension, size_bytes`。原始目录 profile 为 `qwen3-embedding-0.6b`, `qwen3-reranker-0.6b`, `bge-m3`, `bge-reranker-v2-m3`。ONNX 使用对应 BGE profile 与明确的模型包元数据。

`rag.service.RagService(repository, models=None)` 提供 `async handle(action, params)`, `async search(project_id, query, run_id=None, library_ids=None, version_ids=None, limit=8)`, `read_evidence(project_id, evidence_id, offset=0, length=8000)`, `project_state(project_id)`, `async close()`。返回 JSON 可序列化字典。

管理动作统一使用 IPC `rag.request`，参数为 `action` 与 `params`。动作白名单包含 `model.list`, `model.inspect`, `model.import`, `model.root`, `model.device`, `model.remove`, `model.probe`, `library.list`, `library.create`, `library.update`, `library.bind`, `document.import`, `document.paste`, `document.preview`, `document.confirm`, `document.withdraw`, `job.list`, `job.cancel`, `index.build`, `search`, `evidence.preview`。

`model.list` 返回 `{models, root, runtime}`。资料库记录为 `id, name, embedding_model_id, reranker_model_id, project_id, active_index_id, generation`。`library.create` 参数为 `name, embedding_model_id, reranker_model_id`。`library.bind` 参数为 `library_id, project_id, enabled`。`library.list` 返回 `{libraries}`，允许 `project_id` 过滤。

文档参数为 `library_id, paths` 或 `library_id, title, content`。版本确认参数为 `version_id, revision, source, document_number, title`。`document.preview` 返回 `{version, chunks, warnings}`。资料库列表附带 `documents`，各文档包含 `id, version_id, title, revision, status, warnings, chunk_count`。任务列表返回 `{jobs}`，任务含 `id, kind, library_id, status, progress, error`。

`search` 返回 `{evidence, generation, index_ids}`，证据包含 `id, library_id, version_id, title, revision, clause_number, locator, text, text_hash, score`。无可用证据抛 `CoreDataError`。检索结果为 external_untrusted。

Rust 提供 `rag_request(action, params)` 及 `rag_select_paths(kind)`，kind 为 `model`, `documents`, `root`。文件路径由原生选择授权，模型或文档不能自行授予路径访问权限。前端使用显式类型的 RagRepository。

## 执行裁定与进展

- 原生 worktree 工具超时，使用现有独立仓库的 `codex/local-rag` 分支，避免阻塞实现。
- 基线环境缺少 pytest/httpx/PyYAML 及推理依赖，使用项目内隔离虚拟环境准备测试和打包依赖。
- 生成模型继续使用现有本地配置，辅助模型由应用加载。ONNX 首期仅承诺适配后的 BGE 模型包。

## 已落实的关键约束

- 模型选择授予短期、精确路径权限，复制并哈希复核后原子发布。worker 禁止自动下载与远程模型代码。
- 项目绑定索引快照。资料更新及索引重建保留旧绑定，项目显式采用当前索引后生效。
- 检索采用条款精确匹配、中文二元词 FTS、sqlite-vec 授权范围余弦扫描及 RRF，融合前 40 条重排序，最多 8 条进入证据。
- 原文切分保留标题路径、父块、所属条款及相邻块关系。超长语句强制分片会产生警告，`rag.read` 可分页展开同版本父上下文。
- 回答累计证据预算遵守输入预算 30% 及 6,000 token 上限。引用校验只接受实际装入本轮模型上下文的摘录。
- 最终提交通过 SQLite 事务再次验证项目资料变更序号，防止资料撤回与回答发布竞争。
- 本地限制覆盖生成、重试、压缩及保留 RAG 历史的复制与编辑分支，HTTP 客户端关闭环境代理。
- Host、Core、构建契约同步为 IPC 17、schema 24、工具契约 v3，保留旧项目与对话迁移路径。
- 发行组件逐文件校验，同时校验 worker 源码和设备依赖锁身份。CPU 和 CUDA 有独立固定依赖清单。
- Windows 持续 IPC 采用可用字节探测后读取，避免等待下一请求时阻塞首次 NumPy/ONNX 原生初始化。保留 CRLF、UTF-8、EOF 残帧及文件重定向行为。

## 尚待完成的验收与实现

CPU 已提供冻结组件。CUDA 目前通过匹配的完整离线发行组件部署，图形化独立组件安装入口待实现。

解析使用受限后台线程，超时和取消后的结果禁止提交。原生解析调用的强制终止及操作系统级单任务内存限制待实现。

适配器数值比较使用微型生成权重及合成 ONNX 图。完整官方权重质量、目标 NVIDIA 设备、十万条 1024 维完整检索延迟和资源占用、进程出网监测以及最终 NSIS 安装器尚需在目标环境验收。用户操作与验证范围见 [使用说明](../../local-rag.md)。

## 本次验证记录

验证设备为 Intel Core i5-10200H，4 核 8 线程，15.9 GiB RAM，Windows 11 专业版 Insider Preview 10.0.26220。测试使用 CPU 组件和 Python 3.12，辅助推理组件约 3.5 GB。

| 检查 | 结果 |
| --- | --- |
| Python 完整回归 | 910 项通过，451.96 秒 |
| 前端单元测试与生产构建 | 147 项通过，构建通过，保留已有大分块提示 |
| Edge 完整浏览器流程 | 171 项通过，4.6 分钟 |
| 父上下文 UI 更新后的 RAG 浏览器流程 | 2 项通过，9.4 秒 |
| Rust 完整回归 | 244 项通过，2 项原有忽略项 |
| Rust 格式与严格 Clippy | 通过，全部 target 与 feature 启用 |
| 发行工具测试 | 24 项通过 |
| 上下文保持与负向变异评估 | 54 个场景，759 项断言通过，6 个变异全部检出 |
| 冻结 Core 兼容性 | Core 0.14.2、IPC 17、schema 24、schema24-rag-v1 握手通过 |
| 冻结 CPU 辅助组件 | 四种微型原始适配模型、BGE 合成 ONNX、中文路径及错误帧通过 |
| 冻结 Core 完整 RAG 链路 | ONNX 导入、嵌入与重排序、DOCX 入库、sqlite-vec 索引、检索和证据预览通过 |
| 冻结 PDF 原生依赖 | 文本 PDF 入库、物理页定位和待校核警告通过 |

冻结链路中首次组件完整性校验与嵌入模型探测合计约 92.9 秒。该数字包含约 3.5 GB 运行文件的首次完整哈希扫描。合成 ONNX 的一次检索为 0.079 秒，属于小样本功能检查。生产模型冷启动、查询分位数、峰值内存与 GPU 数据仍按前述待验收事项处理。
