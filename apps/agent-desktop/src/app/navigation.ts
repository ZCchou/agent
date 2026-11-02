import type {
  LucideIcon,
} from "lucide-react";

import {
  Bell,
  Bot,
  Brain,
  Folder,
  MessageCircle,
  Settings,
  ShieldCheck,
  Sparkles,
  Stethoscope,
} from "lucide-react";


export type AppPage =
  | "chat"
  | "projects"
  | "models"
  | "abilities"
  | "memory"
  | "reminders"
  | "approvals"
  | "settings"
  | "diagnostics";


export interface NavigationItem {
  id: AppPage;
  availability?: "ready" | "planned";

  /*
   * 这里只保存 translation key，
   * 永远不保存用户可见文本。
   */
  labelKey: string;

  icon: LucideIcon;

  section:
    | "main"
    | "system";
}


export const navigationItems:
  NavigationItem[] = [
    {
      id: "chat",
      labelKey:
        "navigation.chat",
      icon:
        MessageCircle,
      section:
        "main",
    },

    {
      id: "projects",
      labelKey:
        "navigation.projects",
      icon:
        Folder,
      section:
        "main",
    },

    {
      id: "models",
      labelKey:
        "navigation.models",
      icon:
        Bot,
      section:
        "main",
    },

    {
      id: "abilities",
      labelKey:
        "navigation.abilities",
      icon:
        Sparkles,
      section:
        "main",
    },

    {
      id: "memory",
      labelKey:
        "navigation.memory",
      icon:
        Brain,
      section:
        "main",
    },

    {
      id: "reminders",
      availability: "planned",
      labelKey:
        "navigation.reminders",
      icon:
        Bell,
      section:
        "main",
    },

    {
      id: "approvals",
      labelKey:
        "navigation.approvals",
      icon:
        ShieldCheck,
      section:
        "main",
    },

    {
      id: "settings",
      labelKey:
        "navigation.settings",
      icon:
        Settings,
      section:
        "system",
    },

    {
      id: "diagnostics",
      labelKey:
        "navigation.diagnostics",
      icon:
        Stethoscope,
      section:
        "system",
    },
  ];


/*
 * 页面标题同样只存 translation key。
 */
export const pageTitleKeys:
  Record<AppPage, string> = {
    chat:
      "pages.chat.title",

    projects:
      "pages.projects.title",

    models:
      "pages.models.title",

    abilities:
      "pages.abilities.title",

    memory:
      "pages.memory.title",

    reminders:
      "pages.reminders.title",

    approvals:
      "pages.approvals.title",

    settings:
      "pages.settings.title",

    diagnostics:
      "pages.diagnostics.title",
  };
