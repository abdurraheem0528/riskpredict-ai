// How many steps and news items the risk page shows so that it fits its sheet (src/lib/fit.ts).
import { describe, expect, it } from 'vitest';
import { fitCounts, fitNeed, measureSheet, type FitInput } from '../../src/lib/fit';

// Three 3-line steps (18 px lines, 6 px apart) and two 2-line news items (40 px, 8 px apart),
// under 400 px of everything else; the toggle adds 28 px when it shows.
const base: FitInput = { available: 700, fixed: 400, steps: [54, 54, 54], stepGap: 6, news: [40, 40], newsGap: 8, toggle: 28 };
const at = (available: number, more: Partial<FitInput> = {}) => fitCounts({ ...base, ...more, available });

describe('fitting the risk page', () => {
  it('shows everything when everything fits', () => {
    expect(fitNeed(base, 3, 2)).toBe(400 + 174 + 88);
    expect(at(700)).toEqual({ steps: 3, news: 2, fits: true });
    expect(at(662)).toEqual({ steps: 3, news: 2, fits: true }); // to the pixel
  });
  it('holds back the last step when the page is over by less than one step', () => {
    // 1 px over; two steps and the toggle need 630
    expect(at(661)).toEqual({ steps: 2, news: 2, fits: true });
    expect(at(630)).toEqual({ steps: 2, news: 2, fits: true });
  });
  it('holds back steps from the last, down to one', () => {
    expect(at(629)).toEqual({ steps: 1, news: 2, fits: true });
    expect(at(570)).toEqual({ steps: 1, news: 2, fits: true });
  });
  it('then holds back a news item, once the steps are down to one', () => {
    expect(at(569)).toEqual({ steps: 1, news: 1, fits: true });
    expect(at(522)).toEqual({ steps: 1, news: 1, fits: true });
  });
  it('shows one step and one news item when even those do not fit, and says so', () => {
    expect(at(521)).toEqual({ steps: 1, news: 1, fits: false });
    expect(at(0)).toEqual({ steps: 1, news: 1, fits: false });
  });
  it('never shows none of what there is', () => {
    expect(at(0, { steps: [54], news: [40] })).toEqual({ steps: 1, news: 1, fits: false });
    expect(at(0, { steps: [54, 54], news: [40] })).toEqual({ steps: 1, news: 1, fits: false });
  });
  it('counts the toggle only when steps are held back', () => {
    // all three steps and no toggle need 662; with the toggle, two steps would need 630
    expect(at(662, { toggle: 100 })).toEqual({ steps: 3, news: 2, fits: true });
    expect(at(661, { toggle: 100 })).toEqual({ steps: 1, news: 2, fits: true }); // 400 + 54 + 88 + 100 = 642
  });
  it('works with no advice or no news', () => {
    expect(at(500, { steps: [] })).toEqual({ steps: 0, news: 2, fits: true });
    expect(at(450, { steps: [] })).toEqual({ steps: 0, news: 1, fits: true });
    expect(at(545, { news: [] })).toEqual({ steps: 2, news: 0, fits: true }); // 400 + 114 + 28
    expect(at(0, { steps: [], news: [] })).toEqual({ steps: 0, news: 0, fits: false });
  });
  it('holds back any number of steps, from the last', () => {
    expect(at(700, { steps: [36, 36, 36, 36, 36] })).toEqual({ steps: 5, news: 2, fits: true }); // 400 + 204 + 88
    expect(at(691, { steps: [36, 36, 36, 36, 36] })).toEqual({ steps: 4, news: 2, fits: true }); // 400 + 162 + 88 + 28
  });
  it('reads nothing from a sheet body that has no height, and leaves its overflow as it was', () => {
    const body = document.createElement('div');
    body.style.overflowY = 'auto';
    expect(measureSheet(body)).toBeNull(); // jsdom lays nothing out
    expect(body.style.overflowY).toBe('auto');
  });
});
