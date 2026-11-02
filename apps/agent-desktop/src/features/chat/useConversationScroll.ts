import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

const BOTTOM_THRESHOLD = 64;

export function pauseProcessFollowing(toggle: HTMLElement) {
  toggle.dispatchEvent(new Event("work-process-expand", { bubbles: true }));
}

export function useConversationScroll(conversationKey: string, contentVersion: string, forceBottomVersion: number) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const followRef = useRef(true);
  const initializedRef = useRef(false);
  const [showBottomButton, setShowBottomButton] = useState(false);
  const [hasNewContent, setHasNewContent] = useState(false);

  const distanceFromBottom = useCallback(() => {
    const node = scrollRef.current;
    return node ? node.scrollHeight - node.scrollTop - node.clientHeight : 0;
  }, []);

  const scrollToBottom = useCallback((smooth = true) => {
    const node = scrollRef.current;
    if (!node) return;
    const reduced = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
    node.scrollTo({ top: node.scrollHeight, behavior: smooth && !reduced ? "smooth" : "auto" });
    followRef.current = true;
    setShowBottomButton(false);
    setHasNewContent(false);
  }, []);

  const onScroll = useCallback(() => {
    const nearBottom = distanceFromBottom() <= BOTTOM_THRESHOLD;
    followRef.current = nearBottom;
    setShowBottomButton(!nearBottom);
    if (nearBottom) setHasNewContent(false);
  }, [distanceFromBottom]);

  useLayoutEffect(() => {
    initializedRef.current = false;
    followRef.current = true;
    setShowBottomButton(false);
    setHasNewContent(false);
    const frame = window.requestAnimationFrame(() => {
      scrollToBottom(false);
      initializedRef.current = true;
    });
    return () => window.cancelAnimationFrame(frame);
  }, [conversationKey, scrollToBottom]);

  useLayoutEffect(() => {
    if (!initializedRef.current) return;
    if (followRef.current) scrollToBottom(false);
    else {
      setShowBottomButton(true);
      setHasNewContent(true);
    }
  }, [contentVersion, scrollToBottom]);

  useEffect(() => {
    if (forceBottomVersion <= 0) return;
    const frame = window.requestAnimationFrame(() => scrollToBottom(true));
    return () => window.cancelAnimationFrame(frame);
  }, [forceBottomVersion, scrollToBottom]);

  useEffect(() => {
    const content = contentRef.current;
    if (!content || typeof ResizeObserver === "undefined") return;
    // Explicitly opening retained text is a reading action. Do not pull the
    // reader past its beginning when the disclosure changes content height.
    const pauseFollowing = () => { followRef.current = false; };
    content.addEventListener("work-process-expand", pauseFollowing);
    let previousHeight = content.getBoundingClientRect().height;
    const observer = new ResizeObserver(() => {
      const nextHeight = content.getBoundingClientRect().height;
      if (nextHeight === previousHeight) return;
      previousHeight = nextHeight;
      if (followRef.current) scrollToBottom(false);
      else {
        setShowBottomButton(true);
        setHasNewContent(true);
      }
    });
    observer.observe(content);
    return () => {
      observer.disconnect();
      content.removeEventListener("work-process-expand", pauseFollowing);
    };
  }, [conversationKey, scrollToBottom]);

  return { scrollRef, contentRef, showBottomButton, hasNewContent, onScroll, scrollToBottom };
}
