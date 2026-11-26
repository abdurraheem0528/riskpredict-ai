// The live sequence's hand-off to the sheet. The player (./player) applies a risk's new state
// when its pulse lands; then the list may animate and the changed row flashes. Changes that
// do not follow a live apply (first load, a filter, a workspace switch) are shown at once.
//
// Two kinds of apply: "full" (a played sequence: count-ups, FLIP reorder, rows entering and
// leaving, the flash) and "count" (risks past the per-poll cap: only the count-up).
import { useEffect, useState } from 'react';

/** How long after a live apply a list change still counts as live (refetch latency). */
export const LIVE_WINDOW_MS = 5000;
/** The flash on a changed row (live.css `rp-flash`). */
export const FLASH_MS = 1400;

export type ApplyMode = 'full' | 'count';

let lastFull = -Infinity;
let lastAny = -Infinity;
const flashes = new Map<string, number>(); // key → when requested
const listeners = new Set<() => void>();
const emit = () => listeners.forEach((l) => l());

const now = () => (typeof performance !== 'undefined' ? performance.now() : Date.now());

/** A live event's state was just applied (its queries invalidated). */
export function noteLiveApply(mode: ApplyMode, flashKeys: readonly string[] = []) {
  const t = now();
  lastAny = t;
  if (mode === 'full') lastFull = t;
  for (const k of flashKeys) if (k) flashes.set(k, t);
  emit();
}

/**
 * True for a short while after a live apply, so a list change then animates. `full` asks
 * for a played sequence (FLIP, enter/leave); `count` for any apply (count-ups).
 */
export function liveRecent(mode: ApplyMode = 'count', at = now()): boolean {
  return at - (mode === 'full' ? lastFull : lastAny) < LIVE_WINDOW_MS;
}

/** The keys (risk ids, story keys) among `keys` waiting to flash; they are consumed. */
export function takeFlashes(keys: Iterable<string>): Set<string> {
  const t = now();
  const out = new Set<string>();
  for (const k of keys) {
    const at = flashes.get(k);
    if (at == null) continue;
    flashes.delete(k);
    if (t - at < LIVE_WINDOW_MS) out.add(k);
  }
  for (const [k, at] of flashes) if (t - at >= LIVE_WINDOW_MS) flashes.delete(k);
  return out;
}

export function subscribeLive(l: () => void): () => void {
  listeners.add(l);
  return () => {
    listeners.delete(l);
  };
}

/** Re-renders on every live apply; the value is the apply's time. */
export function useLiveTick(): number {
  const [t, setT] = useState(lastAny);
  useEffect(() => subscribeLive(() => setT(lastAny)), []);
  return t;
}

/** For tests. */
export function __resetLive() {
  lastFull = lastAny = -Infinity;
  flashes.clear();
}
