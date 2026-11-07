export default {
  evidenceFragment: "引用片段", evidenceParentContext: "原文上下文", evidenceContextScopes: { clause: "完整条款", table: "完整表格", block: "原文段落" },
  indexVersions: "建立索引所用版本", indexVersionsHelp: "每份资料选择一个已确认版本，也可选择暂不纳入。有多个已确认版本时，需要明确选择。",
  excludeDocument: "暂不纳入此资料", chooseRevisionRequired: "请为有多个版本的资料选择一个版本，或选择暂不纳入。",
  currentIndex: "资料库当前索引", projectIndex: "此项目采用的索引", noIndex: "尚未建立索引", adoptCurrentIndex: "采用当前索引",
  evidenceClause: "条款", evidenceLocation: "原文位置", evidencePrevious: "上一页", evidenceNext: "下一页", evidenceRange: "第 {{start}}–{{end}} 字，共 {{total}} 字",
  evidenceUnavailable: "原文不可用、已撤回，或此项目无权访问。", evidenceError: "原文加载失败，请关闭后重新打开引用重试。",
  runtime: "辅助模型运行环境", loadMore: "加载更多片段",
  title: "本地资料库", description: "管理适航资料、辅助模型与项目检索范围。",
  models: "辅助模型", libraries: "资料库", refresh: "刷新", empty: "暂无记录。", loading: "正在处理…", error: "操作未完成，请检查后重试。",
  root: "模型存储位置", chooseRoot: "选择存储文件夹", importFolder: "选择模型文件夹", importFile: "选择模型文件", importModel: "导入模型", modelProfile: "模型类型", device: "运行设备", cpu: "处理器", cuda: "显卡", auto: "自动选择", probe: "检查可用性", remove: "移除", removeConfirm: "确认移除 {{name}}？", cancel: "取消", confirm: "确认", save: "保存", name: "名称", status: "状态", size: "大小", dimension: "向量维度", inspected: "已完成文件检查，请确认导入。", modelHelp: "选择本地模型文件夹或已适配的 ONNX 模型包。首次导入后需检查可用性。",
  createLibrary: "创建资料库", editLibrary: "编辑资料库", embedding: "检索模型", reranker: "排序模型", none: "不使用", project: "项目", chooseProject: "选择项目", bind: "用于此项目", unbind: "从此项目解绑", bound: "此项目已启用", bindingHelp: "项目对话只检索已启用的资料库。", libraryHelp: "先导入并检查检索模型，再创建资料库。确认资料版本后建立索引。", index: "建立检索索引", generation: "索引版本", importDocuments: "导入 DOCX / PDF / 文本", paste: "粘贴资料", content: "资料正文", titleField: "资料标题", submitPaste: "导入文本", documents: "资料版本", preview: "预览与确认", withdraw: "撤回版本", revision: "修订版本", source: "来源", documentNumber: "文件编号", warnings: "解析提示", acknowledge: "我已核查以上解析提示和资料内容", confirmDocument: "确认版本", reviewHelp: "核对标题、版本与来源。确认后可建立索引用于检索。", noWarnings: "未发现解析提示。", chunks: "解析片段", jobs: "后台任务", cancelJob: "取消任务", search: "检索证据", query: "检索问题", searchHelp: "请选择已绑定资料库的项目，检索并核对原文。", evidence: "证据原文", viewEvidence: "查看原文", untrusted: "资料内容来自外部来源，请核对其适用性。", close: "关闭", noProject: "请先选择项目。", noEmbedding: "尚无可用检索模型，请先在辅助模型中导入并检查。",
  statuses: { ready: "可用", imported: "已导入", pending: "待确认", confirmed: "已确认", withdrawn: "已撤回", queued: "排队中", running: "处理中", completed: "已完成", succeeded: "已完成", failed: "失败", cancelled: "已取消", error: "异常", unavailable: "不可用", loading: "加载中", available: "可用", draft: "待确认", indexed: "已索引", probing: "检查中" },
};
