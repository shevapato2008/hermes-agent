/**
 * Terminal appearance helpers shared by the chat composer's xterm host:
 * theme colors and the layout-width-driven font tiers. Extracted from
 * ChatPage.tsx so the page file can keep shrinking (repo file-size ratchet).
 */

// Colors for the terminal body.  Matches the dashboard's dark teal canvas
// with cream foreground — we intentionally don't pick monokai or a loud
// theme, because the TUI's skin engine already paints the content; the
// terminal chrome just needs to sit quietly inside the dashboard.
export const DEFAULT_TERMINAL_BACKGROUND = "#000000";
export const DEFAULT_TERMINAL_FOREGROUND = "#f0e6d2";

export function buildTerminalTheme(background: string, foreground: string) {
  return {
    background,
    foreground,
    cursor: foreground,
    cursorAccent: background,
    selectionBackground:
      foreground.length === 7 ? `${foreground}44` : foreground,
  };
}

function readRootNumberVar(varName: string): number | null {
  if (typeof document === "undefined") return null;
  const root = document.documentElement;
  if (!root) return null;
  const inlineVal = root.style.getPropertyValue(varName).trim();
  const raw =
    inlineVal ||
    (typeof window !== "undefined" && typeof window.getComputedStyle === "function"
      ? window.getComputedStyle(root).getPropertyValue(varName).trim()
      : "");
  if (!raw) return null;
  const parsed = Number.parseFloat(raw);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
}

export function configuredFontSizeForWidth(layoutWidthPx: number, configured: number): number {
  if (layoutWidthPx < 360) return 10;
  if (layoutWidthPx < 460) return Math.max(11, configured - 2);
  if (layoutWidthPx < 580) return Math.max(12, configured - 1);
  return configured;
}

/**
 * CSS width for xterm font tiers.
 *
 * Prefer the terminal host's `clientWidth` — Chrome DevTools device mode often
 * keeps `window.innerWidth` at the full desktop value while the *drawn* layout
 * is phone-sized, which made us pick desktop font sizes (~14px) and look huge.
 */
export function terminalTierWidthPx(host: HTMLElement | null): number {
  if (typeof window === "undefined") return 1280;
  const fromHost = host?.clientWidth ?? 0;
  if (fromHost > 2) return Math.round(fromHost);
  const doc = document.documentElement?.clientWidth ?? 0;
  const vv = window.visualViewport;
  const inner = window.innerWidth;
  const vvw = vv?.width ?? inner;
  const layout = Math.min(inner, vvw, doc > 0 ? doc : inner);
  return Math.max(1, Math.round(layout));
}

export function terminalFontSizeForWidth(layoutWidthPx: number): number {
  const configured = readRootNumberVar("--theme-terminal-font-size");
  if (configured !== null) {
    return configuredFontSizeForWidth(layoutWidthPx, configured);
  }
  if (layoutWidthPx < 300) return 7;
  if (layoutWidthPx < 360) return 8;
  if (layoutWidthPx < 420) return 9;
  if (layoutWidthPx < 520) return 10;
  if (layoutWidthPx < 720) return 11;
  if (layoutWidthPx < 1024) return 12;
  return 14;
}

export function terminalLineHeightForWidth(layoutWidthPx: number): number {
  const configuredLh = readRootNumberVar("--theme-terminal-line-height");
  if (configuredLh !== null) {
    return layoutWidthPx < 640
      ? Math.max(1.08, Number((configuredLh - 0.08).toFixed(2)))
      : configuredLh;
  }
  return layoutWidthPx < 1024 ? 1.02 : 1.15;
}
