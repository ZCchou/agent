import { AppUpdateCard } from "../updater/AppUpdateCard";
import { ProfileMigrationCard } from '../migration/ProfileMigrationCard';
import { ChevronRight, Languages, Trash2 } from "lucide-react";

import {
  useEffect,
  useState,
} from "react";

import {
  useTranslation,
} from "react-i18next";

import {
  Card,
} from "../../components/ui/Card";

import {
  changeInterfaceLanguage,
} from "../../i18n";

import {
  settingsRepository,
} from "../../services/runtime";
import { Palette } from "lucide-react";
import { readInterfaceStyle, saveInterfaceStyle } from "./interfaceStyle";
import { TrashSettingsPage } from "./TrashSettingsPage";

import type {
  InterfaceLanguage,
} from "../../types/settings";


export function SettingsPage() {
  const {
    t,
  } =
    useTranslation(
      "common",
    );


  const [
    languagePreference,
    setLanguagePreference,
  ] =
    useState<
      InterfaceLanguage
    >(
      "system",
    );


  const [
    loading,
    setLoading,
  ] =
    useState(true);


  const [
    saving,
    setSaving,
  ] =
    useState(false);


  const [
    error,
    setError,
  ] =
    useState(false);

  const [style, setStyle] = useState(readInterfaceStyle);
  const [styleSaveError, setStyleSaveError] = useState(false);
  const [section, setSection] = useState<"root" | "trash">("root");


  useEffect(
    () => {
      let cancelled =
        false;


      async function loadSettings() {
        try {
          const settings =
            await settingsRepository
              .getAppSettings();


          if (!cancelled) {
            setLanguagePreference(
              settings
                .general
                .interface_language,
            );

            setError(false);
          }
        } catch (loadError) {
          console.error(
            "Failed to load application settings:",
            loadError,
          );


          if (!cancelled) {
            setError(true);
          }
        } finally {
          if (!cancelled) {
            setLoading(false);
          }
        }
      }


      void loadSettings();


      return () => {
        cancelled =
          true;
      };
    },
    [],
  );


  async function handleLanguageChange(
    language:
      InterfaceLanguage,
  ) {
    setSaving(true);
    setError(false);


    try {
      const settings =
        await changeInterfaceLanguage(
          language,
        );


      setLanguagePreference(
        settings
          .general
          .interface_language,
      );
    } catch (saveError) {
      console.error(
        "Failed to update interface language:",
        saveError,
      );


      /*
       * 持久化失败时不更新 selector，
       * 防止 UI 与 Rust SettingsManager 状态不一致。
       */
      setError(true);
    } finally {
      setSaving(false);
    }
  }


  if (section === "trash") {
    return <TrashSettingsPage onBack={() => setSection("root")} />;
  }

  return (
    <div className="settings-shell">
      <div className="page-intro">
        <h2>
          {t(
            "pages.settings.title",
          )}
        </h2>

        <p>
          {t(
            "pages.settings.description",
          )}
        </p>
      </div>


      <Card className="settings-language-card">
        <div className="settings-language-card__icon"><Palette size={24} /></div>
        <div className="settings-language-card__copy"><strong>UI</strong></div>
        <label className="settings-language-control">
          <span>{t("settings.interfaceStyle.label")}</span>
          <select value={style} onChange={event => {
            const next = event.target.value === "classic" ? "classic" : "ragent";
            try {
              saveInterfaceStyle(next);
              setStyle(next);
              setStyleSaveError(false);
            } catch {
              setStyleSaveError(true);
            }
          }}>
            <option value="ragent">ragent</option>
            <option value="classic">{t("settings.interfaceStyle.classic")}</option>
          </select>
        </label>
        {styleSaveError && <p role="alert">{t("settings.interfaceStyle.error")}</p>}
      </Card>
      <AppUpdateCard />
      <ProfileMigrationCard />
      <Card className="settings-language-card">
        <div className="settings-language-card__icon">
          <Languages
            size={24}
          />
        </div>


        <div className="settings-language-card__copy">
          <strong>
            {t(
              "settings.language.title",
            )}
          </strong>

          <span>
            {t(
              "settings.language.description",
            )}
          </span>
        </div>


        <label className="settings-language-control">
          <span>
            {t(
              "settings.language.label",
            )}
          </span>


          <select
            value={
              languagePreference
            }
            disabled={
              loading ||
              saving
            }
            onChange={(
              event,
            ) =>
              void handleLanguageChange(
                event.target
                  .value as InterfaceLanguage,
              )
            }
          >
            <option value="system">
              {t(
                "settings.language.system",
              )}
            </option>

            <option value="en">
              {t(
                "settings.language.english",
              )}
            </option>

            <option value="zh-CN">
              {t(
                "settings.language.chinese",
              )}
            </option>
          </select>
        </label>


        <p className="settings-language-card__hint">
          {error
            ? t(
                "settings.language.error",
              )
            : t(
                "settings.language.hint",
              )}
        </p>
      </Card>


      <nav className="settings-secondary-navigation" aria-label={t("settings.moreSettings")}>
        <Card className="settings-navigation-card">
          <div className="settings-language-card__icon">
            <Trash2 size={24} />
          </div>

          <div className="settings-language-card__copy">
            <strong>{t("settings.trash.title")}</strong>
            <span>{t("settings.trash.description")}</span>
          </div>

        <button
          className="settings-navigation-card__action"
          type="button"
          onClick={() => setSection("trash")}
          aria-label={t("settings.trash.open")}
        >
          <span>{t("settings.trash.open")}</span>
          <ChevronRight size={16} />
        </button>
        </Card>
      </nav>
    </div>
  );
}
