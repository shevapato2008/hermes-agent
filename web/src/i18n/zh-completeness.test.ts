import { describe, expect, it } from "vitest";
import { en } from "./en";
import { zh } from "./zh";
import { resolvePageTitle } from "@/lib/resolve-page-title";

function collectLeafPaths(obj: unknown, prefix = ""): string[] {
  if (obj === null || typeof obj !== "object" || Array.isArray(obj)) {
    return prefix ? [prefix] : [];
  }
  const paths: string[] = [];
  for (const [key, val] of Object.entries(obj as Record<string, unknown>)) {
    if (val === undefined) continue;
    const next = prefix ? `${prefix}.${key}` : key;
    if (val !== null && typeof val === "object" && !Array.isArray(val)) {
      paths.push(...collectLeafPaths(val, next));
    } else {
      paths.push(next);
    }
  }
  return paths;
}

function getByPath(obj: unknown, path: string): unknown {
  return path.split(".").reduce<unknown>((acc, part) => {
    if (acc !== null && typeof acc === "object") {
      return (acc as Record<string, unknown>)[part];
    }
    return undefined;
  }, obj);
}

describe("Simplified Chinese (zh) completeness & SmartBox terminology", () => {
  it("translates built-in sidebar nav and plugin labels with exact terminology", () => {
    expect(zh.app.nav.files).toBe("文件");
    expect(zh.app.nav.system).toBe("系统");
    expect(zh.app.nav.channels).toBe("频道");
    expect(zh.app.nav.mcp).toBe("MCP");
    expect(zh.app.nav.pairing).toBe("配对");
    expect(zh.app.nav.webhooks).toBe("Webhooks");
    expect(zh.app.nav.profiles).toBe("多智能体配置");
    expect(zh.app.pluginLabels?.kanban).toBe("看板");
    expect(zh.app.pluginLabels?.["hermes-achievements"]).toBe("成就");
  });

  it("replaces all 多Agent配置 occurrences with 多智能体配置", () => {
    expect(JSON.stringify(zh)).not.toContain("多Agent配置");
    expect(zh.profiles.newProfile).toContain("多智能体配置");
    expect(zh.profiles.invalidName).toContain("多智能体配置");
    expect(zh.profiles.allProfiles).toContain("多智能体配置");
    expect(zh.profiles.noProfiles).toContain("多智能体配置");
    expect(zh.profiles.confirmDeleteTitle).toContain("多智能体配置");
    expect(zh.profiles.confirmDeleteMessage).toContain("多智能体配置");
  });

  it("has 100% leaf-key parity with English (en)", () => {
    const enLeaves = collectLeafPaths(en);
    const missing = enLeaves.filter((p) => getByPath(zh, p) === undefined);
    expect(missing).toEqual([]);
  });

  it("translates kanban bulk confirmation strings into Chinese", () => {
    for (const val of [
      zh.kanban.confirmDoneMany,
      zh.kanban.confirmArchiveMany,
      zh.kanban.confirmBlockedMany,
    ]) {
      expect(val).not.toMatch(/^(Mark|Archive) /);
      expect(val).toMatch(/[\u4e00-\u9fff]/);
    }
  });

  it("resolves localized page titles for built-in and plugin routes", () => {
    expect(resolvePageTitle("/files", zh, [])).toBe("文件");
    expect(resolvePageTitle("/system", zh, [])).toBe("系统");
    expect(resolvePageTitle("/channels", zh, [])).toBe("频道");
    expect(resolvePageTitle("/mcp", zh, [])).toBe("MCP");
    expect(resolvePageTitle("/profiles", zh, [])).toBe("多智能体配置");
    expect(
      resolvePageTitle("/kanban", zh, [
        { path: "/kanban", label: "Kanban", name: "kanban" },
      ]),
    ).toBe("看板");
  });
});
