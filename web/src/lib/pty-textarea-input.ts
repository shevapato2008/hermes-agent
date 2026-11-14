import { shouldTreatInputAsMobileReplacement } from "@/lib/pty-mobile-input";

export interface PtyTextareaInputOptions {
  textarea: HTMLTextAreaElement;
  compositionForwarder: {
    onCompositionEnd: (data: string | null) => void;
    sendDirect: (data: string) => void;
  };
  onMobileReplacementInput: () => void;
  isMobileLike?: boolean;
  screenReaderMode?: boolean;
}

interface SmartKeyboardEvent extends Event {
  smartKeyboardSource?: boolean;
  smartKeyboardText?: unknown;
}

function detectMobileLike(): boolean {
  return (
    typeof navigator !== "undefined" &&
    /Android|iPhone|iPad|iPod|Mobile/i.test(navigator.userAgent)
  );
}

function isPrintablePhysicalKey(ev: KeyboardEvent): boolean {
  return (
    !ev.isComposing &&
    ev.keyCode !== 229 &&
    ev.key !== "Process" &&
    ev.key.length === 1 &&
    !ev.ctrlKey &&
    !ev.altKey &&
    !ev.metaKey
  );
}

function extractSmartKeyboardText(ev: Event): string | null {
  const sk = ev as SmartKeyboardEvent;
  if (sk.smartKeyboardSource !== true) return null;
  if (typeof sk.smartKeyboardText !== "string" || sk.smartKeyboardText.length === 0) {
    return null;
  }
  return sk.smartKeyboardText;
}

function extractVirtualInsertText(ev: Event): string | null {
  const input = ev as InputEvent;
  if (input.isComposing || input.inputType !== "insertText") return null;
  if (typeof input.data !== "string" || input.data.length === 0) return null;
  return input.data;
}

/**
 * Bridges virtual / on-screen keyboard text commits on xterm's helper textarea
 * while keeping `screenReaderMode: true` intact for assistive technologies.
 *
 * In `@xterm/xterm`, `_inputEvent` drops `inputType === "insertText"` whenever
 * `options.screenReaderMode` is `true`, assuming physical `keydown` or native
 * IME `compositionend` will carry every character. Kiosk / mobile virtual
 * keyboards (such as RK3562 `SmartKeyboard` + `hermes-terminal-input.js`) emit
 * `insertText` `InputEvent`s without physical `keydown` or `compositionend`,
 * so we forward those unguarded commits synchronously to the PTY.
 */
export function installPtyTextareaInput(options: PtyTextareaInputOptions): () => void {
  const { textarea, compositionForwarder, onMobileReplacementInput } = options;
  textarea.setAttribute("autocomplete", "off");
  textarea.setAttribute("autocorrect", "off");
  textarea.setAttribute("autocapitalize", "off");
  textarea.setAttribute("spellcheck", "false");

  const mobileLike = options.isMobileLike ?? detectMobileLike();
  const srMode = options.screenReaderMode ?? true;

  let keyDownSeen = false;
  let keyDownTimer: ReturnType<typeof setTimeout> | null = null;
  let composing = false;
  let compositionSeq = 0;

  const clearKeyDownTimer = () => {
    if (keyDownTimer !== null) {
      clearTimeout(keyDownTimer);
      keyDownTimer = null;
    }
  };

  const onKeyDown = (ev: KeyboardEvent) => {
    if (!isPrintablePhysicalKey(ev)) return;
    clearKeyDownTimer();
    keyDownSeen = true;
    keyDownTimer = setTimeout(() => {
      keyDownTimer = null;
      keyDownSeen = false;
    }, 0);
  };

  const onKeyUp = () => {
    clearKeyDownTimer();
    keyDownSeen = false;
  };

  const onCompositionStart = () => {
    compositionSeq += 1;
    composing = true;
  };

  const onCompositionEnd = (ev: CompositionEvent) => {
    composing = true;
    onMobileReplacementInput();
    compositionForwarder.onCompositionEnd(ev.data);
    const seq = compositionSeq;
    queueMicrotask(() => {
      if (compositionSeq === seq) {
        composing = false;
      }
    });
  };

  const onBeforeInput = (ev: Event) => {
    const input = ev as InputEvent;
    if (shouldTreatInputAsMobileReplacement(input.inputType, input.data, mobileLike)) {
      onMobileReplacementInput();
    }
  };

  const onInput = (ev: Event) => {
    const smartText = extractSmartKeyboardText(ev);
    if (smartText !== null) {
      textarea.value = "";
      compositionForwarder.sendDirect(smartText);
      return;
    }
    if (!srMode || composing || keyDownSeen) return;
    const inserted = extractVirtualInsertText(ev);
    if (inserted === null) return;
    textarea.value = "";
    compositionForwarder.sendDirect(inserted);
  };

  textarea.addEventListener("keydown", onKeyDown, true);
  textarea.addEventListener("keyup", onKeyUp, true);
  textarea.addEventListener("compositionstart", onCompositionStart, true);
  textarea.addEventListener("beforeinput", onBeforeInput, true);
  textarea.addEventListener("compositionend", onCompositionEnd, true);
  textarea.addEventListener("input", onInput, true);

  return () => {
    clearKeyDownTimer();
    textarea.removeEventListener("keydown", onKeyDown, true);
    textarea.removeEventListener("keyup", onKeyUp, true);
    textarea.removeEventListener("compositionstart", onCompositionStart, true);
    textarea.removeEventListener("beforeinput", onBeforeInput, true);
    textarea.removeEventListener("compositionend", onCompositionEnd, true);
    textarea.removeEventListener("input", onInput, true);
  };
}
