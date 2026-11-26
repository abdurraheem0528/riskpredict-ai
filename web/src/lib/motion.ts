// One requestAnimationFrame loop for everything that moves on the map and in the sheet:
// feature-state tweens, the sea-lane dash flow, idle rotation, travelling lines, count-ups.
// It stops while the tab is hidden (and restarts when it is shown), pauses CSS animations
// through a class on <html>, and under `prefers-reduced-motion: reduce` every tween jumps
// to its end.

/** `cubic-bezier(x1, y1, x2, y2)` as a function of progress 0..1. */
export function bezier(x1: number, y1: number, x2: number, y2: number): (t: number) => number {
  const cx = 3 * x1, bx = 3 * (x2 - x1) - cx, ax = 1 - cx - bx;
  const cy = 3 * y1, by = 3 * (y2 - y1) - cy, ay = 1 - cy - by;
  const X = (t: number) => ((ax * t + bx) * t + cx) * t;
  const Y = (t: number) => ((ay * t + by) * t + cy) * t;
  const dX = (t: number) => (3 * ax * t + 2 * bx) * t + cx;
  return (x) => {
    if (x <= 0) return 0;
    if (x >= 1) return 1;
    let t = x;
    for (let i = 0; i < 8; i++) {
      const e = X(t) - x, d = dX(t);
      if (Math.abs(e) < 1e-5) return Y(t);
      if (Math.abs(d) < 1e-6) break;
      t -= e / d;
    }
    // Newton did not settle: bisect.
    let lo = 0, hi = 1;
    t = x;
    for (let i = 0; i < 24; i++) {
      const v = X(t);
      if (Math.abs(v - x) < 1e-5) break;
      if (v < x) lo = t;
      else hi = t;
      t = (lo + hi) / 2;
    }
    return Y(t);
  };
}

/** The `--ease` token, cubic-bezier(.2, 0, 0, 1). */
export const EASE = bezier(0.2, 0, 0, 1);
/** Ease-out for count-ups. */
export const EASE_OUT = (t: number) => 1 - (1 - t) ** 3;
export const EASE_IN_OUT = bezier(0.42, 0, 0.58, 1);
export const LINEAR = (t: number) => t;

/** The motion tokens (styles.css), in ms. */
export const D1 = 120, D2 = 200, D3 = 320, D4 = 600;

const RM_QUERY = '(prefers-reduced-motion: reduce)';
export function reducedMotion(): boolean {
  return typeof window !== 'undefined' && !!window.matchMedia?.(RM_QUERY).matches;
}
/** True when nothing should animate: reduced motion, or the tab is hidden. */
export function motionOff(): boolean {
  return reducedMotion() || (typeof document !== 'undefined' && document.hidden);
}

// --- the loop ------------------------------------------------------------------------------

/** Called every frame with the frame time and the ms since the last frame. Return false to stop. */
export type FrameFn = (now: number, dt: number) => boolean | void;

const subs = new Set<FrameFn>();
let raf = 0;
let last = 0;
let wired = false;

function wire() {
  if (wired || typeof document === 'undefined') return;
  wired = true;
  const sync = () => {
    document.documentElement.classList.toggle('rp-hidden', document.hidden);
    if (document.hidden) {
      if (raf) cancelAnimationFrame(raf);
      raf = 0;
    } else kick();
  };
  document.addEventListener('visibilitychange', sync);
  sync();
}

function kick() {
  if (raf || !subs.size || typeof requestAnimationFrame === 'undefined') return;
  if (typeof document !== 'undefined' && document.hidden) return;
  last = 0;
  raf = requestAnimationFrame(frame);
}

function frame(now: number) {
  raf = 0;
  const dt = last ? Math.min(100, now - last) : 16;
  last = now;
  for (const f of [...subs]) {
    let keep: boolean | void = true;
    try {
      keep = f(now, dt);
    } catch {
      keep = false; // a map removed under a running tween
    }
    if (keep === false) subs.delete(f);
  }
  if (subs.size) {
    if (!(typeof document !== 'undefined' && document.hidden)) raf = requestAnimationFrame(frame);
  } else last = 0;
}

/** Run `fn` every frame until it returns false or the returned function is called. */
export function onFrame(fn: FrameFn): () => void {
  wire();
  subs.add(fn);
  kick();
  return () => {
    subs.delete(fn);
  };
}

// --- tweens --------------------------------------------------------------------------------

export type Tween = { cancel: () => void; finish: () => void };

/**
 * Animate progress 0 → 1 over `ms`. `step` gets the eased progress. With motion off (reduced
 * motion or a hidden tab) or `ms <= 0`, `step(1)` and `done` run at once.
 */
export function tween(
  ms: number,
  step: (p: number) => void,
  opts: { ease?: (t: number) => number; delay?: number; done?: () => void } = {},
): Tween {
  const ease = opts.ease ?? EASE;
  let finished = false;
  const end = () => {
    if (finished) return;
    finished = true;
    step(1);
    opts.done?.();
  };
  if (ms <= 0 || motionOff()) {
    end();
    return { cancel: () => undefined, finish: () => undefined };
  }
  let t0 = -1;
  const delay = opts.delay ?? 0;
  const stop = onFrame((now) => {
    if (finished) return false;
    if (t0 < 0) t0 = now + delay;
    if (now < t0) return true;
    const p = Math.min(1, (now - t0) / ms);
    if (p >= 1) {
      end();
      return false;
    }
    step(ease(p));
    return true;
  });
  // A tab hidden mid-tween: land at the end rather than freeze half way.
  const onHide = () => {
    if (document.hidden) {
      stop();
      end();
    }
  };
  document.addEventListener('visibilitychange', onHide, { once: true });
  return {
    cancel: () => {
      finished = true;
      stop();
      document.removeEventListener('visibilitychange', onHide);
    },
    finish: () => {
      stop();
      document.removeEventListener('visibilitychange', onHide);
      end();
    },
  };
}

/** setTimeout that runs at once (and in order) when motion is off or the tab hides. */
export function later(fn: () => void, ms: number): () => void {
  if (motionOff() || ms <= 0) {
    fn();
    return () => undefined;
  }
  let done = false;
  const run = () => {
    if (done) return;
    done = true;
    clearTimeout(id);
    document.removeEventListener('visibilitychange', onHide);
    fn();
  };
  const onHide = () => {
    if (document.hidden) run();
  };
  const id = setTimeout(run, ms);
  document.addEventListener('visibilitychange', onHide);
  return () => {
    done = true;
    clearTimeout(id);
    document.removeEventListener('visibilitychange', onHide);
  };
}

export const lerp = (a: number, b: number, t: number) => a + (b - a) * t;
export const clamp = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, v));
