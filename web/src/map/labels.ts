// Map text drawn by MapLibre itself, so labels, cluster counts and callouts take part in
// symbol collision. The style has no `glyphs` URL: MapLibre 5 then draws every glyph
// locally with TinySDF, using the `text-font` name as a CSS font family and reading the
// weight from words in it ("SemiBold" → 600). Two FontFace aliases of Inter make that
// work, and they are loaded before any label is drawn (a glyph drawn before the font
// arrives would be cached in the fallback face).
import { useEffect, useMemo, useState } from 'react';
import type { Feature, FeatureCollection, Point } from 'geojson';
import type { Map as MlMap } from 'maplibre-gl';
import latin from '@fontsource-variable/inter/files/inter-latin-opsz-normal.woff2?url';
import latinExt from '@fontsource-variable/inter/files/inter-latin-ext-opsz-normal.woff2?url';
import { LEVELS, type Level } from '../lib/format';

export const FONT = 'Inter Map';
export const FONT_SEMIBOLD = 'Inter Map SemiBold';
const LATIN = 'U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+0304,U+0308,U+0329,U+2000-206F,U+20AC,U+2122,U+2191,U+2193,U+2212,U+2215,U+FEFF,U+FFFD';
const LATIN_EXT = 'U+0100-02BA,U+02BD-02C5,U+02C7-02CC,U+02CE-02D7,U+02DD-02FF,U+0304,U+0308,U+0329,U+1D00-1DBF,U+1E00-1E9F,U+1EF2-1EFF,U+2020,U+20A0-20AB,U+20AD-20C0,U+2113,U+2C60-2C7F,U+A720-A7FF';

let latinReady: Promise<void> | null = null;
let extReady: Promise<void> | null = null;

function addFaces(url: string, range: string): Promise<void> {
  if (typeof FontFace === 'undefined' || typeof document === 'undefined' || !document.fonts) return Promise.resolve();
  const faces = [FONT, FONT_SEMIBOLD].map((family) => new FontFace(family, `url(${url})`, { weight: '100 900', unicodeRange: range }));
  faces.forEach((f) => document.fonts.add(f));
  return Promise.all(faces.map((f) => f.load())).then(
    () => undefined,
    () => undefined,
  );
}

/** Inter for map text (Latin), loaded once; resolves after at most 2.5 s either way. */
export function loadMapFonts(): Promise<void> {
  if (!latinReady) {
    latinReady = Promise.race([addFaces(latin, LATIN), new Promise<void>((r) => setTimeout(r, 2500))]);
  }
  return latinReady;
}
/** Latin Extended (Ł, ő, ș…), loaded only when a label needs it. */
function loadExtFonts(): Promise<void> {
  if (!extReady) extReady = Promise.race([addFaces(latinExt, LATIN_EXT), new Promise<void>((r) => setTimeout(r, 2500))]);
  return extReady;
}
const NEEDS_EXT = /[Ā-ɏḀ-ỿ]/;

// --- images ----------------------------------------------------------------------------

const S = 2; // pixel ratio of the drawn images

function canvas(w: number, h: number) {
  const c = document.createElement('canvas');
  c.width = w * S;
  c.height = h * S;
  const g = c.getContext('2d');
  if (!g) return null;
  g.scale(S, S);
  return { c, g };
}

function capsule(g: CanvasRenderingContext2D, x: number, y: number, w: number, h: number) {
  const r = h / 2;
  g.beginPath();
  g.moveTo(x + r, y);
  g.lineTo(x + w - r, y);
  g.arc(x + w - r, y + r, r, -Math.PI / 2, Math.PI / 2);
  g.lineTo(x + r, y + h);
  g.arc(x + r, y + r, r, Math.PI / 2, (3 * Math.PI) / 2);
  g.closePath();
}

const M = 5; // room for the shadow around a capsule
/** Callout geometry (logical px, inside the shadow margin). */
export const CALLOUT = { h: 34, pillX: 5, pillW: 36, textLeft: 14, labelLeft: 49, right: 12 };

/**
 * Stretchable images: a white capsule for cluster counts, and a callout per risk level
 * (white capsule with the level's tinted score pill), plain and selected.
 */
export function addLabelImages(map: MlMap) {
  if (typeof document === 'undefined') return;
  const add = (id: string, draw: () => { data: ImageData; stretchX: [number, number][]; stretchY: [number, number][]; content: [number, number, number, number] } | null) => {
    if (map.hasImage(id)) return;
    const img = draw();
    if (img) map.addImage(id, img.data, { pixelRatio: S, stretchX: img.stretchX, stretchY: img.stretchY, content: img.content });
  };

  add('rp-bubble', () => {
    const m = 4, d = 28;
    const cv = canvas(d + 2 * m, d + 2 * m);
    if (!cv) return null;
    const { c, g } = cv;
    g.shadowColor = 'rgba(20,21,24,0.14)';
    g.shadowBlur = 4;
    g.shadowOffsetY = 1;
    capsule(g, m, m, d, d);
    g.fillStyle = '#FFFFFF';
    g.fill();
    g.shadowColor = 'transparent';
    g.strokeStyle = 'rgba(20,21,24,0.08)';
    g.lineWidth = 1;
    capsule(g, m + 0.5, m + 0.5, d - 1, d - 1);
    g.stroke();
    return {
      data: g.getImageData(0, 0, c.width, c.height),
      stretchX: [[(m + 13) * S, (m + 15) * S]],
      stretchY: [[(m + 13) * S, (m + 15) * S]],
      content: [(m + 8) * S, (m + 7) * S, (m + 20) * S, (m + 21) * S],
    };
  });

  for (const lvl of Object.keys(LEVELS) as Level[]) {
    for (const on of [false, true]) {
      add(`rp-callout-${lvl}${on ? '-on' : ''}`, () => {
        const { h, pillX, pillW, textLeft, labelLeft, right } = CALLOUT;
        const w = labelLeft + 20 + right;
        const cv = canvas(w + 2 * M, h + 2 * M);
        if (!cv) return null;
        const { c, g } = cv;
        g.shadowColor = 'rgba(20,21,24,0.14)';
        g.shadowBlur = 6;
        g.shadowOffsetY = 2;
        capsule(g, M, M, w, h);
        g.fillStyle = '#FFFFFF';
        g.fill();
        g.shadowColor = 'transparent';
        if (on) {
          g.strokeStyle = '#1766F0';
          g.lineWidth = 2;
          capsule(g, M + 1, M + 1, w - 2, h - 2);
          g.stroke();
        }
        capsule(g, M + pillX, M + 4, pillW, h - 8);
        g.fillStyle = LEVELS[lvl].tint;
        g.fill();
        return {
          data: g.getImageData(0, 0, c.width, c.height),
          stretchX: [[(M + labelLeft) * S, (M + w - right) * S]],
          stretchY: [[(M + h / 2 - 1) * S, (M + h / 2 + 1) * S]],
          content: [(M + textLeft) * S, (M + 9.5) * S, (M + w - right) * S, (M + h - 9.5) * S],
        };
      });
    }
  }
}

// --- anchors that keep text inside the viewport ------------------------------------------

export type Anchor = 'left' | 'right' | 'top' | 'bottom';
type Box = [number, number, number, number]; // x0, y0, x1, y1 relative to the point

export type LabelSpec = {
  /** Canvas font used to measure the text, e.g. `600 13px "Inter Map SemiBold"`. */
  font: string;
  /** Text of a feature as drawn (measured). Null means no label. */
  text: (p: Record<string, unknown>) => string | null;
  /** Extra width around the text (padding of a capsule). */
  extra: number;
  /** Anchors to try, in order, with their text-variable-anchor-offset (ems). */
  anchors: [Anchor, [number, number]][];
  /** Screen box of the label for an anchor and a measured text width. */
  box: (a: Anchor, width: number) => Box;
};

let measurer: CanvasRenderingContext2D | null = null;
const widths = new Map<string, number>();
function measure(font: string, text: string): number {
  const key = `${font}|${text}`;
  const hit = widths.get(key);
  if (hit != null) return hit;
  if (!measurer && typeof document !== 'undefined') measurer = document.createElement('canvas').getContext('2d');
  if (!measurer) return text.length * 7;
  measurer.font = font;
  const w = measurer.measureText(text).width;
  widths.set(key, w);
  return w;
}

/**
 * Where text may go: the map minus its edges, the top row, and everything floating over
 * it (sheets, corner cards, the legend, map controls), so no label is cut off by them.
 */
function safeArea(map: MlMap) {
  const c = map.getContainer();
  const origin = c.getBoundingClientRect();
  const covers: Box[] = [];
  const app = c.closest('.app') ?? document;
  for (const el of app.querySelectorAll('.sheet, .side, .legend, .ctrl, .card, .top > *')) {
    const r = el.getBoundingClientRect();
    if (r.width && r.height) covers.push([r.left - origin.left - 6, r.top - origin.top - 6, r.right - origin.left + 6, r.bottom - origin.top + 6]);
  }
  return { l: 8, t: 8, r: c.clientWidth - 8, b: c.clientHeight - 8, covers };
}

function fits(area: ReturnType<typeof safeArea>, b: Box) {
  if (b[0] < area.l || b[2] > area.r || b[1] < area.t || b[3] > area.b) return false;
  return !area.covers.some((c) => b[0] < c[2] && b[2] > c[0] && b[1] < c[3] && b[3] > c[1]);
}

/**
 * Give each feature the anchors under which its label stays fully inside the open map
 * (`_vao`, for `text-variable-anchor-offset`), or `_hide` when none does. MapLibre then
 * places the label at the first of those anchors that does not collide, or hides it.
 * Recomputed after every move.
 */
export function useViewportAnchors<P extends Record<string, unknown>>(
  map: MlMap | null,
  fc: FeatureCollection<Point, P> | null,
  spec: LabelSpec,
) {
  const [tick, setTick] = useState(0);
  const [fontsOk, setFontsOk] = useState(false);
  useEffect(() => {
    if (!map) return;
    let raf = 0;
    const bump = () => {
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(() => setTick((t) => t + 1));
    };
    map.on('moveend', bump);
    map.on('resize', bump);
    window.addEventListener('rp:layout', bump);
    return () => {
      cancelAnimationFrame(raf);
      map.off('moveend', bump);
      map.off('resize', bump);
      window.removeEventListener('rp:layout', bump);
    };
  }, [map]);

  // Load Latin Extended first if any label needs it.
  const needsExt = useMemo(() => !!fc?.features.some((f) => NEEDS_EXT.test(spec.text(f.properties) ?? '')), [fc, spec]);
  useEffect(() => {
    let live = true;
    void (needsExt ? loadExtFonts() : loadMapFonts()).then(() => live && setFontsOk(true));
    return () => {
      live = false;
    };
  }, [needsExt]);

  return useMemo(() => {
    void tick;
    if (!fc || !map || !fontsOk) return null;
    const area = safeArea(map);
    const features = fc.features.map((f): Feature<Point, P & { _vao: unknown[]; _hide: boolean }> => {
      const text = spec.text(f.properties);
      const vao: unknown[] = [];
      if (text) {
        const p = map.project(f.geometry.coordinates as [number, number]);
        const w = measure(spec.font, text) + spec.extra;
        for (const [a, off] of spec.anchors) {
          const [x0, y0, x1, y1] = spec.box(a, w);
          if (fits(area, [p.x + x0, p.y + y0, p.x + x1, p.y + y1])) vao.push(a, off);
        }
      }
      return { ...f, properties: { ...f.properties, _vao: vao, _hide: vao.length === 0 } };
    });
    return { ...fc, features };
  }, [fc, map, spec, tick, fontsOk]);
}

/** Labels at the bottom, then callouts, then cluster counts on top (placed first). */
export function raiseSymbols(map: MlMap) {
  const ids = map.getStyle().layers.map((l) => l.id);
  for (const suffix of ['-label', '-callout', '-count']) for (const id of ids) if (id.endsWith(suffix)) map.moveLayer(id);
}
