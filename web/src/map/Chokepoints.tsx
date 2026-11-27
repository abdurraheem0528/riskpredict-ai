// Chokepoints from /v1/map/disruptions: an 8 px diamond in the signal colour; a watched or
// disrupted one in the Medium (or High) fill with a slow ring and its name and the number
// of drawn lanes through it. Diamonds show from about zoom 1.2 on the globe (class `zd`),
// labels from the pill zoom (`zp`); the ring stops under reduced motion and in a hidden tab.
// A chokepoint that carries a story's score pill (StoryPills) shows no label of its own.
import { useEffect, useMemo, useRef } from 'react';
import type { Marker } from 'maplibre-gl';
import { useMap } from './MapRoot';
import { useDisruptions } from '../api/queries';

export type Chokepoint = { id: string; name: string; at: [number, number]; status: 'normal' | 'watch' | 'disrupted' | string };

/** The label of an active chokepoint: "Bab el-Mandeb · 8 lanes" (no count when none pass it). */
export function chokeLabel(name: string, lanes: number | undefined): string {
  if (!lanes) return name;
  return `${name} · ${lanes} ${lanes === 1 ? 'lane' : 'lanes'}`;
}

export function useChokepoints(): Chokepoint[] {
  const d = useDisruptions();
  return useMemo(
    () =>
      (d.data?.features ?? [])
        .filter((f) => f.properties.kind === 'chokepoint' && f.geometry)
        .map((f) => ({ id: String(f.id).replace(/^chokepoint:/, ''), name: f.properties.name, at: f.geometry!.coordinates as [number, number], status: f.properties.status })),
    [d.data],
  );
}

export function Chokepoints({ laneCounts = {}, only = null, unlabelled = null }: { laneCounts?: Record<string, number>; only?: Set<string> | null; unlabelled?: ReadonlySet<string> | null }) {
  const { map, lib } = useMap();
  const all = useChokepoints();
  const list = useMemo(() => (only ? all.filter((c) => only.has(c.id)) : all), [all, only]);
  const recs = useRef(new Map<string, { m: Marker; el: HTMLSpanElement; lab: HTMLSpanElement | null; key: string }>());

  useEffect(() => {
    if (!map || !lib) return;
    const live = new Set(list.map((c) => c.id));
    for (const [id, r] of recs.current) if (!live.has(id)) {
      r.m.remove();
      recs.current.delete(id);
    }
    for (const c of list) {
      const active = c.status === 'watch' || c.status === 'disrupted';
      const text = active && !unlabelled?.has(c.id) ? chokeLabel(c.name, laneCounts[c.id]) : '';
      const key = `${c.status}|${text}`;
      let r = recs.current.get(c.id);
      if (r && r.key === key) continue;
      r?.m.remove();
      const root = document.createElement('div');
      root.className = 'mk';
      root.setAttribute('aria-hidden', 'true');
      const el = document.createElement('span');
      el.className = `mcp${active ? ` cp-on${c.status === 'disrupted' ? ' cp-hi' : ''}` : ''}`;
      const dia = document.createElement('i');
      dia.className = 'dia';
      el.append(dia);
      let lab: HTMLSpanElement | null = null;
      if (active) {
        const ring = document.createElement('i');
        ring.className = 'ring';
        el.append(ring);
        if (text) {
          lab = document.createElement('span');
          lab.className = 'clab';
          lab.textContent = text;
          el.append(lab);
        }
      }
      root.append(el);
      root.style.zIndex = '1';
      const m = new lib.Marker({ element: root, anchor: 'center', opacityWhenCovered: '0', subpixelPositioning: true }).setLngLat(c.at).addTo(map);
      r = { m, el, lab, key };
      recs.current.set(c.id, r);
    }
  }, [map, lib, list, laneCounts, unlabelled]);

  useEffect(() => {
    const rs = recs.current;
    return () => {
      for (const r of rs.values()) r.m.remove();
      rs.clear();
    };
  }, []);
  return null;
}
