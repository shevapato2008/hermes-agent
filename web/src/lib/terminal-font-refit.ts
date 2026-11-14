/**
 * Dashboard xterm font stack. 'JetBrains Mono' is bundled via @font-face in
 * index.css with `font-display: swap`, and "SmartBox Kai" provides self-hosted
 * KaiTi CJK glyphs via unicode-range shards from /fonts-kai/.
 */
export const TERMINAL_FONT_FAMILY =
  "'JetBrains Mono', 'Cascadia Mono', 'Fira Code', 'MesloLGS NF', 'Source Code Pro', Menlo, Consolas, 'DejaVu Sans Mono', \"SmartBox Kai\", \"LXGW WenKai\", \"Kaiti SC\", \"STKaiti\", \"KaiTi\", monospace";

const BUNDLED_FACES = ["400", "700", "italic 400"].map(
  (descriptor) => `${descriptor} 1em 'JetBrains Mono'`,
);

const KAITI_PREWARM_DESCRIPTOR = '16px "SmartBox Kai"';
const KAITI_PREWARM_SAMPLE = "中文对话会话智星盒系统文件";

type TerminalFontSet = Pick<FontFaceSet, "check" | "load"> & {
  addEventListener?: (type: string, listener: () => void) => void;
  removeEventListener?: (type: string, listener: () => void) => void;
};

export interface RemeasurableTerminal {
  options: { fontFamily?: string };
  rows: number;
  clearTextureAtlas(): void;
  refresh(start: number, end: number): void;
}

function browserFontSet(): TerminalFontSet | undefined {
  return typeof document === "undefined" ? undefined : document.fonts;
}

/**
 * xterm measures its cell size once at open() and afterwards only when
 * fontFamily/fontSize *change* or the grid resizes. When the bundled font
 * swaps in later, the grid keeps fallback-font metrics (and the WebGL atlas
 * keeps fallback glyphs) until something resizes the host, e.g. toggling the
 * sidebar (#92899). Once the bundled faces load, force a re-measure, refit,
 * and redraw. Also prewarm SmartBox Kai CJK glyphs and clear/refresh the
 * glyph atlas whenever lazy unicode-range shards finish loading.
 */
export function refitWhenTerminalFontLoads(
  term: RemeasurableTerminal,
  fit: () => void,
  fontSet: TerminalFontSet | undefined = browserFontSet(),
): () => void {
  if (!fontSet?.load) return () => undefined;

  let cancelled = false;
  const onLoadingDone = () => {
    if (cancelled) return;
    term.clearTextureAtlas();
    if (term.rows > 0) term.refresh(0, term.rows - 1);
  };
  fontSet.addEventListener?.("loadingdone", onLoadingDone);

  const cleanup = () => {
    cancelled = true;
    fontSet.removeEventListener?.("loadingdone", onLoadingDone);
  };

  let kaitiLoaded = false;
  try {
    kaitiLoaded = fontSet.check(KAITI_PREWARM_DESCRIPTOR, KAITI_PREWARM_SAMPLE);
  } catch {
    kaitiLoaded = false;
  }
  if (!kaitiLoaded) {
    void Promise.resolve()
      .then(() => fontSet.load(KAITI_PREWARM_DESCRIPTOR, KAITI_PREWARM_SAMPLE))
      .catch(() => undefined);
  }

  try {
    if (BUNDLED_FACES.every((face) => fontSet.check(face))) {
      return cleanup;
    }
  } catch {
    /* check() throws on unparsable descriptors in some engines; just load */
  }

  void Promise.allSettled(
    BUNDLED_FACES.map((face) => Promise.resolve().then(() => fontSet.load(face))),
  ).then((results) => {
    if (cancelled) return;
    const loaded = results.some(
      (r) => r.status === "fulfilled" && r.value.length > 0,
    );
    if (!loaded) return;

    // A same-value assignment is a no-op in xterm, so bounce through a
    // generic family to make it re-measure against the loaded face.
    const family = term.options.fontFamily ?? TERMINAL_FONT_FAMILY;
    term.options.fontFamily = "monospace";
    term.options.fontFamily = family;
    fit();
    term.clearTextureAtlas();
    if (term.rows > 0) term.refresh(0, term.rows - 1);
  });

  return cleanup;
}
