import { Image as ImageIcon } from "lucide-react";
import { useState, type ComponentPropsWithoutRef } from "react";
import { classifyImage } from "./markdownPolicy";
export function MarkdownImage({ src = "", alt = "" }: ComponentPropsWithoutRef<"img">) {
  const [failed, setFailed] = useState(false);
  if (classifyImage(src) !== "local" || failed) return <span className="markdown-image-placeholder markdown-image-placeholder--blocked"><ImageIcon size={18} /><span>{alt || "离线模式已拦截远程图片"}</span></span>;
  return <img className="markdown-image" src={src} alt={alt} loading="lazy" decoding="async" referrerPolicy="no-referrer" onError={() => setFailed(true)} />;
}
