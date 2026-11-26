// The map controls (System-Nav): Globe / Flat, zoom out, zoom in, fit. One glass capsule at
// the bottom right of the map, 44 px targets; hidden on phones (pinch and drag instead).
import { useMap } from './MapRoot';
import { fitTo, sheetPadding } from './camera';
import { setProjection, useProjection } from './projection';
import { WORLD } from './MapRoot';
import { Icon } from '../ui/Icon';

type Box = [[number, number], [number, number]];

export function MapControls({ fit, fitLabel = 'Fit everything', side = 'left', sheet = 344 }: { fit?: Box | null; fitLabel?: string; side?: 'left' | 'right'; sheet?: number }) {
  const { map } = useMap();
  const proj = useProjection();
  if (!map) return null;
  return (
    <div className="mctl glass" role="group" aria-label="Map controls" style={side === 'right' ? { right: 20 + sheet + 16 } : undefined}>
      <div className="mseg" role="group" aria-label="Projection">
        <button type="button" className="msb" aria-pressed={proj === 'globe'} onClick={() => setProjection('globe')}>Globe</button>
        <button type="button" className="msb" aria-pressed={proj === 'flat'} onClick={() => setProjection('flat')}>Flat</button>
      </div>
      <span className="mvr" aria-hidden="true" />
      <button type="button" className="msb ic" aria-label="Zoom out" title="Zoom out" onClick={() => map.zoomOut()}>
        <Icon name="minus" size={18} stroke={1.75} />
      </button>
      <button type="button" className="msb ic" aria-label="Zoom in" title="Zoom in" onClick={() => map.zoomIn()}>
        <Icon name="add" size={18} stroke={1.75} />
      </button>
      <button type="button" className="msb ic" aria-label={fitLabel} title={fitLabel} onClick={() => fitTo(map, fit ?? WORLD, sheetPadding(side, sheet), fit ? 5 : 3)}>
        <Icon name="fit" size={18} />
      </button>
    </div>
  );
}
