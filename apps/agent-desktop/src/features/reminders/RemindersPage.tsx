import { Bell } from "lucide-react";
import { useTranslation } from "react-i18next";

import { PlaceholderPage } from "../../components/layout/PlaceholderPage";


export function RemindersPage() {
  const { t } =
    useTranslation("common");

  return (
    <PlaceholderPage
      icon={Bell}
      title={t(
        "pages.reminders.title",
      )}
      description={t(
        "pages.reminders.description",
      )}
    />
  );
}