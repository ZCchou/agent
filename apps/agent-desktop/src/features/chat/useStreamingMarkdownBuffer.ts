import { useCallback, useEffect, useRef, useState } from "react";

const STREAM_RENDER_INTERVAL_MS = 60;

export function useStreamingMarkdownBuffer() {
  const buffer = useRef("");
  const timer = useRef<number | null>(null);
  const [content, setContent] = useState("");

  const cancelTimer = useCallback(() => {
    if (timer.current !== null) {
      window.clearTimeout(timer.current);
      timer.current = null;
    }
  }, []);

  const flush = useCallback(() => {
    cancelTimer();
    setContent(buffer.current);
  }, [cancelTimer]);

  const append = useCallback((delta: string) => {
    buffer.current += delta;
    if (timer.current === null) {
      timer.current = window.setTimeout(flush, STREAM_RENDER_INTERVAL_MS);
    }
  }, [flush]);

  const replace = useCallback((value: string) => {
    cancelTimer();
    buffer.current = value;
    setContent(value);
  }, [cancelTimer]);

  const clear = useCallback(() => replace(""), [replace]);

  useEffect(() => cancelTimer, [cancelTimer]);

  return { content, append, replace, clear, flush };
}
