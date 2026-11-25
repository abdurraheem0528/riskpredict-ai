// The bottom sheet's handle on phones: tap to lower the sheet and see more map.
import { useState } from 'react';

export function Grab() {
  const [low, setLow] = useState(false);
  return (
    <button
      type="button"
      className="grab"
      aria-label={low ? 'Raise the sheet' : 'Lower the sheet to see more map'}
      aria-expanded={!low}
      onClick={(e) => {
        const sheet = e.currentTarget.closest('.sheet');
        sheet?.classList.toggle('min', !low);
        if (sheet && !low) sheet.scrollTop = 0;
        setLow(!low);
        // map labels re-place themselves around the sheet's new height
        window.setTimeout(() => window.dispatchEvent(new Event('rp:layout')), 250);
      }}
    />
  );
}
