import i18n from "i18next";

import {
  initReactI18next,
} from "react-i18next";


import enCommon
  from "./locales/en/common";

import enChat
  from "./locales/en/chat";

import enDiagnostics
  from "./locales/en/diagnostics";


import zhCommon
  from "./locales/zh-CN/common";

import zhChat
  from "./locales/zh-CN/chat";

import zhDiagnostics
  from "./locales/zh-CN/diagnostics";


import {
  resolveLanguage,
} from "./language";

import {
  settingsRepository,
  systemLocaleProvider,
} from "../services/runtime";

import type {
  AppSettings,
  InterfaceLanguage,
} from "../types/settings";


/**
 * i18next 初始化 Promise。
 *
 * 防止未来多个模块同时请求初始化时
 * 重复执行 init()。
 */
let initializationPromise:
  Promise<void> | null =
    null;


/**
 * 根据持久设置解析当前真正应该使用的语言。
 *
 * 只有 preference=system 时才请求 Native Locale。
 */
async function resolveActiveLanguage(
  preference:
    InterfaceLanguage,
) {
  if (
    preference !== "system"
  ) {
    return resolveLanguage(
      preference,
    );
  }


  const systemLocale =
    await systemLocaleProvider
      .getLocale();


  return resolveLanguage(
    preference,
    systemLocale,
  );
}


function updateDocumentLanguage(
  language: string,
): void {
  if (
    typeof document ===
    "undefined"
  ) {
    return;
  }


  document.documentElement.lang =
    language;
}


/**
 * 初始化 agent 国际化系统。
 *
 * 正式桌面环境中的语言来源：
 *
 * SettingsManager
 * +
 * Native System Locale
 *
 * 如果基础设施异常，界面降级为英文，
 * 但不会因为语言系统失败而导致整个 React UI 无法启动。
 */
export function initializeI18n():
  Promise<void>
{
  if (
    initializationPromise
  ) {
    return initializationPromise;
  }


  initializationPromise =
    (async () => {
      let activeLanguage:
        "en" | "zh-CN" =
        "en";


      try {
        const settings =
          await settingsRepository
            .getAppSettings();


        activeLanguage =
          await resolveActiveLanguage(
            settings
              .general
              .interface_language,
          );
      } catch (error) {
        console.error(
          "Failed to resolve agent interface language. Falling back to English.",
          error,
        );
      }


      await i18n
        .use(
          initReactI18next,
        )
        .init({
          resources: {
            en: {
              common:
                enCommon,

              chat:
                enChat,

              diagnostics:
                enDiagnostics,
            },


            "zh-CN": {
              common:
                zhCommon,

              chat:
                zhChat,

              diagnostics:
                zhDiagnostics,
            },
          },


          lng:
            activeLanguage,


          fallbackLng:
            "en",


          defaultNS:
            "common",


          interpolation: {
            escapeValue:
              false,
          },


          react: {
            useSuspense:
              false,
          },
        });


      updateDocumentLanguage(
        activeLanguage,
      );


      i18n.on(
        "languageChanged",
        updateDocumentLanguage,
      );
    })();


  return initializationPromise;
}


/**
 * 修改正式的 GLOBAL 界面语言设置。
 *
 * 顺序非常重要：
 *
 * 1. Rust SettingsManager 持久化
 * 2. 根据最终设置解析实际语言
 * 3. i18next 更新 UI
 *
 * 如果 Rust 持久化失败，
 * UI 不应该假装修改成功。
 */
export async function changeInterfaceLanguage(
  language:
    InterfaceLanguage,
): Promise<AppSettings> {
  const updatedSettings =
    await settingsRepository
      .updateInterfaceLanguage(
        language,
      );


  let activeLanguage: "en" | "zh-CN";
  try {
    activeLanguage = await resolveActiveLanguage(updatedSettings.general.interface_language);
  } catch (error) {
    console.error("Failed to resolve system locale after saving language preference.", error);
    activeLanguage = "en";
  }


  await i18n.changeLanguage(
    activeLanguage,
  );


  return updatedSettings;
}


export default i18n;
