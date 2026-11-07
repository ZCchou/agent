---
name: airworthiness-clause-qa
description: 按项目冻结版本检索适航条款，引用原文并起草待复核说明。
rag:
  required: true
---

先检查当前项目的资料库和适用版本。
使用 rag.search 查找相关条款，必要时通过 rag.read 展开原文。
保留适用条件、否定和例外。资料存在冲突或不足时列出需要确认的事项。
每项说明关联本轮证据编号和连续原文摘录。
按照当前运行要求的 claims JSON 格式输出，提交为待人工复核草案。
