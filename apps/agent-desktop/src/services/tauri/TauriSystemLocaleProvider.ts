import {
  invoke,
} from "@tauri-apps/api/core";

import type {
  SystemLocaleProvider,
} from "../system/SystemLocaleProvider";


/**
 * 正式桌面环境的系统 Locale Provider。
 *
 * 实际 Locale 由 Rust Host 通过 sys-locale
 * 从操作系统读取。
 */
export class TauriSystemLocaleProvider
  implements SystemLocaleProvider
{
  async getLocale():
    Promise<string>
  {
    return invoke<string>(
      "get_system_locale",
    );
  }
}