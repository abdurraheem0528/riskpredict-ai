// The risk page fits one view: where the sheet has a fixed height (desktop and tablet), it
// shows fewer advice steps, then fewer news items, rather than scroll (tests/unit/fit.test.ts).
// `fitCounts` is pure: heights in, how many of each to show out. `measureSheet` reads those
// heights from the sheet body (src/screens/RiskView.tsx keeps them and decides when to ask).

export type FitInput = {
  /** The sheet body's height (its client height, padding included). */
  available: number;
  /**
   * Everything in the body that is not a step or a news item, nor the toggle under the steps:
   * the blocks and their headers, the gaps between blocks, the body's padding.
   */
  fixed: number;
  /** Each advice step's height, in order, and the gap between two steps. */
  steps: readonly number[];
  stepGap: number;
  /** Each news item's height, in the order shown (the default two at most), and the gap between two. */
  news: readonly number[];
  newsGap: number;
  /** What the "N more steps" button adds under the steps (its own height and the gap above it). */
  toggle: number;
};

/** How many steps and news items to show, and whether the page then fits without scrolling. */
export type Fit = { steps: number; news: number; fits: boolean };

const listHeight = (h: readonly number[], n: number, gap: number) => (n ? h.slice(0, n).reduce((a, b) => a + b, 0) + gap * (n - 1) : 0);

/** The height the body's content needs with `s` steps and `n` news items shown. */
export function fitNeed(i: FitInput, s: number, n: number): number {
  return i.fixed + listHeight(i.steps, s, i.stepGap) + listHeight(i.news, n, i.newsGap) + (s < i.steps.length ? i.toggle : 0);
}

/**
 * Everything, when it fits. Otherwise steps are held back first, from the last (3 → 2 → 1),
 * then news items (2 → 1); never fewer than one of each that there is. When one step and one
 * news item still do not fit, those are shown and `fits` is false: the sheet scrolls.
 */
export function fitCounts(i: FitInput): Fit {
  let s = i.steps.length;
  let n = i.news.length;
  while (fitNeed(i, s, n) > i.available) {
    if (s > 1) s--;
    else if (n > 1) n--;
    else break;
  }
  return { steps: s, news: n, fits: fitNeed(i, s, n) <= i.available };
}

/** What `measureSheet` reads from the sheet body. Heights in CSS px. */
export type SheetMeasure = {
  /** The body's client height, and its client width without a scrollbar. */
  available: number;
  width: number;
  /** The natural height of the body's content, from its top padding to its bottom padding. */
  content: number;
  /** Every step and news item shown (`data-fit-key`), with its height. */
  items: { key: string; height: number }[];
  /** The step and news lists' own heights (`data-fit="list"`), summed, and the gap within each list (by `data-fit-list`). */
  lists: number;
  gaps: Record<string, number>;
  /** The toggle under the steps (`data-fit="toggle"`) with the gap above it, when it is shown. */
  toggle: number | null;
  /** Blocks the person opened (`data-fit="skip"`: the working), each with the gap after it. */
  skip: number;
};

const px = (v: string) => parseFloat(v) || 0;

/**
 * Reads the sheet body's heights for `fitCounts`. The body's scrollbar is hidden while it reads,
 * so text wraps at the width the page has when it fits (a scrollbar that comes and goes would
 * change the heights it measures). Null when the body has no height (not laid out).
 */
export function measureSheet(body: HTMLElement): SheetMeasure | null {
  const overflow = body.style.overflowY;
  body.style.overflowY = 'hidden';
  try {
    const available = body.clientHeight;
    if (!available) return null;
    const style = getComputedStyle(body);
    const top = body.getBoundingClientRect().top;
    const last = body.lastElementChild?.getBoundingClientRect();
    const content = (last ? last.bottom - top + body.scrollTop : 0) + px(style.paddingBottom);
    const items = [...body.querySelectorAll<HTMLElement>('[data-fit-key]')].map((el) => ({ key: el.dataset.fitKey!, height: el.getBoundingClientRect().height }));
    let lists = 0;
    const gaps: Record<string, number> = {};
    for (const el of body.querySelectorAll<HTMLElement>('[data-fit="list"]')) {
      lists += el.getBoundingClientRect().height;
      gaps[el.dataset.fitList ?? ''] = px(getComputedStyle(el).rowGap);
    }
    const t = body.querySelector<HTMLElement>('[data-fit="toggle"]');
    let toggle: number | null = null;
    if (t) {
      const ts = getComputedStyle(t);
      toggle = t.getBoundingClientRect().height + px(ts.marginTop) + px(ts.marginBottom) + (t.parentElement ? px(getComputedStyle(t.parentElement).rowGap) : 0);
    }
    let skip = 0;
    for (const el of body.children) if ((el as HTMLElement).dataset?.fit === 'skip') skip += el.getBoundingClientRect().height + px(style.rowGap);
    return { available, width: body.clientWidth, content, items, lists, gaps, toggle, skip };
  } finally {
    body.style.overflowY = overflow;
  }
}
