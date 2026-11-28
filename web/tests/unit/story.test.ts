// The words and the scale of the redesigned risk cards: place phrases, the reach line, the
// trend, the receipt, and where a score sits on the 0–100 bands. Test fixtures only.
import { describe, expect, it } from 'vitest';
import { countryProse, levelOf, placePhrase, type EntityKind, type Where } from '../../src/lib/format';
import { headlineText, impactReason, latestReports, latestRun, levelsLine, likelyReason, reachLine, receipt, shownReports, trendText, whySentence } from '../../src/lib/story';
import { scalePlace } from '../../src/ui/ScoreScale';
import { capeCase, hormuzCase, riskDetail, risks, worstCase } from '../fixtures/data';

const w = (match_type: string, match_value: string, label: string): Where => ({ match_type, match_value, label });
const names: Record<string, string> = { NLD: 'Netherlands', TWN: 'Taiwan', USA: 'United States of America', MHL: 'Marshall Is.', COD: 'Dem. Rep. Congo', MYS: 'Malaysia' };
const country = (c: string) => names[c] ?? c;

describe('place phrases', () => {
  it('say what is happening and where, by category and match type', () => {
    expect(placePhrase('logistics', w('chokepoint', 'bab_el_mandeb', 'Bab el-Mandeb'))).toBe('Shipping through Bab el-Mandeb');
    expect(placePhrase('logistics', w('chokepoint', 'suez', 'Suez Canal'))).toBe('Shipping through the Suez Canal');
    expect(placePhrase('logistics', w('port', 'NLRTM', 'Rotterdam'))).toBe('Shipping at Rotterdam');
    expect(placePhrase('logistics', w('country', 'MYS', 'Malaysia'), { country })).toBe('Shipping in Malaysia');
    expect(placePhrase('geopolitical', w('chokepoint', 'taiwan_strait', 'Taiwan Strait'))).toBe('Conflict near the Taiwan Strait');
    expect(placePhrase('geopolitical', w('country', 'TWN', 'Taiwan'), { country })).toBe('Conflict in Taiwan');
    expect(placePhrase('sanctions', w('country', 'MYS', 'Malaysia'), { country })).toBe('Sanctions in Malaysia');
    expect(placePhrase('sanctions', w('sanctions_name', 'NORTHWIND HOLDINGS', 'NORTHWIND HOLDINGS'))).toBe('NORTHWIND HOLDINGS on a sanctions list');
    expect(placePhrase('labour', w('country', 'TWN', 'Taiwan'), { country })).toBe('Labour unrest in Taiwan');
    expect(placePhrase('cyber', w('country', 'TWN', 'Taiwan'), { country })).toBe('Cyber threats in Taiwan');
    expect(placePhrase('cyber', w('software', 'PyPI:requests', 'requests'))).toBe('A flaw in requests');
    expect(placePhrase('natural_hazard', w('geo_radius', '84 km', 'Hsinchu'), { city: 'Hsinchu', leadType: 'hazard.earthquake' })).toBe('Earthquake near Hsinchu');
    expect(placePhrase('natural_hazard', w('country', 'TWN', 'Taiwan'), { leadType: 'hazard.tropical_cyclone', country })).toBe('Tropical cyclone in Taiwan');
    expect(placePhrase('financial', w('currency', 'EUR/TWD', 'EUR/TWD'))).toBe('TWD swings');
    expect(placePhrase('other', w('country', 'TWN', 'Taiwan'), { country })).toBe('Other in Taiwan');
  });

  it('give countries "the" where English needs it, and write out the map’s short names', () => {
    expect(placePhrase('trade_policy', w('country', 'NLD', 'the Netherlands'), { country })).toBe('Trade policy in the Netherlands');
    // without the gazetteer, the API's label (already prose) is used
    expect(placePhrase('trade_policy', w('country', 'NLD', 'the Netherlands'))).toBe('Trade policy in the Netherlands');
    expect(placePhrase('trade_policy', w('country', 'USA', 'the United States'), { country })).toBe('Trade policy in the United States');
    expect(countryProse('MHL', 'Marshall Is.')).toBe('the Marshall Islands');
    expect(countryProse('COD', 'Dem. Rep. Congo')).toBe('the Democratic Republic of the Congo');
    expect(countryProse('PHL', 'Philippines')).toBe('the Philippines');
    expect(countryProse('TWN', 'Taiwan')).toBe('Taiwan');
  });

  it('fall back to the risk title when there is no place', () => {
    expect(placePhrase('cyber', null, { fallback: 'Exploited vulnerability in edge os' })).toBe('Exploited vulnerability in edge os');
  });
});

describe('the reach line', () => {
  const base = risks[0];
  it('names one entity', () => {
    expect(reachLine(risks.find((r) => r.story.entity_count === 1 && r.entity.name === 'Penang Semicon Assembly')!)).toBe('Penang Semicon Assembly');
  });
  it('counts several by kind, with their cities', () => {
    expect(reachLine(risks[1])).toBe('2 sites, in Eindhoven and Rotterdam');
  });
  it('says what they do at a chokepoint', () => {
    expect(reachLine(base)).toBe('8 routes and 7 suppliers ship through it');
  });
  it('leaves cities out when one is unknown or not every entity is listed', () => {
    const r = { ...risks[1], story: { ...risks[1].story, entities: [risks[1].story.entities[0], { ...risks[1].story.entities[1], city: null }] } };
    expect(reachLine(r)).toBe('2 sites');
    const mixed = { ...risks[1], story: { ...risks[1].story, kind_counts: { site: 1, supplier: 2 }, entity_count: 3 } };
    expect(reachLine(mixed)).toBe('2 suppliers and 1 site');
  });
});

describe('why it reaches you', () => {
  type E = { name: string; kind: EntityKind; criticality?: 'low' | 'medium' | 'high' | 'critical' | null };
  /** A risk page whose story reaches `list` (the first is the risk's own unless `own` says), at `where`. */
  const why = (where: Where, list: E[], o: { total?: number; kinds?: Record<string, number>; own?: number } = {}) => {
    const base = riskDetail(risks[1]);
    const ents = list.map((e, i) => ({ id: `e${i}`, name: e.name, kind: e.kind, city: null, criticality: e.criticality ?? null }));
    const kinds: Record<string, number> = o.kinds ?? {};
    if (!o.kinds) for (const e of list) kinds[e.kind] = (kinds[e.kind] ?? 0) + 1;
    const me = ents[o.own ?? 0];
    return whySentence({
      ...base,
      entity: { id: me.id, kind: me.kind, name: me.name, city: null },
      where: { match_type: where.match_type as 'country', match_value: where.match_value, label: where.label },
      story: { ...base.story, entity_count: o.total ?? list.length, risk_count: o.total ?? list.length, kind_counts: kinds, entities: ents.slice(0, 10) },
    }, country);
  };
  const NL = w('country', 'NLD', 'the Netherlands');
  const COD = w('country', 'COD', 'the Democratic Republic of the Congo');
  const RTM = w('port', 'NLRTM', 'Rotterdam');
  const BAB = w('chokepoint', 'bab_el_mandeb', 'Bab el-Mandeb');
  const site = (name: string, criticality: E['criticality'] = null): E => ({ name, kind: 'site', criticality });
  const supplier = (name: string, criticality: E['criticality'] = null): E => ({ name, kind: 'supplier', criticality });
  const route = (name: string): E => ({ name, kind: 'route' });

  it('names the entities, their kind when critical, and the place', () => {
    expect(whySentence(riskDetail(risks[1]), country)).toEqual({
      text: 'Atlas Eindhoven Assembly Plant, a critical site, and Atlas Rotterdam Distribution Centre are in the Netherlands.',
      named: true,
    });
  });
  it('names one or two in a country, and closes a final critical aside with a comma before the verb', () => {
    expect(why(NL, [site('Atlas Rotterdam Distribution Centre')])).toEqual({ text: 'Atlas Rotterdam Distribution Centre is in the Netherlands.', named: true });
    expect(why(NL, [site('Atlas Eindhoven Assembly Plant', 'critical')]).text).toBe('Atlas Eindhoven Assembly Plant, a critical site, is in the Netherlands.');
    expect(why(NL, [site('Atlas Eindhoven Assembly Plant', 'critical'), site('Atlas Rotterdam Distribution Centre', 'critical')]).text).toBe(
      'Atlas Eindhoven Assembly Plant, a critical site, and Atlas Rotterdam Distribution Centre, a critical site, are in the Netherlands.',
    );
    expect(why(NL, [site('Atlas Rotterdam Distribution Centre'), supplier('Rhein Sensorik GmbH', 'critical')]).text).toBe(
      'Atlas Rotterdam Distribution Centre and Rhein Sensorik GmbH, a critical supplier, are in the Netherlands.',
    );
  });
  it('names only the risk’s own entity from three on, and counts the others: "other sites" when all are sites', () => {
    expect(why(COD, [site('Kolwezi Refinery', 'critical'), site('Lubumbashi Works', 'critical'), site('Likasi Store', 'critical')])).toEqual({
      text: 'Kolwezi Refinery, a critical site, and 2 other sites are in the Democratic Republic of the Congo.',
      named: true,
    });
    // the risk's own entity, wherever the list puts it
    expect(why(NL, [site('Atlas Eindhoven Assembly Plant', 'critical'), site('Atlas Rotterdam Distribution Centre'), site('Atlas Venlo Warehouse')], { own: 1 }).text).toBe(
      'Atlas Rotterdam Distribution Centre and 2 other sites are in the Netherlands.',
    );
  });
  it('says "others" when their kinds differ', () => {
    const list = [supplier('Rhein Sensorik GmbH', 'critical'), site('Atlas Eindhoven Assembly Plant'), site('Atlas Rotterdam Distribution Centre'), supplier('Delft Optics')];
    expect(why(NL, list)).toEqual({ text: 'Rhein Sensorik GmbH, a critical supplier, and 3 others are in the Netherlands.', named: true });
  });
  it('counts every entity the story reaches, not only the ten listed', () => {
    const list = Array.from({ length: 10 }, (_, i) => supplier(`Supplier ${i + 1}`));
    expect(why(NL, list, { total: 14, kinds: { supplier: 14 } }).text).toBe('Supplier 1 and 13 other suppliers are in the Netherlands.');
    expect(why(NL, list, { total: 14, kinds: { supplier: 12, site: 2 } }).text).toBe('Supplier 1 and 13 others are in the Netherlands.');
  });
  it('says what uses a port, named or counted', () => {
    expect(why(RTM, [site('Atlas Rotterdam Distribution Centre')]).text).toBe('Atlas Rotterdam Distribution Centre uses the port of Rotterdam.');
    expect(why(RTM, [site('Atlas Rotterdam Distribution Centre', 'critical'), supplier('Delft Optics')]).text).toBe(
      'Atlas Rotterdam Distribution Centre, a critical site, and Delft Optics use the port of Rotterdam.',
    );
    expect(why(RTM, [site('Atlas Rotterdam Distribution Centre', 'critical'), site('Atlas Venlo Warehouse'), site('Atlas Tilburg Depot')]).text).toBe(
      'Atlas Rotterdam Distribution Centre, a critical site, and 2 other sites use the port of Rotterdam.',
    );
    expect(why(RTM, [site('Atlas Rotterdam Distribution Centre'), supplier('Delft Optics'), route('Busan to Rotterdam')]).text).toBe(
      'Atlas Rotterdam Distribution Centre and 2 others use the port of Rotterdam.',
    );
  });
  it('says a route runs through a country, and falls back when routes and other kinds mix', () => {
    expect(why(NL, [route('Busan to Rotterdam')]).text).toBe('Busan to Rotterdam runs through the Netherlands.');
    expect(why(NL, [route('Busan to Rotterdam'), route('Osaka to Rotterdam')]).text).toBe('Busan to Rotterdam and Osaka to Rotterdam run through the Netherlands.');
    expect(why(NL, [route('Busan to Rotterdam'), route('Osaka to Rotterdam'), route('Penang to Rotterdam')]).text).toBe(
      'Busan to Rotterdam and 2 other routes run through the Netherlands.',
    );
    const base = riskDetail(risks[1]);
    expect(why(NL, [route('Busan to Rotterdam'), site('Atlas Rotterdam Distribution Centre')])).toEqual({ text: base.explanation, named: false });
    expect(why(NL, [route('Busan to Rotterdam'), site('Atlas Rotterdam Distribution Centre'), site('Atlas Venlo Warehouse')])).toEqual({ text: base.explanation, named: false });
    // counted beyond the ten listed: a route among the others that are not listed still mixes
    const sites = Array.from({ length: 10 }, (_, i) => site(`Site ${i + 1}`));
    expect(why(NL, sites, { total: 11, kinds: { site: 10, route: 1 } })).toEqual({ text: base.explanation, named: false });
  });
  it('counts what ships through a chokepoint, naming one or two only', () => {
    // counted, not named: it still accounts for every one, so the page lists no others
    expect(whySentence(riskDetail(risks[0]), country)).toEqual({ text: '8 of your routes and 7 of your suppliers ship through Bab el-Mandeb.', named: true });
    expect(why(BAB, [route('Penang to Rotterdam')]).text).toBe('Penang to Rotterdam ships through Bab el-Mandeb.');
    expect(why(BAB, [supplier('Chennai Connector Works', 'critical'), route('Chennai to Rotterdam')]).text).toBe(
      'Chennai Connector Works, a critical supplier, and Chennai to Rotterdam ship through Bab el-Mandeb.',
    );
    expect(why(BAB, [route('Penang to Rotterdam'), route('Chennai to Rotterdam'), route('Singapore to Antwerp')])).toEqual({
      text: '3 of your routes ship through Bab el-Mandeb.',
      named: true,
    });
    const list = Array.from({ length: 10 }, (_, i) => route(`Route ${i + 1}`));
    expect(why(BAB, list, { total: 12, kinds: { route: 9, supplier: 3 } }).text).toBe('9 of your routes and 3 of your suppliers ship through Bab el-Mandeb.');
  });
  it('falls back to the engine’s explanation', () => {
    const d = riskDetail(risks.find((r) => r.where?.match_type === 'geo_radius')!);
    expect(whySentence(d, country)).toEqual({ text: d.explanation, named: false });
  });
});

describe('the trend', () => {
  const now = Date.parse('2026-11-27T12:00:00Z');
  const at = (hoursAgo: number, score: number) => ({ recorded_at: new Date(now - hoursAgo * 3600_000).toISOString(), score });
  it('compares with the latest point at least a day old', () => {
    expect(trendText([at(72, 20), at(30, 27), at(5, 30)], 30, now)).toEqual({ dir: 'up', text: 'Up 3 since yesterday' });
    expect(trendText([at(72, 40), at(26, 36), at(2, 34)], 34, now)).toEqual({ dir: 'down', text: 'Down 2 since yesterday' });
  });
  it('compares with the first point of a risk younger than a day', () => {
    expect(trendText([at(10, 27), at(2, 30)], 30, now)).toEqual({ dir: 'up', text: 'Up 3 since it opened' });
  });
  it('is hidden under one point', () => {
    expect(trendText([at(30, 29.4), at(2, 30)], 30, now)).toBeNull();
    expect(trendText([at(10, 30)], 30.6, now)).toBeNull();
    expect(trendText([], 30, now)).toBeNull();
  });
  it('reads the fixture as the live risk did', () => {
    const d = riskDetail(risks[1]);
    expect(trendText(d.history, d.score)?.text).toBe('Up 3 since yesterday');
  });
});

describe('the receipt', () => {
  // Report dates pinned, so the span does not depend on the time of day the test runs.
  const on = (d: ReturnType<typeof riskDetail>, dates: string[]) => ({
    ...d, evidence: d.evidence.map((e, i) => ({ ...e, snapshot: { ...e.snapshot, published_at: dates[i] ?? e.snapshot.published_at } })),
  });
  it('makes likely × impact the score, with a reason for each', () => {
    const d = on(riskDetail(risks[1]), ['2026-11-27T08:00:00', '2026-11-27T02:00:00', '2026-11-26T16:00:00', '2026-11-26T09:00:00']);
    const [likely, impact] = receipt(d, country);
    expect(likely).toMatchObject({ label: 'Likely', value: '50%' });
    expect(impact).toEqual({ label: 'Impact', value: '60%', reason: 'Trade policy, at a site you rated critical' });
    expect(likely.reason).toBe('4 reports in 2 days name the Netherlands');
    expect(0.5 * 0.6 * 100).toBe(d.score);
  });
  it('says the chokepoint, the route and the rating', () => {
    const d = on(riskDetail(risks[0]), ['2026-11-27T07:00:00', '2026-11-27T01:00:00']);
    expect(likelyReason(d, country)).toBe('2 reports on 27 Nov mention Bab el-Mandeb');
    expect(impactReason(d)).toBe('Shipping, on a route you rated high');
  });
  it('says what moved for a market move, and the share of spend when it is known', () => {
    const d = riskDetail(risks.find((r) => r.category === 'financial')!);
    expect(likelyReason(d, country)).toBe('An unusual move in EUR/TWD');
    const share = { ...d, factors: { ...d.factors, impact: { ...d.factors.impact, exposure_share: 0.15 } } };
    expect(impactReason(share)).toBe('Financial, on 15% of your spend');
    expect(impactReason({ ...d, factors: { ...d.factors, impact: { ...d.factors.impact, criticality: null } } })).toBe('Financial, with a supplier you have not rated');
  });
  it('counts only what the latest run linked', () => {
    const d = riskDetail(risks[1]);
    const older = { ...d.evidence[0], evidence_key: 'event:gdelt:old', last_linked_at: '2026-11-01T00:00:00Z' };
    expect(latestRun({ ...d, evidence: [...d.evidence, older] })).toHaveLength(4);
  });
});

describe('levels', () => {
  it('say how many of each, highest first', () => {
    expect(levelsLine([34, 30, 24], (s) => ({ low: 'Low', med: 'Medium', high: 'High', crit: 'Critical' })[levelOf(s)])).toBe('All are Low');
    expect(levelsLine([70, 65, 20, 10], (s) => ({ low: 'Low', med: 'Medium', high: 'High', crit: 'Critical' })[levelOf(s)])).toBe('2 are High, 2 Low');
    expect(levelsLine([85, 20], (s) => ({ low: 'Low', med: 'Medium', high: 'High', crit: 'Critical' })[levelOf(s)])).toBe('1 is Critical, 1 Low');
    expect(levelsLine([45], () => 'Medium')).toBe('It is Medium');
  });
});

describe('the score scale', () => {
  it.each([
    [0, 'low', 0], [39, 'low', 0], [40, 'med', 1], [59, 'med', 1],
    [60, 'high', 2], [79, 'high', 2], [80, 'crit', 3], [100, 'crit', 3],
  ] as const)('%i is %s, in band %i', (score, level, band) => {
    const p = scalePlace(score);
    expect(p.level).toBe(level);
    expect(levelOf(score)).toBe(level);
    expect(p.band).toBe(band);
    // the bar's width less the three 2 px gaps, in proportion, plus a gap per boundary passed
    expect(p.left).toBe(`calc((100% - 6px) * ${score / 100} + ${2 * band}px)`);
  });
  it('keeps scores on the bar', () => {
    expect(scalePlace(-5).at).toBe(0);
    expect(scalePlace(130).at).toBe(1);
  });
});

describe('the news on the risk page', () => {
  const rep = (title: string, cut = false, headline: string | undefined = title) => ({ headline, cut, snapshot: { title } });
  it('shows the two strongest whole headlines, then fills with the strongest cut ones', () => {
    const [c1, a, c2, b, c3] = [rep('Cut one: \'We Will', true), rep('A'), rep('Cut two…', true), rep('B'), rep('Cut three...', true)];
    expect(shownReports([c1, a, c2, b, c3])).toEqual([a, b]);
    expect(shownReports([c1, a, c2, c3])).toEqual([a, c1]); // one whole: the strongest cut one fills
    expect(shownReports([c1, c2])).toEqual([c1, c2]);
    expect(shownReports([a])).toEqual([a]);
    expect(shownReports([])).toEqual([]);
  });
  it('the live Netherlands page: the cut ASML headline is not among the first two', () => {
    const news = latestReports(riskDetail(risks[1]));
    expect(news[0].cut).toBe(true);
    expect(shownReports(news).map(headlineText)).toEqual([
      'Dutch Government Lobbies Against US Chip Export Bill',
      'Dutch cabinet weighs wider licence rules for chipmaking tool exports',
    ]);
  });
  it('ends a cut headline with "…", wherever it shows', () => {
    expect(headlineText(rep("ASML … Export Curbs: 'We Will", true))).toBe("ASML … Export Curbs: 'We Will…");
    expect(headlineText(rep('Netherlands weighs export licences for…', true))).toBe('Netherlands weighs export licences for…');
    expect(headlineText(rep('Netherlands weighs export licences for ...', true))).toBe('Netherlands weighs export licences for…');
    expect(headlineText(rep('Whole headline', false))).toBe('Whole headline');
  });
  it('shows the API’s headline, else the stored title', () => {
    expect(headlineText(rep('Houthis strike ships - Example Times', false, 'Houthis strike ships'))).toBe('Houthis strike ships');
    expect(headlineText(rep('Stored title', false, undefined))).toBe('Stored title');
    expect(headlineText(rep('Stored title', false, ''))).toBe('Stored title');
  });
  it('the worst-case fixture is at the limits it claims', () => {
    expect(whySentence(worstCase, country)).toEqual({
      text: 'Kolwezi Northern Copper and Cobalt Refining Complex, a critical site, and 2 other sites are in the Democratic Republic of the Congo.',
      named: true,
    });
    const steps = worstCase.advice!.text.split('\n');
    expect(steps.map((x) => x.split(/\s+/).length)).toEqual([20, 20, 20]);
    expect(latestReports(worstCase)).toHaveLength(3);
  });
  it('the pages near the limits are what they claim: chokepoint stories with three steps of at most 20 words', () => {
    for (const [d, place, at] of [[hormuzCase, 'Shipping through the Strait of Hormuz', 'the Strait of Hormuz'], [capeCase, 'Shipping through the Cape of Good Hope', 'the Cape of Good Hope']] as const) {
      expect(placePhrase(d.category, d.where as Where)).toBe(place);
      expect(whySentence(d, country)).toEqual({ text: `3 of your routes and 2 of your suppliers ship through ${at}.`, named: true });
      const steps = d.advice!.text.split('\n');
      expect(steps).toHaveLength(3);
      for (const step of steps) expect(step.split(/\s+/).length, step).toBeLessThanOrEqual(20);
      expect(latestReports(d)).toHaveLength(2);
    }
  });
});
