import { Image, ShieldQuestion } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Button } from "../../components/ui/Button";

interface Props {
  modelId: string;
  busy: boolean;
  onConfirm(): void;
  onCancel(): void;
}

export function VisionCapabilityPrompt({ modelId, busy, onConfirm, onCancel }: Props) {
  const { t } = useTranslation("chat");
  return <section className="vision-capability-prompt" role="alert" aria-live="polite">
    <span className="vision-capability-prompt__icon" aria-hidden="true"><ShieldQuestion size={18} /></span>
    <div>
      <strong>{t("attachments.visionConfirmTitle")}</strong>
      <p>{t("attachments.visionConfirmDescription", { model: modelId })}</p>
    </div>
    <div className="vision-capability-prompt__actions">
      <Button type="button" variant="ghost" disabled={busy} onClick={onCancel}>{t("attachments.visionCancel")}</Button>
      <Button type="button" disabled={busy} icon={<Image size={15} />} onClick={onConfirm}>
        {busy ? t("attachments.visionConfirming") : t("attachments.visionTry")}
      </Button>
    </div>
  </section>;
}
