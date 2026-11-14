// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";

import {
  terminalFontSizeForWidth,
  terminalLineHeightForWidth,
} from "./terminal-appearance";
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
    document.documentElement.style.setProperty("--theme-terminal-font-size", "16");
    document.documentElement.style.setProperty("--theme-terminal-line-height", "1.18");

    // 1024x600 with #app-sidebar expanded (15rem = 270px): ~738px host -> 15px (>=80 cols)
    expect(terminalFontSizeForWidth(738)).toBe(15);
    expect(terminalLineHeightForWidth(738)).toBe(1.18);

    // 1024x600 with #app-sidebar collapsed (w-14): ~948px host -> full 16px
    expect(terminalFontSizeForWidth(948)).toBe(16);
    expect(terminalLineHeightForWidth(948)).toBe(1.18);

    // Split / compact widths step down gracefully
    expect(terminalFontSizeForWidth(480)).toBe(14);
    expect(terminalLineHeightForWidth(480)).toBe(1.1);
    expect(terminalFontSizeForWidth(400)).toBe(12);
    expect(terminalFontSizeForWidth(320)).toBe(10);
    expect(terminalFontSizeForWidth(280)).toBe(9);
  });

  it("includes both JetBrains Mono and SmartBox Kai in TERMINAL_FONT_FAMILY", () => {
    expect(TERMINAL_FONT_FAMILY).toContain("'JetBrains Mono'");
    expect(TERMINAL_FONT_FAMILY).toContain('"SmartBox Kai"');
  });
});
