// Globe or Flat, chosen on the map controls and remembered like the theme (`rp.projection`).
import { useSyncExternalStore } from 'react';

export type Projection = 'globe' | 'flat';
export const PROJECTION_KEY = 'rp.projection';

export function parseProjection(v: unknown): Projection {
  return v === 'flat' ? 'flat' : 'globe';
}

function read(): Projection {
  try {
    return parseProjection(localStorage.getItem(PROJECTION_KEY));
  } catch {
    return 'globe'; // storage blocked
  }
}

let current: Projection = typeof window === 'undefined' ? 'globe' : read();
const listeners = new Set<() => void>();

export function getProjection(): Projection {
  return current;
}

export function setProjection(p: Projection) {
  if (p === current) return;
  current = p;
  try {
    localStorage.setItem(PROJECTION_KEY, p);
  } catch {
    /* storage blocked: the choice lasts for this page */
  }
  for (const l of listeners) l();
}

/** For tests: re-read the stored choice. */
export function __reloadProjection() {
  current = read();
  for (const l of listeners) l();
}

export function subscribeProjection(l: () => void): () => void {
  listeners.add(l);
  return () => {
    listeners.delete(l);
  };
}

export function useProjection(): Projection {
  return useSyncExternalStore(subscribeProjection, getProjection, () => 'globe' as Projection);
}

/** MapLibre's projection spec for a choice. */
export const projectionSpec = (p: Projection) => ({ type: p === 'globe' ? 'globe' : 'mercator' }) as const;
