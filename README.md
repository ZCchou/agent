# agent

<img src="apps/agent-desktop/src/assets/agent-logo.svg" width="72" height="72" alt="agent icon">

面向本地适航资料问答的 Windows 桌面智能体，使用 React、TypeScript、Tauri 2、Rust 和 Python。主模型通过 GGUF 文件导入，由应用托管 llama.cpp，支持 CPU、NVIDIA CUDA 和 Vulkan。辅助嵌入与重排序模型通过本地目录导入，支持已适配的 safetensors 和 ONNX 包。

项目资料库支持 DOCX、文本 PDF、TXT、Markdown 和粘贴条款，提供版本确认、混合检索、重排序、证据引用及 Skill 调用。适航分析结果保存为待人工复核草案。

默认采用适航离线模式。运行组件随完整离线交付包提供，模型权重从本地导入。下载模型可在联网电脑完成，再将完整文件复制到目标电脑。

## 本机运行

当前预览版为 `0.16.0-preview.3`，支持任务识别恢复、按需读取附件、分批填写 DOCX 和可调模型驻留。完整包支持首次安装及差异升级，安装器校验已有文件后，解压和替换变化的受管文件。本版检索组件有更新，旧版升级使用完整离线包。[当前版本下载](https://github.com/ZCchou/agent/releases/tag/v0.16.0-preview.3)

1. 下载统一版安装器和全部 `offline.partNN.zip`，放在同一个目录，运行安装器。安装器会校验并安装同目录离线组件。[发行页面](https://github.com/ZCchou/agent/releases)
2. 在“系统功能 → 模型”选择模型存储磁盘，导入 GGUF、选择设备并设为默认模型。发送消息时会自动加载已选模型，也可先执行加载测试。
3. 在“系统功能 → 模型 → 检索模型”配置嵌入和重排序模型。OCR 模型包随完整离线交付提供。
4. 创建资料库，导入并核对资料版本，等待处理和索引完成，在“项目设置 → 资料与 RAG”启用资料库。
5. 项目对话默认自动分流。纯寒暄直接回复，其他请求由所选本地模型参与意图识别。可开启输入框旁的“强制 RAG”，该设置会保存在当前对话。

### 任务识别与模型驻留

在“系统功能 → 模型 → 对话模型 → 自动模型选择”中调整等待和驻留设置。

| 设置 | 默认值 | 可选范围 |
|---|---|---|
| 意图识别超时 | 90 秒 | 30～180 秒 |
| 主模型空闲驻留 | 30 分钟 | 5、15、30、60 分钟，自定义 1～1440 分钟，手动卸载 |
| 辅助模型空闲驻留 | 30 分钟 | 5、15、30、60 分钟，自定义 1～1440 分钟，手动卸载 |

识别最多发起三次模型调用，首次失败后最多自动重试一次，最后一次由用户触发。格式兼容重发也计入次数。提交澄清答复后，按新修订重新计算识别预算。

驻留设置修改后立即重新计时。使用中或已固定的模型保持加载，辅助模型也受可用内存和设备资源调度约束。

### 通过附件填写模板

将 DOCX 用途设为“待填写模板”，将设备资料用途设为“参考资料”，说明填写整份或具体章节，并明确是否允许模拟设备数据。模型按需读取、检索和分批填写，生成稿保存来源、条款、物理页码、资料缺口及复核事项。文件发布完成后可通过“打开文件”核对 Word 内容。

完整离线包的安装、迁移和故障恢复操作见说明。统一版安装器无 Authenticode 签名，程序更新包和更新清单使用独立密钥签名。干净 Windows 断网环境等验收状态单独列出。[安装与迁移](docs/offline-windows-operations.zh-CN.md) · [验收报告](docs/offline-windows-acceptance.zh-CN.md)

## 统一版迁移与更新

统一版使用独立标识 `io.github.zcchou.agent.airworthiness`，设置中的迁移入口可读取 Document Preview 和 OCR Preview，保存快照并合并项目、对话、资料与模型登记。切换前关闭来源预览版。模型库位置由用户选择，复制校验并验证目标可用后才转移旧版独占文件，共享模型保留原位置。

应用提供手动检查更新。签名程序更新 ZIP 包含主程序、Core、应用 DLL 和更新辅助程序。模型权重与推理运行组件通过完整离线包或本地导入维护。运行组件不兼容时，更新界面会提示使用完整离线安装包。旧预览版迁入统一版需先手动安装完整交付。

统一版与 punkagent 各自使用独立的应用标识、数据目录、凭据命名空间、安装目录及更新通道。安装器为未进行 Authenticode 签名的预发布产物，程序更新采用独立密钥的签名校验。

## 下载主模型

以下文件已用于实机验收，大小采用十进制 GB。主模型在“模型”页面导入 GGUF 文件。

| 模型 | 验收文件 | 大小 | 下载入口 |
| --- | --- | --- | --- |
| Qwen3-1.7B | `Qwen3-1.7B-Q8_0.gguf` | 1.83 GB | [Hugging Face](https://huggingface.co/Qwen/Qwen3-1.7B-GGUF/tree/90862c4b9d2787eaed51d12237eafdfe7c5f6077) · [ModelScope 国内官方](https://modelscope.cn/models/Qwen/Qwen3-1.7B-GGUF/files) |
| Qwen3-8B | `Qwen3-8B-Q4_K_M.gguf` | 5.03 GB | [Hugging Face](https://huggingface.co/Qwen/Qwen3-8B-GGUF/tree/7c41481f57cb95916b40956ab2f0b139b296d974) · [ModelScope 国内官方](https://modelscope.cn/models/Qwen/Qwen3-8B-GGUF/files) |
| DeepSeek-R1-Distill-Qwen-7B，bartowski 社区 GGUF | `DeepSeek-R1-Distill-Qwen-7B-Q4_K_M.gguf` | 4.68 GB | [Hugging Face](https://huggingface.co/bartowski/DeepSeek-R1-Distill-Qwen-7B-GGUF/tree/361004151d4f4f6b446dc5e6d46fbf4422a80d5f) |

### 浏览器下载

打开表中的文件页面，找到对应 GGUF，点击该文件的下载按钮，保存到空间充足的磁盘。在 ModelScope 选择同名文件后，可用下方 SHA256 核对本次验收版本。

### 命令行下载

以下为 Windows PowerShell 示例。`$modelRoot` 可改为实际使用的磁盘目录，先安装下载工具，再按需要执行对应模型的命令。

```powershell
python -m pip install -U huggingface_hub
$modelRoot = 'F:\agent-models'

hf download Qwen/Qwen3-1.7B-GGUF Qwen3-1.7B-Q8_0.gguf --revision 90862c4b9d2787eaed51d12237eafdfe7c5f6077 --local-dir "$modelRoot\qwen3-1.7b"

hf download Qwen/Qwen3-8B-GGUF Qwen3-8B-Q4_K_M.gguf --revision 7c41481f57cb95916b40956ab2f0b139b296d974 --local-dir "$modelRoot\qwen3-8b"

hf download bartowski/DeepSeek-R1-Distill-Qwen-7B-GGUF DeepSeek-R1-Distill-Qwen-7B-Q4_K_M.gguf --revision 361004151d4f4f6b446dc5e6d46fbf4422a80d5f --local-dir "$modelRoot\deepseek-r1-distill-qwen-7b"
```

### 校验下载文件

```powershell
Get-FileHash -LiteralPath "$modelRoot\qwen3-1.7b\Qwen3-1.7B-Q8_0.gguf" -Algorithm SHA256
```

| 文件 | 本次验收 SHA256 |
| --- | --- |
| `Qwen3-1.7B-Q8_0.gguf` | `061b54daade076b5d3362dac252678d17da8c68f07560be70818cace6590cb1a` |
| `Qwen3-8B-Q4_K_M.gguf` | `d98cdcbd03e17ce47681435b5150e34c1417f50b5c0019dd560e4882c5745785` |
| `DeepSeek-R1-Distill-Qwen-7B-Q4_K_M.gguf` | `731ece8d06dc7eda6f6572997feb9ee1258db0784827e642909d9b565641937b` |

## 下载 RAG 辅助模型

### 已通过联合样例的 INT8 ONNX 组合

本机 i5-10200H、16 GiB 内存、GTX 1650 4 GiB 上，Qwen3-1.7B Q8_0 使用 CUDA、以下两个辅助模型使用 CPU，项目问答和适航 Skill 合成样例均通过。8B 纯 CPU 联合样例在 8,192 上下文下超时。具体耗时、样例范围和其他设备状态见验收报告。[实测配置与限制](docs/offline-windows-acceptance.zh-CN.md)

| 用途 | 包来源 | 实际权重文件 | 大小 |
| --- | --- | --- | --- |
| 嵌入，BGE-M3 dense | [Xenova/bge-m3](https://huggingface.co/Xenova/bge-m3/tree/4de13258303883538bd53b696b452bf8099f0858) | `onnx/model_int8.onnx` | 568 MB |
| 重排序，bge-reranker-v2-m3 | [onnx-community/bge-reranker-v2-m3-ONNX](https://huggingface.co/onnx-community/bge-reranker-v2-m3-ONNX/tree/6f5ff65298512715a1e669753bc754d2bc8f367b) | `onnx/model_int8.onnx` | 571 MB |

在同一 PowerShell 会话执行以下命令，下载配置、分词器和图文件。浏览器下载时也须保持这些文件及 `onnx` 子目录的结构。

```powershell
hf download Xenova/bge-m3 --revision 4de13258303883538bd53b696b452bf8099f0858 --local-dir "$modelRoot\bge-m3-int8" --include "README.md" "config.json" "tokenizer.json" "tokenizer_config.json" "special_tokens_map.json" "sentencepiece.bpe.model" "onnx/model_int8.onnx" "LICENSE*"

hf download onnx-community/bge-reranker-v2-m3-ONNX --revision 6f5ff65298512715a1e669753bc754d2bc8f367b --local-dir "$modelRoot\bge-reranker-int8" --include "README.md" "config.json" "tokenizer.json" "tokenizer_config.json" "special_tokens_map.json" "onnx/model_int8.onnx" "LICENSE*"
```

随后生成应用需要的 `rag-model.json`。下列写法在 Windows PowerShell 5.1 和 PowerShell 7 中均生成无 BOM 的 UTF-8 文件。

```powershell
$utf8 = New-Object System.Text.UTF8Encoding($false)
$embeddingConfig = @{
    schema_version = 1
    backend = 'onnx'
    profile = 'bge-m3'
    graph = 'onnx/model_int8.onnx'
    output = 'last_hidden_state'
    pooling = 'cls'
    dimension = 1024
    max_length = 8192
}
$rerankerConfig = @{
    schema_version = 1
    backend = 'onnx'
    profile = 'bge-reranker-v2-m3'
    graph = 'onnx/model_int8.onnx'
    output = 'logits'
    pooling = 'sigmoid'
    dimension = $null
    max_length = 8192
}
[IO.File]::WriteAllText("$modelRoot\bge-m3-int8\rag-model.json", ($embeddingConfig | ConvertTo-Json), $utf8)
[IO.File]::WriteAllText("$modelRoot\bge-reranker-int8\rag-model.json", ($rerankerConfig | ConvertTo-Json), $utf8)

Get-FileHash -LiteralPath "$modelRoot\bge-m3-int8\onnx\model_int8.onnx" -Algorithm SHA256
Get-FileHash -LiteralPath "$modelRoot\bge-reranker-int8\onnx\model_int8.onnx" -Algorithm SHA256
```

嵌入图 SHA256 为 `a206e10e995aa2a833924bcd725ba5dd6c3425cd34bac3cf2b5677cd2a1c51d6`，重排图为 `912fc1215c2dbff6499700534bd8d31253af01573861abbfc43afd1fab6cce5d`。

分别导入 `bge-m3-int8` 和 `bge-reranker-int8` 完整目录，配置项选择 `bge-m3`、`bge-reranker-v2-m3`，设备先设为 CPU，再执行加载测试。资料库分别绑定这两个模型。

这两份 ONNX 包来自社区转换。转换仓库独立 LICENSE 文件及上游许可证的核验情况见验收报告，模型按其自身许可使用。[来源与许可证记录](docs/offline-windows-acceptance.zh-CN.md)

### 原始 safetensors 组合

Qwen3-Embedding-0.6B 与 Qwen3-Reranker-0.6B 也可通过完整本地目录导入。以下命令保留配置、分词器、safetensors 权重及可能的分片索引。

```powershell
hf download Qwen/Qwen3-Embedding-0.6B --revision 97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3 --local-dir "$modelRoot\qwen3-embedding-0.6b" --include "*.json" "*.safetensors" "*.model" "*.txt" "*.tiktoken" "LICENSE*" "README.md"

hf download Qwen/Qwen3-Reranker-0.6B --revision e61197ed45024b0ed8a2d74b80b4d909f1255473 --local-dir "$modelRoot\qwen3-reranker-0.6b" --include "*.json" "*.safetensors" "*.model" "*.txt" "*.tiktoken" "LICENSE*" "README.md"
```

导入时分别选择 `qwen3-embedding-0.6b` 与 `qwen3-reranker-0.6b`。这两份完整原始模型的 CPU 嵌入或评分已实测，当前 4 GiB 显存电脑上的完整原始辅助 CUDA 验收受显存限制。BGE-M3 原始模型需要符合应用要求的 safetensors 包，权重转换的实测来源另行记录。[模型格式与适配规则](docs/rag-models.md) · [参考数值对照](docs/offline-windows-acceptance.zh-CN.md)

## 资料库与 Skill

项目绑定资料库后默认检索证据。内置 `airworthiness-clause-qa` Skill 声明 `rag.required`，执行器安排检索，技能可使用 `rag.search` 和 `rag.read` 查找片段及展开原文。引用发布前核对本轮实际证据、版本、哈希及摘录，专业判断保留人工复核状态。[资料库操作](docs/local-rag.md)

## Windows 开发

开发环境使用 Node.js 24.14.0、Rust 1.98.1/MSVC、Visual Studio C++ 构建工具、Microsoft Edge 和 Python 3.12。

```powershell
conda create -n agent python=3.12 -y
conda activate agent
python -m pip install -r services/agent-core/requirements-build.lock
python -m pip install --no-build-isolation --no-deps -e "services/agent-core[dev]"
cd apps/agent-desktop
npm ci
cd ../..
.\tools\build-python-sidecar.bat
.\tools\verify-python-sidecar.ps1
cd apps/agent-desktop
npm run tauri dev
```

前端预览使用 `apps/agent-desktop` 下的 `npm run dev`。本地推理需要准备并校验主模型和辅助运行组件，完整离线发行入口为 `tools/build-offline-windows.ps1`，运行组件布局及构建流程见说明。[辅助运行组件](docs/rag-models.md) · [离线交付](docs/offline-windows-operations.zh-CN.md)

## 检查

```powershell
# services/agent-core
python -m pytest
python evals/context/run_context_eval.py --verify-mutations
# apps/agent-desktop
npm run check
# apps/agent-desktop/src-tauri，先构建 sidecar
cargo fmt --all -- --check
cargo test --locked
cargo clippy --locked --all-targets --all-features -- -D warnings
# 仓库根目录
python -m unittest discover -s tools/tests -v
```

## 数据与运行约束

应用使用独立的 `io.github.zcchou.agent` 数据目录、`AGENT_*` 环境变量、`agent.db` 和系统凭据存储命名空间。适航运行使用本地生成端点，公网检索、远程图片和在线更新入口由离线策略控制。模型与运行组件通过本地文件加载，旧远程配置显示离线禁用状态。

项目文件操作校验授权根目录，并在支持的操作中创建恢复点。适航 RAG 运行限制任意终端执行；通用终端工具的操作仍以当前 Windows 用户权限执行，工作目录和权限审批需按具体工具范围核对。[安全说明](SECURITY.md)

当前本机交付、合成语料容量测试、模型模板差异、CPU 超时和其他环境待验收项均保留在报告中。公开发行的签名、源码状态与安装验收门禁见发行说明。[验收报告](docs/offline-windows-acceptance.zh-CN.md) · [发行流程](docs/releasing-windows.md)

## 目录

- `apps/agent-desktop` 包含 React 界面与 Tauri Host。
- `services/agent-core` 包含 Python Core、存储、推理托管和工具执行。
- `contracts` 保存共享协议与可靠性契约。
- `tools` 提供构建、校验和离线发行工具。

源码采用 MIT 许可证，第三方依赖和模型分别保留自身许可证。
