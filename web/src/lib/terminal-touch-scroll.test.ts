// @vitest-environment jsdom
import { describe, expect, it, vi } from "vitest";

import { installTerminalTouchScroll } from "./terminal-touch-scroll";

function touchEvent(type: string, y: number, x = 0): TouchEvent {
  const ev = new Event(type, { bubbles: true, cancelable: true }) as TouchEvent;
  const touch = { clientX: x, clientY: y } as Touch;

  Object.defineProperty(ev, "touches", { value: type === "touchend" ? [] : [touch] });
  Object.defineProperty(ev, "changedTouches", { value: [touch] });

  return ev;
}

function term(viewportY: number, baseY: number, textarea?: HTMLTextAreaElement | null) {
  const terminal = {
    buffer: { active: { baseY, viewportY } },
    textarea,
    focus: vi.fn(),
    scrollLines: vi.fn((amount: number) => {
      const active = terminal.buffer.active;
      active.viewportY = Math.max(0, Math.min(active.baseY, active.viewportY + amount));
    }),
  };

  return terminal;
}

describe("installTerminalTouchScroll", () => {
  it("converts one-finger vertical drags into xterm scrollback lines", () => {
    const host = new EventTarget() as HTMLElement;
    const terminal = term(20, 100);
    const cleanup = installTerminalTouchScroll(host, terminal);

    host.dispatchEvent(touchEvent("touchstart", 200));
    const move = touchEvent("touchmove", 164);
    host.dispatchEvent(move);

    expect(terminal.scrollLines).toHaveBeenCalledWith(2);
    expect(move.defaultPrevented).toBe(true);

    cleanup();
  });

  it("lets the outer page handle touch drags at the terminal boundary", () => {
    const host = new EventTarget() as HTMLElement;
    const terminal = term(0, 100);
    const cleanup = installTerminalTouchScroll(host, terminal);

    host.dispatchEvent(touchEvent("touchstart", 100));
    const move = touchEvent("touchmove", 136);
    host.dispatchEvent(move);

    expect(terminal.scrollLines).not.toHaveBeenCalled();
    expect(move.defaultPrevented).toBe(false);

    cleanup();
  });

  it("removes listeners on cleanup", () => {
    const host = new EventTarget() as HTMLElement;
    const terminal = term(20, 100);
    const cleanup = installTerminalTouchScroll(host, terminal);

    cleanup();

    host.dispatchEvent(touchEvent("touchstart", 200));
    host.dispatchEvent(touchEvent("touchmove", 164));

    expect(terminal.scrollLines).not.toHaveBeenCalled();
  });

  it("focuses the terminal on a slight-wiggle capacitive tap when textarea is not focused", () => {
    const host = document.createElement("div");
    const textarea = document.createElement("textarea");
    document.body.appendChild(textarea);
    const terminal = term(20, 100, textarea);
    const cleanup = installTerminalTouchScroll(host, terminal);

    host.dispatchEvent(touchEvent("touchstart", 100, 100));
    host.dispatchEvent(touchEvent("touchmove", 103, 104));
    host.dispatchEvent(touchEvent("touchend", 103, 104));

    expect(terminal.scrollLines).not.toHaveBeenCalled();
    expect(terminal.focus).toHaveBeenCalledTimes(1);

    // When textarea is already focused, tap does not redundantly re-focus.
    textarea.focus();
    host.dispatchEvent(touchEvent("touchstart", 100, 100));
    host.dispatchEvent(touchEvent("touchend", 102, 101));
    expect(terminal.focus).toHaveBeenCalledTimes(1);

    cleanup();
    textarea.remove();
  });

  it("does not focus the terminal when a touch gesture scrolls or moves >= 10px", () => {
    const host = document.createElement("div");
    const textarea = document.createElement("textarea");
    document.body.appendChild(textarea);
    const terminal = term(20, 100, textarea);
    const cleanup = installTerminalTouchScroll(host, terminal);

    // 1. Gesture that scrolls the terminal (even if finger returns near start).
    host.dispatchEvent(touchEvent("touchstart", 200, 100));
    host.dispatchEvent(touchEvent("touchmove", 164, 100));
    host.dispatchEvent(touchEvent("touchend", 200, 100));
    expect(terminal.scrollLines).toHaveBeenCalled();
    expect(terminal.focus).not.toHaveBeenCalled();

    // 2. Gesture that moves >= 10px without scrolling (e.g. horizontal drag or at boundary).
    const boundaryTerminal = term(0, 100, textarea);
    const cleanupBoundary = installTerminalTouchScroll(host, boundaryTerminal);
    host.dispatchEvent(touchEvent("touchstart", 100, 100));
    host.dispatchEvent(touchEvent("touchend", 100, 112));
    expect(boundaryTerminal.focus).not.toHaveBeenCalled();

    cleanupBoundary();
    cleanup();
    textarea.remove();
  });
});
