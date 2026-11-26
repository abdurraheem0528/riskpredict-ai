// The one tween engine for feature-state: hover lift (200 ms), selection ring (320 ms) and
// filter dimming (200 ms) ease a numeric state 0 ↔ 1 per feature on motion.ts's frame
// loop. Only the features that change are touched each frame. With motion off the value
// is set at once.
import type { Map as MlMap } from 'maplibre-gl';
import { EASE, motionOff, onFrame } from '../lib/motion';

type Key = string; // `${source}\u0000${id}\u0000${state}`
type Run = { map: MlMap; source: string; id: string | number; state: string; from: number; to: number; t0: number; ms: number };

const values = new Map<Key, number>();
const runs = new Map<Key, Run>();
let stop: (() => void) | null = null;

const keyOf = (source: string, id: string | number, state: string) => `${source}\u0000${id}\u0000${state}`;

function write(map: MlMap, source: string, id: string | number, state: string, v: number) {
  try {
    map.setFeatureState({ source, id }, { [state]: v });
  } catch {
    /* source removed */
  }
}

function step(now: number) {
  for (const [k, r] of runs) {
    const p = Math.min(1, (now - r.t0) / r.ms);
    const v = r.from + (r.to - r.from) * EASE(p);
    values.set(k, v);
    write(r.map, r.source, r.id, r.state, v);
    if (p >= 1) runs.delete(k);
  }
  if (!runs.size) {
    stop = null;
    return false;
  }
  return true;
}

/** Ease feature `id`'s numeric `state` in `source` to `to` over `ms`. */
export function tweenState(map: MlMap, source: string, id: string | number, state: string, to: number, ms: number) {
  const k = keyOf(source, id, state);
  const from = runs.get(k) ? (values.get(k) ?? 0) : (values.get(k) ?? 0);
  if (from === to && !runs.has(k)) return;
  if (motionOff() || ms <= 0) {
    runs.delete(k);
    values.set(k, to);
    write(map, source, id, state, to);
    return;
  }
  runs.set(k, { map, source, id, state, from, to, t0: performance.now(), ms });
  if (!stop) stop = onFrame((now) => step(now));
}

/** Set at once (no tween), e.g. many features at a time. */
export function setState(map: MlMap, source: string, id: string | number, state: string, to: number) {
  const k = keyOf(source, id, state);
  runs.delete(k);
  values.set(k, to);
  write(map, source, id, state, to);
}

export function stateOf(source: string, id: string | number, state: string): number {
  return values.get(keyOf(source, id, state)) ?? 0;
}

/** Forget a source's states (its layers were removed or its data replaced from scratch). */
export function forgetSource(source: string) {
  const prefix = `${source}\u0000`;
  for (const k of [...values.keys()]) if (k.startsWith(prefix)) values.delete(k);
  for (const k of [...runs.keys()]) if (k.startsWith(prefix)) runs.delete(k);
}

/** Re-apply the known states of a source after its data was set again (setData keeps them, but a new source does not). */
export function reapply(map: MlMap, source: string) {
  const prefix = `${source}\u0000`;
  for (const [k, v] of values) {
    if (!k.startsWith(prefix)) continue;
    const [, id, state] = k.split('\u0000');
    write(map, source, id, state, v);
  }
}
