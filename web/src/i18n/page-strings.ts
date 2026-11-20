import { useI18n } from "@/i18n";
import { en } from "./en";
import type { Translations } from "./types";

export type PageSectionKey =
  | "skills"
  | "sessions"
  | "sessionsExtra"
  | "filesPage"
  | "channelsPage"
  | "pairingPage"
  | "webhooksPage"
  | "mcpPage"
  | "systemPage"
  | "cron"
  | "models"
  | "profiles"
  | "pluginsPage"
  | "env";

export function pageStrings<K extends PageSectionKey>(
  t: Translations,
  key: K,
): Required<NonNullable<Translations[K]>> {
  return { ...en[key]!, ...(t?.[key] ?? {}) } as unknown as Required<
    NonNullable<Translations[K]>
  >;
}

export function usePageStrings<K extends PageSectionKey>(
  key: K,
): Required<NonNullable<Translations[K]>> {
  const { t } = useI18n();
  return pageStrings(t, key);
}
