// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";

import {
  configuredFontSizeForWidth,
  terminalFontSizeForWidth,
  terminalLineHeightForWidth,
} from "./terminal-appearance";
import {
  CHAT_NARROW_MEDIA_QUERY,
  isNarrowChatViewport,
  readChatPanelCollapsed,
} from "./chat-panel-collapsed";
import { TERMINAL_FONT_FAMILY } from "./terminal-font-refit";

describe("terminal-appearance theme metric tiers", () => {
  afterEach(() => {
    document.documentElement.style.removeProperty("--theme-terminal-font-size");
    document.documentElement.style.removeProperty("--theme-terminal-line-height");
  });

  it("preserves legacy width tiers when theme terminal CSS variables are unset", () => {
    expect(terminalFontSizeForWidth(280)).toBe(7);
    expect(terminalFontSizeForWidth(340)).toBe(8);
    expect(terminalFontSizeForWidth(400)).toBe(9);
    expect(terminalFontSizeForWidth(480)).toBe(10);
    expect(terminalFontSizeForWidth(640)).toBe(11);
    expect(terminalFontSizeForWidth(736)).toBe(12);
    expect(terminalFontSizeForWidth(1280)).toBe(14);

    expect(terminalLineHeightForWidth(736)).toBe(1.02);
    expect(terminalLineHeightForWidth(1280)).toBe(1.15);
  });

  it("scales smartbox-7inch terminal metrics across 1024x600 expanded/collapsed sidebar and mobile widths", () => {
    document.documentElement.style.setProperty("--theme-terminal-font-size", "19");
    document.documentElement.style.setProperty("--theme-terminal-line-height", "1.2");

    // 1024x600 with #app-sidebar expanded (240px): ~748px host (>= 580) -> full 19px
    expect(configuredFontSizeForWidth(580, 19)).toBe(19);
    expect(terminalFontSizeForWidth(738)).toBe(19);
    expect(terminalLineHeightForWidth(738)).toBe(1.2);

    // 1024x600 with #app-sidebar collapsed (w-14): ~948px host -> full 19px
    expect(terminalFontSizeForWidth(948)).toBe(19);
    expect(terminalLineHeightForWidth(948)).toBe(1.2);

    // Split / compact widths step down gracefully
    expect(terminalFontSizeForWidth(480)).toBe(18);
    expect(terminalLineHeightForWidth(480)).toBe(1.12);
    expect(terminalFontSizeForWidth(400)).toBe(17);
    expect(terminalFontSizeForWidth(320)).toBe(10);
    expect(terminalFontSizeForWidth(280)).toBe(10);
  });

  it("treats <= 1024px as a narrow chat viewport and defaults unpersisted side-panel state accordingly", () => {
    expect(CHAT_NARROW_MEDIA_QUERY).toBe("(max-width: 1024px)");
    const prevMatchMedia = window.matchMedia;
    const store: Record<string, string> = {};
    const prevStorage = globalThis.localStorage;
    Object.defineProperty(globalThis, "localStorage", {
      configurable: true,
      value: {
        getItem: (k: string) => store[k] ?? null,
        setItem: (k: string, v: string) => {
          store[k] = v;
        },
      },
    });
    try {
      window.matchMedia = ((query: string) =>
        ({
          matches: query === "(max-width: 1024px)",
          media: query,
          addEventListener() {},
          removeEventListener() {},
        }) as unknown as MediaQueryList) as typeof window.matchMedia;
      expect(isNarrowChatViewport()).toBe(true);
      expect(readChatPanelCollapsed()).toBe(true);

      store["hermes-chat-panel-collapsed"] = "0";
      expect(readChatPanelCollapsed()).toBe(false);
    } finally {
      window.matchMedia = prevMatchMedia;
      Object.defineProperty(globalThis, "localStorage", {
        configurable: true,
        value: prevStorage,
      });
    }
  });

  it("includes both JetBrains Mono and SmartBox Kai in TERMINAL_FONT_FAMILY", () => {
    expect(TERMINAL_FONT_FAMILY).toContain("'JetBrains Mono'");
    expect(TERMINAL_FONT_FAMILY).toContain('"SmartBox Kai"');
  });
});
