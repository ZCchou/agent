import type {
  InterfaceLanguage,
} from "../types/settings";


export const LANGUAGE_PREFERENCES = [
  "system",
  "en",
  "zh-CN",
] as const;


export type LanguagePreference =
  InterfaceLanguage;


export type SupportedLanguage =
  Exclude<
    LanguagePreference,
    "system"
  >;


/**
 * 判断未知值是否属于 agent
 * 当前支持的持久界面语言设置。
 */
export function isLanguagePreference(
  value: unknown,
): value is LanguagePreference {
  return (
    typeof value === "string" &&
    LANGUAGE_PREFERENCES.includes(
      value as LanguagePreference,
    )
  );
}


/**
 * 将 Native OS Locale 映射到
 * agent 当前真正拥有的语言资源。
 *
 * 当前 MVP：
 *
 * zh-* -> zh-CN
 * 其他 -> en
 */
export function normalizeSystemLanguage(
  locale?: string,
): SupportedLanguage {
  const normalized =
    locale
      ?.trim()
      .toLowerCase() ?? "";


  if (
    normalized.startsWith(
      "zh",
    )
  ) {
    return "zh-CN";
  }


  return "en";
}


/**
 * 根据持久语言设置和系统 Locale，
 * 得到 i18next 最终使用的资源语言。
 */
export function resolveLanguage(
  preference:
    LanguagePreference,

  systemLocale?: string,
): SupportedLanguage {
  if (
    preference === "system"
  ) {
    return normalizeSystemLanguage(
      systemLocale,
    );
  }


  return preference;
}