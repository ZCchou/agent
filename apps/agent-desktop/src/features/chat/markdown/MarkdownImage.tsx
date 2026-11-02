import { Image as ImageIcon, ShieldCheck } from "lucide-react";
import { useEffect, useState, type ComponentPropsWithoutRef } from "react";
import { useTranslation } from "react-i18next";
import { useRemoteContentPermission } from "../../../contexts/RemoteContentPermissionContext";
import { classifyImage, remoteImageHost } from "./markdownPolicy";

export function MarkdownImage({ src = "", alt = "" }: ComponentPropsWithoutRef<"img">) {
  const { t } = useTranslation("chat");
  const permission = useRemoteContentPermission();
  const [failed, setFailed] = useState(false);
  const allowedUrl = classifyImage(src) === "remote_https" ? src : null;

  useEffect(() => {
    setFailed(false);
  }, [src, permission.policy]);

  if (!allowedUrl) {
    return <span className="markdown-image-placeholder markdown-image-placeholder--blocked">
      <ImageIcon size={18} /><span>{alt || t("markdown.imageBlocked")}</span>
    </span>;
  }

  if (permission.policy !== "allow") {
    return <span className="markdown-image-placeholder">
      <span className="markdown-image-placeholder__icon"><ShieldCheck size={19} /></span>
      <span className="markdown-image-placeholder__copy">
        <strong>{alt || t("markdown.remoteImage")}</strong>
        <small>{t("markdown.remoteImageSource", { host: remoteImageHost(allowedUrl) })}</small>
        <small>{t("markdown.remoteImageNotice")}</small>
      </span>
      <button
        type="button"
        disabled={permission.loading || permission.saving}
        onClick={() => void permission.allowAllRemoteImages().catch(() => undefined)}
      >
        {permission.saving ? t("markdown.imagePermissionSaving") : t("markdown.allowAllImages")}
      </button>
      {permission.error ? <small className="markdown-image-placeholder__error">{t("markdown.imagePermissionError")}</small> : null}
    </span>;
  }

  if (failed) {
    return <span className="markdown-image-placeholder markdown-image-placeholder--blocked">
      <ImageIcon size={18} /><span>{t("markdown.imageLoadFailed", { alt: alt || t("markdown.remoteImage") })}</span>
    </span>;
  }

  return (
    <img
      className="markdown-image"
      src={allowedUrl}
      alt={alt}
      loading="lazy"
      decoding="async"
      referrerPolicy="no-referrer"
      onError={() => setFailed(true)}
    />
  );
}
