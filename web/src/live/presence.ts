// The sheet's list after a live update: rows that moved slide to their new place (FLIP,
// 320 ms), new rows enter (opacity and a 10 px slide, 320 ms), rows that left fade out where
// they were (220 ms), and the row whose risk changed flashes its level tint (1.4 s). Any
// other change (first load, a filter, sorting) is shown at once, as is everything under
// reduced motion or in a hidden tab.
//
// Usage: const m = useListMotion(items, (r) => r.id, (r) => [r.id, r.story.key]);
//   <div ref={m.ref} className="rp-rows"> …rows with {...m.row(key)}… {m.ghosts.map(render as leaving)} </div>
import { useCallback, useLayoutEffect, useRef, useState, type CSSProperties } from 'react';
import { D3, motionOff } from '../lib/motion';
import { FLASH_MS, liveRecent, takeFlashes } from './bus';

type Box = { top: number; left: number; width: number; height: number };
export type Ghost<T> = { key: string; item: T; style: CSSProperties };

const LEAVE_MS = 220;
const ATTR = 'data-motion-key';

export function useListMotion<T>(items: readonly T[] | undefined, keyOf: (t: T) => string, flashKeysOf: (t: T) => string[] = (t) => [keyOf(t)]) {
  const container = useRef<HTMLElement | null>(null);
  const boxes = useRef(new Map<string, Box>());
  const prev = useRef<readonly T[] | null>(null);
  const [ghosts, setGhosts] = useState<Ghost<T>[]>([]);
  const keyRef = useRef(keyOf);
  const flashRef = useRef(flashKeysOf);
  useLayoutEffect(() => {
    keyRef.current = keyOf;
    flashRef.current = flashKeysOf;
  });

  const ref = useCallback((el: HTMLElement | null) => {
    container.current = el;
  }, []);

  useLayoutEffect(() => {
    const el = container.current;
    if (!el || !items) return;
    const key = keyRef.current;
    const origin = el.getBoundingClientRect();
    const rows = new Map<string, HTMLElement>();
    for (const r of el.querySelectorAll<HTMLElement>(`[${ATTR}]`)) if (!r.hasAttribute('inert')) rows.set(r.getAttribute(ATTR)!, r);
    const measure = (r: HTMLElement): Box => {
      const b = r.getBoundingClientRect();
      return { top: b.top - origin.top, left: b.left - origin.left, width: b.width, height: b.height };
    };

    const before = prev.current;
    const animate = before !== null && before !== items && liveRecent('full') && !motionOff();
    // Each timer only undoes its own class or style, so an unmount mid-animation is harmless.
    const later = (fn: () => void, ms: number) => void setTimeout(fn, ms);

    if (animate) {
      const now = new Set(items.map(key));
      // Leaving: drawn where they were, out of the flow.
      const gone: Ghost<T>[] = [];
      for (const it of before) {
        const k = key(it);
        const b = boxes.current.get(k);
        if (now.has(k) || !b) continue;
        gone.push({ key: k, item: it, style: { position: 'absolute', top: b.top, left: b.left, width: b.width, height: b.height, margin: 0, boxSizing: 'border-box' } });
      }
      if (gone.length) {
        setGhosts((g) => [...g.filter((x) => !gone.some((y) => y.key === x.key)), ...gone]);
        const keys = new Set(gone.map((g) => g.key));
        later(() => setGhosts((g) => g.filter((x) => !keys.has(x.key))), LEAVE_MS + 40);
      }
      // FLIP the rows that stayed; the new ones enter.
      const moved: HTMLElement[] = [];
      for (const [k, r] of rows) {
        const first = boxes.current.get(k);
        if (!first) {
          r.classList.add('rp-enter');
          later(() => r.classList.remove('rp-enter'), D3 + 60);
          continue;
        }
        const dy = first.top - measure(r).top;
        if (Math.abs(dy) > 0.5) {
          r.style.transition = 'none';
          r.style.transform = `translateY(${dy}px)`;
          moved.push(r);
        }
      }
      if (moved.length) {
        void el.offsetHeight; // commit the inverted positions before playing
        for (const r of moved) {
          r.style.transition = 'transform var(--d3) var(--ease)';
          r.style.transform = '';
        }
        later(() => moved.forEach((r) => (r.style.transition = '')), D3 + 60);
      }
    }

    // The changed rows flash (requested by the player when their pulse landed).
    if (before !== items) {
      const byFlash = new Map<string, HTMLElement>();
      for (const it of items) {
        const r = rows.get(key(it));
        if (r) for (const fk of flashRef.current(it)) byFlash.set(fk, r);
      }
      const hit = motionOff() ? new Set<string>() : takeFlashes(byFlash.keys());
      const flashed = new Set<HTMLElement>();
      for (const fk of hit) {
        const r = byFlash.get(fk);
        if (!r || flashed.has(r)) continue;
        flashed.add(r);
        r.classList.remove('rp-flash');
        void r.offsetWidth;
        r.classList.add('rp-flash');
        later(() => r.classList.remove('rp-flash'), FLASH_MS + 60);
      }
    }

    // Where every row is now, for the next change.
    const next = new Map<string, Box>();
    for (const [k, r] of rows) {
      const b = measure(r);
      // a row mid-FLIP is measured where it is headed
      const t = r.style.transform.match(/translateY\((-?[\d.]+)px\)/);
      next.set(k, t ? { ...b, top: b.top - Number(t[1]) } : b);
    }
    boxes.current = next;
    prev.current = items;
  }, [items]);

  /** Props for a row: its key for measuring. */
  const row = useCallback((key: string) => ({ [ATTR]: key }), []);
  return { ref, row, ghosts };
}
