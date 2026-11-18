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

  it("registers smartbox-7inch first in BUILTIN_THEMES with 7-inch Option A (20px) touch metrics and additional presets", () => {
    expect(Object.keys(BUILTIN_THEMES).slice(0, 4)).toEqual([
      "smartbox-7inch",
      "smartbox-7inch-18",
      "smartbox-7inch-b",
      "smartbox-7inch-c",
    ]);
    expect(BUILTIN_THEMES["smartbox-7inch"]).toBe(smartbox7InchTheme);
    expect(smartbox7InchTheme.label).toBe("智星盒 7″ · 方案A 大字 (20px)");
    expect(smartbox7InchTheme.typography.baseSize).toBe("20px");
    expect(smartbox7InchTheme.typography.lineHeight).toBe("1.55");
    expect(smartbox7InchTheme.terminalFontSize).toBe(15);
    expect(smartbox7InchTheme.terminalLineHeight).toBe(1.2);
    expect(smartbox7InchTheme.terminalBackground).toBe("#021010");
    expect(smartbox7InchTheme.terminalForeground).toBe("#fff2df");

    const preset18 = BUILTIN_THEMES["smartbox-7inch-18"];
    expect(preset18.label).toBe("智星盒 7″ · 标准字号 (18px)");
    expect(preset18.typography.baseSize).toBe("18px");
    expect(preset18.typography.lineHeight).toBe("1.58");
    expect(preset18.terminalFontSize).toBe(14);
    expect(preset18.terminalLineHeight).toBe(1.18);
    expect(preset18.customCSS).toContain("--text-xs: max(0.75rem, 14px)");

    const presetB = BUILTIN_THEMES["smartbox-7inch-b"];
    expect(presetB.label).toBe("智星盒 7″ · 方案B 特大 (22px)");
    expect(presetB.typography.baseSize).toBe("22px");
    expect(presetB.typography.lineHeight).toBe("1.52");
    expect(presetB.terminalFontSize).toBe(16.5);
    expect(presetB.terminalLineHeight).toBe(1.2);
    expect(presetB.customCSS).toContain("--text-sm: 21px");
    expect(presetB.customCSS).toContain("--text-xs: max(0.75rem, 17.5px)");
    expect(presetB.customCSS).toContain("font-size: 20px");

    const presetC = BUILTIN_THEMES["smartbox-7inch-c"];
    expect(presetC.label).toBe("智星盒 7″ · 方案C 适老 (24px)");
    expect(presetC.typography.baseSize).toBe("24px");
    expect(presetC.typography.lineHeight).toBe("1.48");
    expect(presetC.terminalFontSize).toBe(18);
    expect(presetC.terminalLineHeight).toBe(1.22);
    expect(presetC.customCSS).toContain("--text-sm: 23px");
    expect(presetC.customCSS).toContain("--text-xs: max(0.75rem, 19px)");
    expect(presetC.customCSS).toContain("font-size: 22px");
  });

  it("scopes customCSS to #app-sidebar and :root portal-safe tokens", () => {
    const css = smartbox7InchTheme.customCSS ?? "";
    expect(css).toContain("@media (min-width: 1024px)");
    expect(css).toContain("#app-sidebar:not(.lg\\:w-14)");
    expect(css).toContain('header[role="banner"] > div');
    expect(css).toContain("padding-right: 6.75rem !important");
    expect(css).toContain("#app-sidebar > nav a");
    expect(css).toContain("min-height: 42px");
    expect(css).toContain("font-size: 18.5px");
    expect(css).toContain("--text-secondary:");
    expect(css).toContain("--text-tertiary:");
    expect(css).not.toContain("--color-text-secondary");
    expect(css).not.toContain("--color-text-tertiary");
    expect(css).toContain("--text-sm: 19px");
    expect(css).toContain("--text-xs: max(0.75rem, 16px)");
    expect(css).toContain("font-size: 15px !important");
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
        "15",
      );
      expect(document.documentElement.style.getPropertyValue("--theme-terminal-line-height")).toBe(
        "1.2",
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
