/**
 * 操作系统 Locale 的抽象边界。
 *
 * UI 和 i18next 不关心 Locale
 * 是通过 navigator、Rust、Windows API
 * 还是其他方式获得。
 */
export interface SystemLocaleProvider {
  getLocale():
    Promise<string>;
}