const diagnostics = {
  title:
    "诊断",

  description:
    "检查 Agent Core 和应用运行状态。",

  core: {
    label:
      "Agent Core 状态",

    online:
      "在线",

    offline:
      "离线",
    unresponsive: "进程运行中，但无响应",

    ready:
      "Agent Core 正在运行，可以正常使用。",

    unavailable:
      "Agent Core 当前不可用。",
    unhealthy: "Core 进程仍在运行，但健康检查失败。",

    refresh:
      "刷新",
  },
};

export default diagnostics;
