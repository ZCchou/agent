const diagnostics = {
  title:
    "Diagnostics",

  description:
    "Inspect Agent Core and application health.",

  core: {
    label:
      "Agent Core Status",

    online:
      "Online",

    offline:
      "Offline",
    unresponsive: "Running, not responding",

    ready:
      "Agent Core is running and ready.",

    unavailable:
      "Agent Core is not currently available.",
    unhealthy: "The process is running, but its health check failed.",

    refresh:
      "Refresh",
  },
};

export default diagnostics;
