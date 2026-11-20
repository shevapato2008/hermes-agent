interface ScrollableTerminal {
  buffer: {
    active: {
      baseY: number;
      viewportY: number;
    };
  };
  scrollLines: (amount: number) => void;
  textarea?: HTMLTextAreaElement | null;
  focus?: () => void;
}

interface TouchGestureState {
  startX: number | null;
  startY: number | null;
  lastY: number | null;
  residualPx: number;
  didScroll: boolean;
  movedTooFar: boolean;
  multiTouch: boolean;
}

const TOUCH_SCROLL_PX_PER_LINE = 18;
const TOUCH_SCROLL_MIN_PX = 2;
const TOUCH_TAP_MAX_MOVE_PX = 10;
const CAPTURE = { capture: true } as const;
const PASSIVE_CAPTURE = { capture: true, passive: true } as const;
const ACTIVE_CAPTURE = { capture: true, passive: false } as const;

function activeElement(): Element | null {
  return typeof document === "undefined" ? null : document.activeElement;
}

function canScrollTerminal(term: ScrollableTerminal, amount: number): boolean {
  const { baseY, viewportY } = term.buffer.active;

  if (amount < 0) {
    return viewportY > 0;
  }

  if (amount > 0) {
    return viewportY < baseY;
  }

  return false;
}

function exceedsTapDistance(state: TouchGestureState, x: number, y: number): boolean {
  if (state.startX === null || state.startY === null) {
    return true;
  }
  return Math.hypot(x - state.startX, y - state.startY) >= TOUCH_TAP_MAX_MOVE_PX;
}

function maybeFocusTerminalOnTap(
  term: ScrollableTerminal,
  state: TouchGestureState,
  ev: TouchEvent,
): void {
  if (state.multiTouch || state.didScroll || state.movedTooFar) {
    return;
  }
  const endTouch = ev.changedTouches[0];
  if (!endTouch || exceedsTapDistance(state, endTouch.clientX, endTouch.clientY)) {
    return;
  }
  if (ev.cancelable) {
    ev.preventDefault();
  }
  if (typeof term.focus === "function" && (!term.textarea || activeElement() !== term.textarea)) {
    term.focus();
  }
}

/**
 * Add touch-drag scrollback support for dashboard xterm panes.
 *
 * xterm.js handles wheel/trackpad scrollback for the embedded TUI, but iPadOS
 * WebKit does not translate a finger drag inside the terminal into wheel events
 * or native scrollbar dragging. Mirror the wheel path by converting vertical
 * touch movement into `scrollLines()` while there is xterm scrollback available;
 * at the top/bottom boundary we leave the event alone so the outer dashboard
 * page can still move above the software keyboard.
 *
 * Also focuses `term` on a clean single-finger tap (`< 10px` movement without
 * scrolling) so slight finger wiggle on capacitive touchscreens still focuses
 * `term.textarea` and opens the on-screen keyboard.
 */
export function installTerminalTouchScroll(host: HTMLElement, term: ScrollableTerminal): () => void {
  const state: TouchGestureState = {
    startX: null,
    startY: null,
    lastY: null,
    residualPx: 0,
    didScroll: false,
    movedTooFar: false,
    multiTouch: false,
  };

  const reset = () => {
    state.startX = null;
    state.startY = null;
    state.lastY = null;
    state.residualPx = 0;
    state.didScroll = false;
    state.movedTooFar = false;
    state.multiTouch = false;
  };

  const onTouchStart = (ev: TouchEvent) => {
    if (ev.touches.length !== 1) {
      reset();
      state.multiTouch = true;
      return;
    }

    const touch = ev.touches[0];
    state.startX = touch.clientX;
    state.startY = touch.clientY;
    state.lastY = touch.clientY;
    state.residualPx = 0;
    state.didScroll = false;
    state.movedTooFar = false;
    state.multiTouch = false;
  };

  const onTouchMove = (ev: TouchEvent) => {
    if (ev.touches.length !== 1) {
      reset();
      state.multiTouch = true;
      return;
    }
    const touch = ev.touches[0];
    if (state.lastY === null || !touch) {
      reset();
      return;
    }
    if (!state.movedTooFar && exceedsTapDistance(state, touch.clientX, touch.clientY)) {
      state.movedTooFar = true;
    }

    const nextY = touch.clientY;

    const deltaPx = state.lastY - nextY;
    state.lastY = nextY;

    if (Math.abs(deltaPx) < TOUCH_SCROLL_MIN_PX) {
      return;
    }

    state.residualPx += deltaPx;
    const lines = state.residualPx / TOUCH_SCROLL_PX_PER_LINE;
    const wholeLines = lines < 0 ? Math.ceil(lines) : Math.floor(lines);

    if (wholeLines === 0 || !canScrollTerminal(term, wholeLines)) {
      return;
    }

    state.residualPx -= wholeLines * TOUCH_SCROLL_PX_PER_LINE;
    state.didScroll = true;
    term.scrollLines(wholeLines);
    ev.preventDefault();
    ev.stopPropagation();
  };

  const onTouchEnd = (ev: TouchEvent) => {
    maybeFocusTerminalOnTap(term, state, ev);
    if (ev.touches.length === 0) {
      reset();
    }
  };

  host.addEventListener("touchstart", onTouchStart, PASSIVE_CAPTURE);
  host.addEventListener("touchmove", onTouchMove, ACTIVE_CAPTURE);
  host.addEventListener("touchcancel", reset, CAPTURE);
  host.addEventListener("touchend", onTouchEnd, ACTIVE_CAPTURE);

  return () => {
    host.removeEventListener("touchstart", onTouchStart, PASSIVE_CAPTURE);
    host.removeEventListener("touchmove", onTouchMove, ACTIVE_CAPTURE);
    host.removeEventListener("touchcancel", reset, CAPTURE);
    host.removeEventListener("touchend", onTouchEnd, ACTIVE_CAPTURE);
  };
}
