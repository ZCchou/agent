# 本地辅助模型

应用接受四种固定配置的完整本地目录。Embedding 返回 L2 归一化的 1024 维稠密向量，reranker 返回每条候选文段的 0 到 1 分数。

| 配置 | 目录权重 | 推理规则 |
| --- | --- | --- |
| `qwen3-embedding-0.6b` | Qwen3-Embedding-0.6B safetensors | 查询添加固定检索任务说明，文段保持原文，最后有效 token pooling 和 L2 归一化 |
| `qwen3-reranker-0.6b` | Qwen3-Reranker-0.6B safetensors | 官方 yes/no 提示词格式，两个最终 token logits 的 softmax，返回 yes 概率 |
| `bge-m3` | BAAI/bge-m3 safetensors | CLS pooling 和 L2 归一化，仅使用 dense 表示 |
| `bge-reranker-v2-m3` | BAAI/bge-reranker-v2-m3 safetensors | query-document 成对编码，单 logit sigmoid |

## 导入与生命周期

从模型页面选择完整目录并明确选择 CPU 或 CUDA。目录需要 `config.json`、`tokenizer_config.json`、实际 tokenizer 资源以及完整 `model.safetensors` 或分片权重和 `model.safetensors.index.json`。单独的权重文件不足以构成可运行目录。

导入先检查架构、分片映射、safetensors 字节范围和目录内容，再向所选模型根目录的临时目录复制全部文件。复制前后逐文件计算 SHA-256，通过复核后原子发布，最后写入模型注册表。ID 由配置和文件内容生成，重复导入返回相同 ID。每次 worker 加载模型时在后台线程复核全部已管理文件的哈希。同一已加载模型的连续批次复核所有文件的路径、inode、大小和纳秒级修改/创建时间，身份变化时重新计算哈希。外部目录的后续变化不影响已复制模型。

模型根目录变更会复制和复核已有模型，然后切换注册表。旧根目录保留原始副本。删除操作先将当前已管理副本原子移出活动目录，再移除注册项并清理文件。若其他程序占用文件而导致清理失败，返回 `cleanup_pending` 路径，模型仍已注销且可重新导入。资料库使用的模型是否允许删除由资料库服务校验。

导入状态为 `imported`。探测会实际检查引擎、CUDA 或 ONNX provider，并尝试加载 tokenizer 和模型，成功后状态变为 `ready`，失败为 `unavailable` 并返回具体原因。权重导入成功和推理依赖可用分别检测。

应用拒绝 pickle/PyTorch `.bin`、`.pt`、`.pth`、Python 脚本、可执行文件、符号链接和 junction，以及声明 `auto_map` 或自定义 pipeline 的配置。模型加载始终启用 `local_files_only=True`、`trust_remote_code=False` 和 `use_safetensors=True`。模型目录不能指定自定义加载代码。

## 适配后的 ONNX BGE 包

ONNX 首期支持上述两个 BGE 配置。除 config 和 tokenizer 外，包根目录需要 `rag-model.json`。下例描述输出 token hidden states 的 embedding 图。

```json
{
  "schema_version": 1,
  "backend": "onnx",
  "profile": "bge-m3",
  "graph": "model.onnx",
  "output": "last_hidden_state",
  "pooling": "cls",
  "dimension": 1024,
  "max_length": 8192
}
```

`bge-m3` 还接受 `pooling` 为 `normalized_dense`，此时图输出为 `[batch,1024]`，worker 再执行 L2 归一化。`bge-reranker-v2-m3` 使用 `pooling` 为 `sigmoid`、`dimension` 为 `null`，图输出 `[batch,1]` 未归一化 logits，worker 负责 sigmoid。

图输入必须包含 int64 或 int32 `input_ids` 和 `attention_mask`，可选 `token_type_ids`。长度上限为 8192。ONNX checker 检查实际图，所有 external-data 相对路径、文件存在性和字节范围均受检，模型根目录外路径与自定义算子域被拒绝。external-data 文件与图一起复制和哈希。普通 Hugging Face ONNX 导出目录需要按此约定完成适配。

## 隔离运行与打包

每个设备使用一个独立、串行的推理子进程。IPC 为 stdin/stdout JSONL，传输帧使用 JSON ASCII 转义以兼容 Windows 冻结进程的系统编码，解码后保留中文文本和路径。请求取消或超时会终止进程，下次请求重新启动。应用退出时关闭全部推理进程。CPU 为默认设备，CUDA 需要用户明确选择并通过真实探测，ONNX CUDA provider 初始化失败会返回错误。

worker 设置 `HF_HUB_OFFLINE`、`TRANSFORMERS_OFFLINE`、`HF_DATASETS_OFFLINE` 和关闭遥测。标准输出专用于协议，加载日志重定向至标准错误。整个流程使用本地文件和进程管道。

在 Windows x64 CPython 3.12 上运行以下命令创建固定版本的独立 runtime。构建时需要下载依赖。

```powershell
./tools/build-rag-runtime.ps1 -Device cpu
./tools/build-rag-runtime.ps1 -Device cuda
```

CPU 使用 `requirements-rag-cpu.lock` 中的 PyTorch CPU wheel 和 ONNX Runtime。CUDA 使用对应 CUDA 12.8 PyTorch wheel 与 ONNX Runtime GPU。CUDA 构建仍需在目标硬件验证驱动、CUDA 和 cuDNN provider。交付时将整个 `rag-runtime` 目录放在 `agent-core.exe` 旁。目录含独立 worker、原生依赖和 `runtime.json`。应用只从该固定位置加载 worker，核对协议版本、全部文件清单和 SHA-256，拒绝新增、缺失或变化的文件。状态轮询复核完整文件 stat 身份并缓存已核验哈希，身份变化或启动/重启 worker 时执行完整 SHA-256 核验。该清单属于受信任安装包，随安装包一同分发。

源码模式使用当前 Python 解释器。开发者需要为该解释器安装 CPU 或 CUDA lock。运行测试时的微型随机权重和 ONNX 合成图覆盖适配器数值和 IPC，生产模型质量与完整权重的容量验证需要实际官方模型目录。
