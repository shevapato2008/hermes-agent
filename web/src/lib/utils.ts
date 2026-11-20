import { type ClassValue, clsx } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

/** Mondwest font only — use on layout shells; do not force normal-case here or `text-display` chrome (Segmented, badges) stops uppercasing. */
export const themedFont = "font-mondwest";

/** Mondwest body copy — sentence-case themed text (not uppercase chrome). */
export const themedBody = "font-mondwest normal-case";

/** Mondwest brand chrome — uppercase section headers and nav labels. */
export const themedChrome = "font-mondwest text-display";

function isZhDoc(): boolean {
  return typeof document !== "undefined" && /^zh\b/i.test(document.documentElement.lang);
}

/** Relative time from a Unix epoch timestamp (seconds). */
export function timeAgo(ts: number): string {
  const delta = Date.now() / 1000 - ts;
  const zh = isZhDoc();
  if (delta < 60) return zh ? "刚刚" : "just now";
  if (delta < 3600) return zh ? `${Math.floor(delta / 60)} 分钟前` : `${Math.floor(delta / 60)}m ago`;
  if (delta < 86400) return zh ? `${Math.floor(delta / 3600)} 小时前` : `${Math.floor(delta / 3600)}h ago`;
  if (delta < 172800) return zh ? "昨天" : "yesterday";
  return zh ? `${Math.floor(delta / 86400)} 天前` : `${Math.floor(delta / 86400)}d ago`;
}

/** Relative time from an ISO-8601 timestamp string. */
export function isoTimeAgo(iso: string): string {
  const delta = (Date.now() - new Date(iso).getTime()) / 1000;
  const zh = isZhDoc();
  if (delta < 0 || Number.isNaN(delta)) return zh ? "未知" : "unknown";
  if (delta < 60) return zh ? "刚刚" : "just now";
  if (delta < 3600) return zh ? `${Math.floor(delta / 60)} 分钟前` : `${Math.floor(delta / 60)}m ago`;
  if (delta < 86400) return zh ? `${Math.floor(delta / 3600)} 小时前` : `${Math.floor(delta / 3600)}h ago`;
  return zh ? `${Math.floor(delta / 86400)} 天前` : `${Math.floor(delta / 86400)}d ago`;
}
