import React from "react";

import ReactDOM
  from "react-dom/client";

import App
  from "./app/App";

import {
  initializeI18n,
} from "./i18n";


import "./styles/tokens.css";
import "./styles/global.css";
import "./styles/components.css";
import "katex/dist/katex.min.css";


async function bootstrap() {
  /*
   * 在 React 首次渲染以前完成语言解析，
   * 避免启动时先闪一下英文再切换中文。
   */
  await initializeI18n();


  ReactDOM
    .createRoot(
      document.getElementById(
        "root",
      )!,
    )
    .render(
      <React.StrictMode>
        <App />
      </React.StrictMode>,
    );
}


void bootstrap();
