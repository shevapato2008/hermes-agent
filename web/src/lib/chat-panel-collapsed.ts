/**
 * Collapse state for the desktop chat side panel (model + sessions),
 * persisted in localStorage so the choice survives reloads.
 *
 * Pure helpers so ChatPage stays under its FILE_LINES cap and the
 * persistence stays testable without React.
 */

const CHAT_PANEL_COLLAPSED_KEY = "hermes-chat-panel-collapsed";

export const CHAT_NARROW_MEDIA_QUERY = "(max-width: 1024px)";

export function isNarrowChatViewport(): boolean {
  return (
    typeof window !== "undefined" &&
    typeof window.matchMedia === "function" &&
    window.matchMedia(CHAT_NARROW_MEDIA_QUERY).matches
  );
}

/** The panel's collapsed flag remembered from the previous visit. */
export function readChatPanelCollapsed(): boolean {
  const stored = localStorage.getItem(CHAT_PANEL_COLLAPSED_KEY);
  if (stored === null) return isNarrowChatViewport();
  return stored === "1";
}

/** Flip the collapsed flag and persist the new choice. */
export function toggleChatPanelCollapsed(previous: boolean): boolean {
  const next = !previous;
  localStorage.setItem(CHAT_PANEL_COLLAPSED_KEY, next ? "1" : "0");
  return next;
}
