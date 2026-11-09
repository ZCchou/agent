# RAG 工作区预览版发布与更新

发布版本为 `0.15.0-ocr-preview.2`，Windows 11 x64，单用户离线预览版。数据库 schema28，IPC21。

## 下载选择

首次安装下载 `agent-0.15.0-ocr-preview.2-windows-x64-setup.exe` 及同版本全部六个 `offline.partNN.zip`，放在同一目录运行安装器。组件包含 llama.cpp CPU/CUDA/Vulkan、RAG CPU/CUDA、OCR CPU、OCR 模型、WebView2 和微软运行库。主对话、嵌入和重排序权重按 README 下载并在应用中导入。

已安装公开 `0.15.0-ocr-preview.1` 时，下载 `agent-0.15.0-ocr-preview.1-to.2-delta.zip`。完整解压后关闭应用，运行 `UPDATE.cmd`，选择旧 `agent-desktop.exe` 所在目录。此包沿用 `io.github.zcchou.agent.ocrpreview` 的数据目录。

本机 Document Preview 用户使用 `agent-0.15.0-document-preview.1-to.2-delta.zip`，适用此前 schema27、源提交 `4da8068` 的 Document Preview 三件套。此包沿用 `io.github.zcchou.agent.documentpreview` 的数据目录。

增量包校验旧三件套和组件清单，应用文件不匹配时给出错误。成功更新提示 `UPDATED`，重复执行提示 `ALREADY_UPDATED`。组件发生变化时使用完整安装包。

## 数据和恢复

新 Core 首次启动时自动迁移数据库并生成 `agent.db.pre-schema28-*.bak` 备份。项目、对话、资料、索引和历史证据继续使用原身份。

安装版在 `.managed-runtime/application-updates` 保存原应用文件，由原卸载程序统一清理。便携版在 `.agent-app-update` 保存备份。替换失败时恢复旧文件。断电后关闭应用并重新执行同一增量包，更新程序核验事务和备份后恢复并继续。

启动新版完成数据库迁移后，如需回退，应配套恢复旧三件套和同一代数据库备份。备份数据库恢复前应关闭所有对应应用进程，保留当前数据库副本。

## 本机验证范围

RAG 改造包括分离的资料库页面、两步导入、阶段任务、最终索引统一发布、版本历史、项目多资料库配置和自动采用就绪索引。

DO-160F 中文 PDF 的300物理页、64页OCR缓存和663片段已在隔离数据中验证。资料库检索与项目检索各返回8条证据。Qwen3 1.7B Q8 CPU 的逐字摘录对话通过引用校验，普通分析问题曾改写引文并触发 `rag_quote_invalid`。

测试电脑为 i5-10200H、GTX1650 4GB。前两次完整检索各约3分半。首次全量 OCR 耗时、干净电脑断网安装及 WPS 专有二进制格式等项目继续记录为待验收。

安装器和应用采用未签名预览构建，自动更新关闭。公开资产包含程序、运行组件与说明，用户数据库、模型库和业务文件由各应用数据目录保存。
