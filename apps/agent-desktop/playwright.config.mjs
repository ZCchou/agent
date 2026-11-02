import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests-ui",
  timeout: 20_000,
  // The desktop test shell shares one Vite server and launches a full Edge
  // process per worker. Serial execution is fast for this small suite and
  // avoids Windows resource contention being reported as page-load failures.
  workers: 1,
  webServer: {
    command: "npm run dev -- --host 127.0.0.1",
    url: "http://127.0.0.1:5173",
    reuseExistingServer: true,
  },
  use: { browserName: "chromium", channel: "msedge", headless: true, viewport: { width: 900, height: 500 } },
});
