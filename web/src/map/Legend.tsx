// The map legend (small, bottom left of the free map area, beside the sheet): the level
// pills, then the map's marks, then whatever the screen adds (feed status). Hidden on phones.
import type { ReactNode } from 'react';
import { LEVELS, type Level } from '../lib/format';

const ORDER: Level[] = ['low', 'med', 'high', 'crit'];

export function Legend({ sheet = 344, side = 'left', marks = ['lane', 'choke', 'site'], children }: {
  sheet?: number;
  side?: 'left' | 'right';
  marks?: ('lane' | 'choke' | 'site' | 'signal' | 'shock')[];
  children?: ReactNode;
}) {
  // Beside the sheet: its edge (20) + its width + a 16 px gap; clear of the map controls (≈ 270 px at the right).
  const left = side === 'left' ? 20 + sheet + 16 : 20;
  const right = (side === 'right' ? 20 + sheet + 16 : 20) + 270;
  const style = { left, maxWidth: `calc(100% - ${left + right}px)` };
  return (
    <div className="legend mlegend glass" style={style} aria-label="Legend">
      <span className="mlv" aria-label="Levels">
        {ORDER.map((l) => (
          <span key={l} className={`mlp l-${l}`}>{LEVELS[l].name}</span>
        ))}
      </span>
      {marks.includes('shock') && <span className="k mk-shock"><i className="sw shock" aria-hidden="true" />Where it happens</span>}
      {marks.includes('lane') && <span className="k mk-lane"><i className="sw lane" aria-hidden="true" />Sea lane</span>}
      {marks.includes('choke') && <span className="k mk-choke"><i className="sw choke" aria-hidden="true" />Chokepoint</span>}
      {marks.includes('site') && <span className="k mk-site"><i className="sw site" aria-hidden="true" />Site</span>}
      {marks.includes('signal') && <span className="k mk-signal"><i className="sw sig" aria-hidden="true" />Signal</span>}
      {children && <span className="vr mvr2" aria-hidden="true" />}
      {children}
    </div>
  );
}
