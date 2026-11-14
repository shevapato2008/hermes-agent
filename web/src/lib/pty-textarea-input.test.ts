// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";

import { createPtyCompositionForwarder } from "./pty-composition";
import { installPtyTextareaInput } from "./pty-textarea-input";

function makeTextarea(): HTMLTextAreaElement {
  const textarea = document.createElement("textarea");
  document.body.appendChild(textarea);
  return textarea;
}

describe("installPtyTextareaInput", () => {
  afterEach(() => {
    vi.useRealTimers();
    document.body.innerHTML = "";
  });

  it("forwards SmartBox hermes-terminal-input.js English insertText sequence including double 'l' and clears textarea", () => {
    const textarea = makeTextarea();
    const sent: string[] = [];
    const compositionForwarder = createPtyCompositionForwarder((d) => sent.push(d));
    const onMobileReplacementInput = vi.fn();

    const cleanup = installPtyTextareaInput({
      textarea,
      compositionForwarder,
      onMobileReplacementInput,
      screenReaderMode: true,
    });

    expect(textarea.getAttribute("autocomplete")).toBe("off");
    expect(textarea.getAttribute("autocorrect")).toBe("off");
    expect(textarea.getAttribute("autocapitalize")).toBe("off");
    expect(textarea.getAttribute("spellcheck")).toBe("false");

    for (const ch of ["h", "e", "l", "l", "o"]) {
      textarea.value = ch;
      textarea.dispatchEvent(
        new InputEvent("input", {
          data: ch,
          inputType: "insertText",
          bubbles: true,
          cancelable: true,
        }),
      );
      expect(textarea.value).toBe("");
    }

    expect(sent).toEqual(["h", "e", "l", "l", "o"]);
    cleanup();
  });

  it("forwards SmartBox Chinese Pinyin/Rime candidate commit synchronously", () => {
    const textarea = makeTextarea();
    const sent: string[] = [];
    const compositionForwarder = createPtyCompositionForwarder((d) => sent.push(d));

    const cleanup = installPtyTextareaInput({
      textarea,
      compositionForwarder,
      onMobileReplacementInput: vi.fn(),
      screenReaderMode: true,
    });

    textarea.value = "你好世界";
    textarea.dispatchEvent(
      new InputEvent("input", {
        data: "你好世界",
        inputType: "insertText",
        bubbles: true,
        cancelable: true,
      }),
    );

    expect(sent).toEqual(["你好世界"]);
    expect(textarea.value).toBe("");
    cleanup();
  });

  it("delivers Pinyin insertText strictly before an immediate synchronous Enter ('\\r')", () => {
    const textarea = makeTextarea();
    const ptyWire: string[] = [];
    const compositionForwarder = createPtyCompositionForwarder((d) => ptyWire.push(d));

    const cleanup = installPtyTextareaInput({
      textarea,
      compositionForwarder,
      onMobileReplacementInput: vi.fn(),
      screenReaderMode: true,
    });

    textarea.dispatchEvent(
      new InputEvent("input", {
        data: "你好",
        inputType: "insertText",
        bubbles: true,
        cancelable: true,
      }),
    );
    // SmartKeyboard immediately sends Enter on the very next synchronous line.
    compositionForwarder.noteTerminalData("\r");
    const unechoed = compositionForwarder.filterTerminalData("\r");
    if (unechoed) ptyWire.push(unechoed);

    expect(ptyWire).toEqual(["你好", "\r"]);
    cleanup();
  });

  it("handles direct SmartKeyboard plain Event('input') fallback with smartKeyboardSource", () => {
    const textarea = makeTextarea();
    const sent: string[] = [];
    const compositionForwarder = createPtyCompositionForwarder((d) => sent.push(d));

    const cleanup = installPtyTextareaInput({
      textarea,
      compositionForwarder,
      onMobileReplacementInput: vi.fn(),
      screenReaderMode: true,
    });

    textarea.value = "楷体";
    const ev = Object.assign(new Event("input", { bubbles: true }), {
      smartKeyboardSource: true,
      smartKeyboardText: "楷体",
    });
    textarea.dispatchEvent(ev);

    expect(sent).toEqual(["楷体"]);
    expect(textarea.value).toBe("");
    cleanup();
  });

  it("does not duplicate physical keyboard printable keystrokes", () => {
    const textarea = makeTextarea();
    const send = vi.fn();
    const compositionForwarder = createPtyCompositionForwarder(send);

    const cleanup = installPtyTextareaInput({
      textarea,
      compositionForwarder,
      onMobileReplacementInput: vi.fn(),
      screenReaderMode: true,
    });

    textarea.dispatchEvent(new KeyboardEvent("keydown", { key: "a", bubbles: true }));
    compositionForwarder.noteTerminalData("a");
    textarea.dispatchEvent(
      new InputEvent("input", {
        data: "a",
        inputType: "insertText",
        composed: true,
        bubbles: true,
      }),
    );
    textarea.dispatchEvent(new KeyboardEvent("keyup", { key: "a", bubbles: true }));

    expect(send).not.toHaveBeenCalled();

    // After keyup resets keyDownSeen, a subsequent virtual keyboard insertText still works.
    textarea.dispatchEvent(
      new InputEvent("input", {
        data: "b",
        inputType: "insertText",
        bubbles: true,
        cancelable: true,
      }),
    );
    expect(send).toHaveBeenCalledExactlyOnceWith("b");
    cleanup();
  });

  it("forwards native OS IME composition once without duplicating same-turn trailing insertText", async () => {
    vi.useFakeTimers();
    const textarea = makeTextarea();
    const send = vi.fn();
    const onMobileReplacementInput = vi.fn();
    const compositionForwarder = createPtyCompositionForwarder(send);

    const cleanup = installPtyTextareaInput({
      textarea,
      compositionForwarder,
      onMobileReplacementInput,
      screenReaderMode: true,
    });

    textarea.dispatchEvent(new CompositionEvent("compositionstart", { bubbles: true }));
    textarea.dispatchEvent(
      new InputEvent("input", {
        data: "ni",
        inputType: "insertCompositionText",
        isComposing: true,
        bubbles: true,
      }),
    );
    textarea.dispatchEvent(
      new CompositionEvent("compositionend", { data: "你", bubbles: true }),
    );
    textarea.value = "你";
    // Blink/V8 drains microtasks before dispatching a trailing native insertText InputEvent.
    await Promise.resolve();
    textarea.dispatchEvent(
      new InputEvent("input", {
        data: "你",
        inputType: "insertText",
        bubbles: true,
      }),
    );

    // textarea.value must remain intact for xterm CompositionHelper's 0ms finalize timer.
    expect(textarea.value).toBe("你");
    vi.runAllTimers();

    expect(send).toHaveBeenCalledExactlyOnceWith("你");
    expect(onMobileReplacementInput).toHaveBeenCalled();
    cleanup();
  });

  it("leaves non-composing keyCode 229 / Process keystrokes to xterm without wiping leftover textarea.value", () => {
    const textarea = makeTextarea();
    textarea.value = "你好";
    const send = vi.fn();
    const compositionForwarder = createPtyCompositionForwarder(send);

    const cleanup = installPtyTextareaInput({
      textarea,
      compositionForwarder,
      onMobileReplacementInput: vi.fn(),
      screenReaderMode: true,
    });

    textarea.dispatchEvent(
      new KeyboardEvent("keydown", {
        key: "Process",
        keyCode: 229,
        bubbles: true,
      }),
    );
    textarea.value = "你好，";
    textarea.dispatchEvent(
      new InputEvent("input", {
        data: "，",
        inputType: "insertText",
        bubbles: true,
      }),
    );

    expect(textarea.value).toBe("你好，");
    expect(send).not.toHaveBeenCalled();
    cleanup();
  });

  it("ignores paste InputEvents so xterm/paste handlers remain authoritative", () => {
    const textarea = makeTextarea();
    const send = vi.fn();
    const compositionForwarder = createPtyCompositionForwarder(send);

    const cleanup = installPtyTextareaInput({
      textarea,
      compositionForwarder,
      onMobileReplacementInput: vi.fn(),
      screenReaderMode: true,
    });

    textarea.dispatchEvent(
      new InputEvent("input", {
        data: "pasted",
        inputType: "insertFromPaste",
        bubbles: true,
      }),
    );

    expect(send).not.toHaveBeenCalled();
    cleanup();
  });

  it("does not intercept standard insertText when screenReaderMode is false, while still honoring smartKeyboardSource", () => {
    const textarea = makeTextarea();
    const send = vi.fn();
    const compositionForwarder = createPtyCompositionForwarder(send);

    const cleanup = installPtyTextareaInput({
      textarea,
      compositionForwarder,
      onMobileReplacementInput: vi.fn(),
      screenReaderMode: false,
    });

    textarea.dispatchEvent(
      new InputEvent("input", {
        data: "x",
        inputType: "insertText",
        bubbles: true,
      }),
    );
    expect(send).not.toHaveBeenCalled();

    const smartEv = Object.assign(new Event("input", { bubbles: true }), {
      smartKeyboardSource: true,
      smartKeyboardText: "y",
    });
    textarea.dispatchEvent(smartEv);
    expect(send).toHaveBeenCalledExactlyOnceWith("y");

    cleanup();
  });
});
