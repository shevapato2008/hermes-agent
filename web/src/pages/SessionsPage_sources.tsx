import {
  Clock,
  Database,
  Globe,
  Hash,
  MessageCircle,
  MessageSquare,
  Play,
  Terminal,
} from "lucide-react";

export const SOURCE_CONFIG: Record<string, { icon: typeof Terminal; color: string }> =
  {
    cli: { icon: Terminal, color: "text-primary" },
    tui: { icon: Terminal, color: "text-primary" },
    telegram: { icon: MessageCircle, color: "text-[oklch(0.65_0.15_250)]" },
    discord: { icon: Hash, color: "text-[oklch(0.65_0.15_280)]" },
    slack: { icon: MessageSquare, color: "text-[oklch(0.7_0.15_155)]" },
    whatsapp: { icon: Globe, color: "text-success" },
    whatsapp_cloud: { icon: Globe, color: "text-success" },
    signal: { icon: MessageCircle, color: "text-success" },
    matrix: { icon: MessageCircle, color: "text-[oklch(0.65_0.15_250)]" },
    email: { icon: MessageSquare, color: "text-[oklch(0.7_0.15_155)]" },
    sms: { icon: MessageCircle, color: "text-success" },
    cron: { icon: Clock, color: "text-warning" },
    tool: { icon: Play, color: "text-warning" },
    oneshot: { icon: Terminal, color: "text-warning" },
    api_server: { icon: Globe, color: "text-muted-foreground" },
    acp: { icon: Database, color: "text-muted-foreground" },
    hermes_flow: { icon: Play, color: "text-warning" },
    vulcan_delegate: { icon: Play, color: "text-warning" },
    webhook: { icon: Globe, color: "text-warning" },
  };

export const AUTOMATION_SESSION_SOURCES = [
  "cron",
  "tool",
  "oneshot",
  "api_server",
  "acp",
  "hermes_flow",
  "vulcan_delegate",
  "webhook",
];
const AUTOMATION_SESSION_SOURCE_SET = new Set(AUTOMATION_SESSION_SOURCES);
export const NO_MATCHING_SESSION_SOURCE = "__hermes_dashboard_no_matching_source__";

export type SessionFilterCategory = "chats" | "automation" | "all";
export type SourceSelectionsByCategory = Record<SessionFilterCategory, string[] | null>;

export function isAutomationSource(source: string): boolean {
  return AUTOMATION_SESSION_SOURCE_SET.has(source);
}

export function sourceBelongsToCategory(
  source: string,
  category: SessionFilterCategory,
): boolean {
  if (category === "all") return true;
  if (category === "automation") return isAutomationSource(source);
  return !isAutomationSource(source);
}

export function buildSessionQueryOptions(
  selectedSources: string[] | null,
  sessionCategory: SessionFilterCategory,
  allSourceNames: string[],
): { source?: string; sources?: string[]; excludeSources?: string[] } {
  if (selectedSources !== null) {
    if (selectedSources.length === 0) {
      return allSourceNames.length > 0
        ? { excludeSources: allSourceNames }
        : { source: NO_MATCHING_SESSION_SOURCE };
    }
    if (selectedSources.length === 1) {
      return { source: selectedSources[0] };
    }
    const selected = new Set(selectedSources);
    const excludedSources = allSourceNames.filter((s) => !selected.has(s));
    return excludedSources.length > 0 ? { excludeSources: excludedSources } : {};
  }
  if (sessionCategory === "chats") {
    return { excludeSources: AUTOMATION_SESSION_SOURCES };
  }
  if (sessionCategory === "automation") {
    const excludedSources = allSourceNames.filter((s) => !isAutomationSource(s));
    return excludedSources.length > 0
      ? { excludeSources: excludedSources }
      : { sources: AUTOMATION_SESSION_SOURCES };
  }
  return {};
}

import type { Translations } from "@/i18n/types";
import { pageStrings } from "@/i18n/page-strings";
import { isZhDocument } from "@/i18n/catalog-zh";

const SOURCE_LABELS_EN: Record<string, string> = {
  api_server: "API server",
  acp: "ACP",
  cli: "CLI",
  tui: "TUI",
  telegram: "Telegram",
  discord: "Discord",
  slack: "Slack",
  whatsapp: "WhatsApp",
  whatsapp_cloud: "WhatsApp Cloud",
  sms: "SMS",
  cron: "Cron",
  tool: "Tool",
  hermes_flow: "Hermes Flow",
  vulcan_delegate: "Vulcan delegate",
  webhook: "Webhook",
  weixin: "WeChat",
  wecom: "WeCom",
  wecom_callback: "WeCom App",
  feishu: "Feishu",
  dingtalk: "DingTalk",
  qqbot: "QQ Bot",
  smartbox_voice: "SmartBox Voice",
};

const SOURCE_LABELS_ZH: Record<string, string> = {
  api_server: "API 服务",
  acp: "ACP",
  cli: "CLI 命令行",
  tui: "终端对话 (TUI)",
  telegram: "Telegram",
  discord: "Discord",
  slack: "Slack",
  whatsapp: "WhatsApp",
  whatsapp_cloud: "WhatsApp Cloud",
  sms: "短信",
  cron: "定时任务",
  tool: "工具调用",
  oneshot: "单次执行",
  hermes_flow: "Hermes 工作流",
  vulcan_delegate: "子智能体委派",
  webhook: "Webhook",
  weixin: "微信",
  wecom: "企业微信",
  wecom_callback: "企业微信应用",
  feishu: "飞书",
  dingtalk: "钉钉",
  qqbot: "QQ 机器人",
  smartbox_voice: "语音助手",
};

export function sourceLabel(source: string): string {
  const table = isZhDocument() ? SOURCE_LABELS_ZH : SOURCE_LABELS_EN;
  if (table[source]) return table[source];
  if (SOURCE_LABELS_EN[source]) return SOURCE_LABELS_EN[source];
  return source
    .split("_")
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

export function getSourceMenuTitle(
  sessionCategory: SessionFilterCategory,
  t: Translations,
): string {
  const sessionsStrings = pageStrings(t, "sessions");
  if (sessionCategory === "chats") return sessionsStrings.chatSources;
  if (sessionCategory === "automation") return sessionsStrings.automationSources;
  return sessionsStrings.sourceFilter;
}

export function getSourceFilterLabel(
  sessionCategory: SessionFilterCategory,
  selectedSources: string[] | null,
  t: Translations,
): string {
  const sessionsStrings = pageStrings(t, "sessions");
  if (selectedSources === null) {
    if (sessionCategory === "chats") return sessionsStrings.anyChatSource;
    if (sessionCategory === "automation") return sessionsStrings.anyAutomationSource;
    return sessionsStrings.anySource;
  }
  if (selectedSources.length === 0) {
    return sessionsStrings.noSources;
  }
  if (selectedSources.length === 1) {
    return sourceLabel(selectedSources[0]);
  }
  return sessionsStrings.sourcesCount.replace(
    "{count}",
    String(selectedSources.length),
  );
}
