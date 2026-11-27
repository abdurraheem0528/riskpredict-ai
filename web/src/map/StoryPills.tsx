// Score pills at the place the trouble is, one per story on screen (up to five): at the
// chokepoint for a chokepoint match ("34 Bab el-Mandeb"), else at the entity the story reaches
// ("30 Eindhoven"). The ports and route ends a story reaches only through a chokepoint get no
// pill. Same glass pill as the entity pills (map.css .mpill): the score chip in its level tint
// (grey for Low), then the short name; a blue ring on the selected story. Hover and selection
// are linked with the list through ./focus (by entity id: the row's entity and the others its
// story reaches). Not in the tab order: the list has the same action.
import { useEffect, useMemo, useRef } from 'react';
import { useMap } from './MapRoot';
import { getFocus, setHovered, setSelected, useFocus } from './focus';
import { setState } from './fstate';
import { pillOffset, pillPlace, placePills, type EntityProps, type PillRec } from './EntityLayers';
import { D2, later } from '../lib/motion';
import { levelOf, scoreText } from '../lib/format';
import type { LonLat } from './camera';
import type { Chokepoint } from './Chokepoints';

export type StoryPill = {
  /** the row's risk id */
  id: string;
  /** the row's entity: what hover, selection and a click on the pill pass on */
  focus: string;
  /** every entity the story reaches (hovering any of them lights the pill) */
  members: readonly string[];
  at: LonLat;
  score: number;
  label: string;
  /** set for a chokepoint pill: its plain label is hidden while the pill shows */
  chokepoint?: string;
  /** at a site: its label sits under the square, so the pill tries above it first */
  site?: boolean;
};

type StoryRow = {
  id: string;
  score: number;
  entity: { id: string };
  story: { entities: readonly { id: string }[] };
  where?: { match_type: string; match_value: string } | null;
};

/**
 * The pills for `rows` (the stories on screen, in order): the chokepoint's position for a
 * chokepoint match, otherwise the reached entity's placed position. A row with neither gets none.
 */
export function storyPills(
  rows: readonly StoryRow[],
  chokepoints: readonly Chokepoint[],
  placed: ReadonlyMap<string, { at: LonLat; props: Pick<EntityProps, 'city' | 'ports' | 'countries' | 'name' | 'kind'> }>,
): StoryPill[] {
  const out: StoryPill[] = [];
  for (const r of rows) {
    const members = [...new Set([r.entity.id, ...r.story.entities.map((e) => e.id)])];
    const base = { id: r.id, focus: r.entity.id, members, score: r.score };
    if (r.where?.match_type === 'chokepoint') {
      const c = chokepoints.find((x) => x.id === r.where!.match_value);
      if (c) {
        out.push({ ...base, at: c.at, label: c.name, chokepoint: c.id });
        continue;
      }
    }
    const e = placed.get(r.entity.id);
    if (e) out.push({ ...base, at: e.at, label: pillPlace(e.props), site: e.props.kind === 'site' });
  }
  return out;
}

export function StoryPills({ pills, onPick }: { pills: readonly StoryPill[]; onPick?: (entityId: string) => void }) {
  const { map, lib } = useMap();
  const recs = useRef(new Map<string, PillRec>());
  const focus = useFocus();
  const pickRef = useRef(onPick);
  useEffect(() => {
    pickRef.current = onPick;
  });
  const byId = useMemo(() => new Map(pills.map((p) => [p.id, p])), [pills]);
  const byIdRef = useRef(byId);
  useEffect(() => {
    byIdRef.current = byId;
  });

  // An entity carrying a story's pill hides its own map label underneath (as an entity pill does).
  const labelled = useRef(new Set<string>());
  useEffect(() => {
    if (!map) return;
    const now = new Set(pills.filter((p) => !p.chokepoint).map((p) => p.focus));
    for (const id of labelled.current) if (!now.has(id)) setState(map, 'ents', id, 'pill', 0);
    for (const id of now) setState(map, 'ents', id, 'pill', 1);
    labelled.current = now;
  }, [map, pills]);

  useEffect(() => {
    if (!map || !lib) return;
    for (const [id, r] of recs.current) {
      if (byId.has(id) || r.gone) continue;
      r.btn.classList.remove('show', 'force', 'on');
      r.gone = later(() => {
        r.m.remove();
        recs.current.delete(id);
      }, D2);
    }
    for (const p of pills) {
      let r = recs.current.get(p.id);
      if (!r) {
        const root = document.createElement('div');
        root.className = 'mk';
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.tabIndex = -1;
        btn.className = 'mpill';
        btn.setAttribute('aria-hidden', 'true');
        const pn = document.createElement('span');
        const pc = document.createElement('span');
        pc.className = 'pc';
        btn.append(pn, pc);
        root.append(btn);
        btn.addEventListener('mousemove', (e) => e.stopPropagation());
        btn.addEventListener('mouseenter', () => {
          const cur = byIdRef.current.get(p.id);
          if (cur) setHovered(cur.focus, 'map');
        });
        btn.addEventListener('mouseleave', () => {
          const cur = byIdRef.current.get(p.id);
          if (cur && getFocus().hovered === cur.focus) setHovered(null, 'map');
        });
        btn.addEventListener('click', (e) => {
          e.stopPropagation();
          const cur = byIdRef.current.get(p.id);
          if (!cur) return;
          setSelected(cur.focus, 'map');
          pickRef.current?.(cur.focus);
        });
        const m = new lib.Marker({ element: root, anchor: 'center', offset: pillOffset(0, 120, 30), opacityWhenCovered: '0', subpixelPositioning: true }).setLngLat(p.at).addTo(map);
        r = { m, root, btn, at: p.at, gone: null, side: -1 };
        recs.current.set(p.id, r);
        const rec = r;
        requestAnimationFrame(() => {
          if (!rec.gone) rec.btn.classList.add('show');
        });
      } else if (r.gone) {
        r.gone();
        r.gone = null;
        r.btn.classList.add('show');
      }
      r.sides = p.site ? [2, 0, 1, 3] : undefined;
      if (r.at[0] !== p.at[0] || r.at[1] !== p.at[1]) {
        r.m.setLngLat(p.at);
        r.at = p.at;
      }
      const [pn, pc] = r.btn.children as unknown as [HTMLSpanElement, HTMLSpanElement];
      pn.className = `pn l-${levelOf(p.score)}`;
      pn.textContent = scoreText(p.score);
      pc.textContent = p.label;
      const lit = (id: string | null) => !!id && p.members.includes(id);
      const force = lit(focus.hovered) || lit(focus.selected);
      r.btn.classList.toggle('force', force);
      r.btn.classList.toggle('on', lit(focus.selected));
      r.root.style.zIndex = force ? '6' : '4';
    }
    placePills(map, recs.current);
  }, [map, lib, pills, byId, focus.hovered, focus.selected]);

  useEffect(() => {
    if (!map) return;
    let t: ReturnType<typeof setTimeout> | null = null;
    const onEnd = (e: unknown) => {
      if (t) clearTimeout(t);
      t = setTimeout(() => placePills(map, recs.current), (e as { rpSpin?: boolean })?.rpSpin ? 500 : 0);
    };
    map.on('moveend', onEnd);
    map.on('resize', onEnd);
    return () => {
      if (t) clearTimeout(t);
      map.off('moveend', onEnd);
      map.off('resize', onEnd);
    };
  }, [map]);

  useEffect(() => {
    const rs = recs.current;
    return () => {
      for (const r of rs.values()) {
        r.gone?.();
        r.m.remove();
      }
      rs.clear();
    };
  }, []);
  return null;
}
