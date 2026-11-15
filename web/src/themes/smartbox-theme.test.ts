// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";

import { applyTheme } from "./context";
import { FONT_CHOICES, KAITI_CJK_STACK, SMARTBOX_KAITI_UI_STACK } from "./fonts";
import { BUILTIN_THEMES, smartbox7InchTheme } from "./presets";

describe("SmartBox 7-inch default theme and KaiTi font stacks", () => {
  afterEach(() => {
    document.documentElement.removeAttribute("style");
    document.getElementById("hermes-theme-custom-css")?.remove();
  });

  it("registers smartbox-7inch first in BUILTIN_THEMES with 7-inch touch metrics", () => {
    expect(Object.keys(BUILTIN_THEMES)[0]).toBe("smartbox-7inch");
    expect(BUILTIN_THEMES["smartbox-7inch"]).toBe(smartbox7InchTheme);
    expect(smartbox7InchTheme.typography.baseSize).toBe("18px");
    expect(smartbox7InchTheme.typography.lineHeight).toBe("1.58");
    expect(smartbox7InchTheme.terminalFontSize).toBe(16);
    expect(smartbox7InchTheme.terminalLineHeight).toBe(1.18);
    expect(smartbox7InchTheme.terminalBackground).toBe("#021010");
    expect(smartbox7InchTheme.terminalForeground).toBe("#fff2df");
  });

  it("scopes customCSS to #app-sidebar and :root portal-safe tokens", () => {
    const css = smartbox7InchTheme.customCSS ?? "";
    expect(css).toContain("@media (min-width: 1024px)");
    expect(css).toContain("#app-sidebar:not(.lg\\:w-14)");
    expect(css).toContain('header[role="banner"] > div');
    expect(css).toContain("padding-right: 6.75rem !important");
    expect(css).toContain("#app-sidebar > nav a");
    expect(css).toContain("min-height: 42px");
    expect(css).toContain("--text-secondary:");
    expect(css).toContain("--text-tertiary:");
    expect(css).not.toContain("--color-text-secondary");
    expect(css).not.toContain("--color-text-tertiary");
    expect(css).toContain("--text-xs: max(0.75rem, 14px)");
  });

  it("places SmartBox Sans ahead of SmartBox Kai and includes KaiTi in every built-in theme", () => {
    expect(KAITI_CJK_STACK).toContain('"SmartBox Kai"');
    const sansIdx = SMARTBOX_KAITI_UI_STACK.indexOf('"SmartBox Sans"');
    const kaiIdx = SMARTBOX_KAITI_UI_STACK.indexOf('"SmartBox Kai"');
    expect(sansIdx).toBeGreaterThanOrEqual(0);
    expect(kaiIdx).toBeGreaterThan(sansIdx);

    for (const [name, theme] of Object.entries(BUILTIN_THEMES)) {
      expect(theme.typography.fontSans, `${name}.fontSans`).toContain('"SmartBox Kai"');
      expect(theme.typography.fontMono, `${name}.fontMono`).toContain('"SmartBox Kai"');
    }

    const kaitiChoice = FONT_CHOICES.find((choice) => choice.id === "kaiti");
    expect(kaitiChoice).toBeDefined();
    expect(kaitiChoice?.stack).toBe(SMARTBOX_KAITI_UI_STACK);
  });

  it("writes and clears terminal metric CSS variables on applyTheme and notifies resize listeners", () => {
    const onResize = vi.fn();
    window.addEventListener("resize", onResize);
    try {
      applyTheme(smartbox7InchTheme);
      expect(document.documentElement.style.getPropertyValue("--theme-terminal-font-size")).toBe(
        "16",
      );
      expect(document.documentElement.style.getPropertyValue("--theme-terminal-line-height")).toBe(
        "1.18",
      );
      expect(onResize).toHaveBeenCalledTimes(1);

      applyTheme(BUILTIN_THEMES.default);
      expect(document.documentElement.style.getPropertyValue("--theme-terminal-font-size")).toBe(
        "",
      );
      expect(document.documentElement.style.getPropertyValue("--theme-terminal-line-height")).toBe(
        "",
      );
      expect(onResize).toHaveBeenCalledTimes(2);
    } finally {
      window.removeEventListener("resize", onResize);
    }
  });
});
