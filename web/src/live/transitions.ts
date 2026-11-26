// View Transitions between the list and a risk (src/lib/router.tsx calls `withTransition`).
// Going to /risks/:id from the Overview or the Register, the risk's title in the sheet
// morphs into the risk's header (`risk-title`, 320 ms) and the sheet's content slides 12 px
// in from the right (out 120 ms, in 200 ms); going back it slides in from the left. The page
// itself is not captured (`:root { view-transition-name: none }`, live.css), so the map
// keeps drawing underneath. Where `document.startViewTransition` is missing, or under
// reduced motion, the screens just swap.
import { flushSync } from 'react-dom';
import { reducedMotion } from '../lib/motion';

const RISK = /^\/risks\/([^/?#]+)/;
const LIST = /^\/(?:register)?(?:[?#]|$)/;

/** Which way a navigation goes, if it is one that morphs; `id` is the risk's. */
export function transitionFor(from: string, to: string): { dir: 'fwd' | 'back'; id: string } | null {
  const a = RISK.exec(to);
  if (a && LIST.test(from)) return { dir: 'fwd', id: decodeURIComponent(a[1]) };
  const b = RISK.exec(from);
  if (b && LIST.test(to)) return { dir: 'back', id: decodeURIComponent(b[1]) };
  return null;
}

/** The element that carries a risk's title on screen (a row, the selected card, the header). */
const titleOf = (id: string) => document.querySelector<HTMLElement>(`[data-vt-title="${CSS.escape(id)}"]`);
const sheet = () => document.querySelector<HTMLElement>('aside.sheet');

type VT = { finished: Promise<void>; ready: Promise<void>; updateCallbackDone: Promise<void> };
type Doc = Document & { startViewTransition?: (cb: () => void) => VT };

/** Run `update` (which changes the route) inside a view transition when one applies. */
export function withTransition(from: string, to: string, update: () => void) {
  const t = transitionFor(from, to);
  const doc = document as Doc;
  if (!t || !doc.startViewTransition || reducedMotion() || document.hidden) {
    update();
    return;
  }
  const root = document.documentElement;
  root.dataset.vtDir = t.dir;
  // One element per name: the old ones now, the new ones inside the update.
  const named: HTMLElement[] = [];
  const name = (el: HTMLElement | null, n: string) => {
    if (!el) return;
    el.style.viewTransitionName = n;
    named.push(el);
  };
  const clear = () => {
    for (const el of named.splice(0)) el.style.viewTransitionName = '';
  };
  name(titleOf(t.id), 'risk-title');
  name(sheet(), 'rp-sheet');
  let vt: VT;
  try {
    vt = doc.startViewTransition(() => {
      clear();
      flushSync(update);
      name(titleOf(t.id), 'risk-title');
      name(sheet(), 'rp-sheet');
    });
  } catch {
    clear();
    update();
    return;
  }
  vt.ready.catch(() => undefined);
  vt.updateCallbackDone.catch(() => undefined);
  vt.finished.catch(() => undefined).finally(() => {
    clear();
    delete root.dataset.vtDir;
  });
}
