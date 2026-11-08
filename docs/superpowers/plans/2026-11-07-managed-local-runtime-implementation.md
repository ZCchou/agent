# 本地模型托管、GPU 适配与离线部署实施计划

用户批准的完整计划为本次实施规范，基线为 `49a5dff`。实施分支为 `codex/managed-local-runtime`。

## 全局要求

- Windows 11 x64 单用户适航离线应用，模型权重本地导入，运行组件随包提供。
- 主模型为 GGUF，应用托管同版本 llama.cpp 的 CPU/CUDA/Vulkan 构建，默认 CPU，自动模式依次探测 CUDA、Vulkan、CPU。
- 首批文本架构为 Qwen3 与 DeepSeek-R1-Distill-Qwen。完整权重验收使用 Qwen3-8B、DeepSeek-R1-Distill-Qwen-7B Q4_K_M。
- RAG 原始目录与已适配 ONNX 的现有 CPU/CUDA 路线接入资源检查和完整离线组件。
- 主模型默认上下文 8192、加载超时 300 秒、单模型驻留、生成串行、活动请求期间切换排队。
- 解析默认并发一，进程内存上限 1 GiB、超时125秒、结果256 MiB，复用原件100 MiB、DOCX解压200 MiB、文本2000万字符、片段10万上限。
- schema25、IPC18、构建ID `schema25-local-runtime-v1`，保留旧数据和本地服务兼容。
- 单EXE优先。容量或离线验收失败时交付含安装器和全部本地载荷的ZIP。离线提供WebView2、运行库、组件清单和许可证。

## Task 1 — 主模型文件与托管引擎后端

- [x] 新增 `agent.local_models`，提供严格GGUF单文件/分片检查、受管导入、指纹、配置、默认模型、迁移、异步任务和取消恢复。
- [x] 提供CPU/CUDA/Vulkan固定清单校验与探测，隔离DLL路径，后台完整性校验与进度。
- [x] 管理llama.cpp启动、私有回环端口、会话鉴权文件、健康/身份检查、流式调用租约、串行、卸载、空闲退出和Job Object回收。
- [x] 新增schema25迁移模块及聚焦测试。固定接口见本任务brief。

## Task 2 — 解析进程隔离

- [x] 解析worker支持冻结Core启动模式，等待父进程授权后解析，受Windows Job Object约束。
- [x] 文档导入与粘贴统一使用受管原件，输出受限暂存NDJSON，由父进程核验后发布。
- [x] 验证取消、超时、超限、崩溃、重启、定位和原有数据恢复。

## Task 3 — 前端与Host文件授权

- [x] 新增LocalModelRepository、类型、原生文件授权与严格动作白名单。
- [x] 主模型、辅助模型、设备/运行组件界面，导入/加载/默认/配置/取消/占用提示与模型选择器整合。
- [x] 本地模型目录与旧手动本地服务合并，动态端点和鉴权不进入设置存储。
- [x] 前端、Host、浏览器流程测试。

## Task 4 — Core路由、离线策略和迁移整合

- [x] Core接入local_model.request和托管provider，并在所有生成/工具/压缩入口检查会话与本地权限。
- [x] 生产离线模式禁用公网模型/发现/搜索/远程图片/更新，历史资料保护持续生效。
- [x] 主模型和RAG辅助GPU资源协调，ONNX CUDA依赖预加载与实际provider验证。
- [x] schema25、IPC18和构建兼容升级，既有记录与引用迁移测试。

## Task 5 — 运行组件构建与离线发行

- [x] 固定llama.cpp源码版本、构建配置、资源清单/哈希和许可证，准备三类Windows引擎及RAG组件。
- [x] 单EXE路径与完整ZIP载荷路径，离线WebView2和运行库，验证升级与回滚。
- [x] 更新发行manifest与签名处理，旧调用兼容及发行工具回归。

## Task 6 — 整体验收

- [x] 全部Python、前端、Host、浏览器、上下文评估及发行测试通过。
- [ ] 冻结组件/真实主模型、CUDA/Vulkan、CPU-only兼容、进程回收、无公网访问检查。
- [x] 十万条1024维片段完整检索/重排序与资源性能报告。
- [x] 离线安装产物、操作与迁移说明，逐项记录实测范围。

## 本机交付状态

2026-11-08 已完成本地源码、完整离线 ZIP、实际隔离安装及卸载和验收报告。完整 Python 1066 项和前端 180 项通过。其余记录详见 `docs/offline-windows-acceptance.zh-CN.md`。

Task 6 的 CPU-only 无驱动实机、干净 Windows 断网环境、大显存原始辅助 CUDA 和完整网络审计仍待取得对应环境。十万片段为重复合成语料，1.7B CUDA 联合 RAG 通过，8B 默认 CPU 联合样例超时。当前交付标为未签名本机验证包。
