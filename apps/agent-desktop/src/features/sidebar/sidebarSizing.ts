export const DEFAULT_SIDEBAR_WIDTH = 280;
export const MIN_SIDEBAR_WIDTH = 220;
export const MAX_SIDEBAR_WIDTH = 480;

export function clampSidebarWidth(width: number, viewportWidth = window.innerWidth): number {
  const responsiveMaximum = Math.max(
    MIN_SIDEBAR_WIDTH,
    Math.min(MAX_SIDEBAR_WIDTH, Math.floor(viewportWidth * 0.48)),
  );
  return Math.round(Math.min(responsiveMaximum, Math.max(MIN_SIDEBAR_WIDTH, width)));
}
