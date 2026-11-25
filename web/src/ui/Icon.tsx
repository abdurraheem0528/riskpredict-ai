// The RiskPredict icon set (design/project/System-Icons.dc.html), paths copied as drawn:
// 24 grid, 1.5 pen at 24, 1.25 at 20 and 1 at 16 would thin them further; the screens use
// the strokes their own SVGs use, which is what `stroke` overrides.
import type { ReactElement, SVGProps } from 'react';

const dot = { fill: 'currentColor', stroke: 'none' } as const;

const paths = {
  overview: (<><rect x="3.5" y="3.5" width="7" height="7" rx="2" /><rect x="13.5" y="3.5" width="7" height="7" rx="2" /><rect x="3.5" y="13.5" width="7" height="7" rx="2" /><rect x="13.5" y="13.5" width="7" height="7" rx="2" /></>),
  signals: (<><circle cx="12" cy="12" r="1.75" {...dot} /><path d="M8.1 15.9a5.5 5.5 0 0 1 0-7.8M15.9 8.1a5.5 5.5 0 0 1 0 7.8M5.3 18.7a9.5 9.5 0 0 1 0-13.4M18.7 5.3a9.5 9.5 0 0 1 0 13.4" /></>),
  register: (<><path d="M9.5 6.5h10M9.5 12h10M9.5 17.5h10" /><circle cx="5" cy="6.5" r="1.25" {...dot} /><circle cx="5" cy="12" r="1.25" {...dot} /><circle cx="5" cy="17.5" r="1.25" {...dot} /></>),
  scenarios: (<><circle cx="6" cy="6" r="2.25" /><circle cx="6" cy="18" r="2.25" /><circle cx="18" cy="9" r="2.25" /><path d="M6 8.25v7.5M18 11.25V13a5 5 0 0 1-5 5H8.25" /></>),
  sources: <path d="M9 3.5v4M15 3.5v4M6.5 7.5h11v3a5.5 5.5 0 0 1-11 0zM12 16v4.5" />,
  alerts: <path d="M6.5 15.5V11a5.5 5.5 0 0 1 11 0v4.5l1.5 2h-14zM10 20a2 2 0 0 0 4 0" />,
  warning: (<><path d="M10.3 4.7 3.4 16.8a2 2 0 0 0 1.7 3h13.8a2 2 0 0 0 1.7-3L13.7 4.7a2 2 0 0 0-3.4 0z" /><path d="M12 9.5v4" /><circle cx="12" cy="16.6" r="0.9" {...dot} /></>),
  info: (<><circle cx="12" cy="12" r="8.5" /><path d="M12 11v5.5" /><circle cx="12" cy="7.75" r="0.9" {...dot} /></>),
  live: (<><circle cx="12" cy="12" r="3" {...dot} /><circle cx="12" cy="12" r="7.5" /></>),
  search: (<><circle cx="11" cy="11" r="6.5" /><path d="m20 20-4.4-4.4" /></>),
  filter: <path d="M4 6.5h16M7 12h10M10 17.5h4" />,
  sort: <path d="M8 19.5v-15M4.5 8 8 4.5 11.5 8M16 4.5v15M12.5 16l3.5 3.5 3.5-3.5" />,
  export: <path d="M14 4.5h5.5V10M19.5 4.5 11 13M18.5 14v4.5a2 2 0 0 1-2 2h-11a2 2 0 0 1-2-2v-11a2 2 0 0 1 2-2H10" />,
  add: <path d="M12 5v14M5 12h14" />,
  minus: <path d="M5 12h14" />,
  more: (<><circle cx="6" cy="12" r="1.5" {...dot} /><circle cx="12" cy="12" r="1.5" {...dot} /><circle cx="18" cy="12" r="1.5" {...dot} /></>),
  close: <path d="M6.5 6.5l11 11M17.5 6.5l-11 11" />,
  done: <path d="m5 12.5 4.5 4.5L19 7.5" />,
  expand: <path d="m6.5 9.5 5.5 5.5 5.5-5.5" />,
  back: <path d="m14.5 6.5-5.5 5.5 5.5 5.5" />,
  chev: <path d="m9.5 6.5 5.5 5.5-5.5 5.5" />,
  refresh: <path d="M19.5 12a7.5 7.5 0 0 1-13.3 4.8M4.5 12a7.5 7.5 0 0 1 13.3-4.8M18 3.5v3.7h-3.7M6 20.5v-3.7h3.7" />,
  sanctions: (<><circle cx="12" cy="12" r="8.5" /><path d="m6 6 12 12" /></>),
  supply: (<><path d="M12 3.5 4.5 7.5v9l7.5 4 7.5-4v-9z" /><path d="M4.5 7.5 12 11.5l7.5-4M12 11.5v9" /></>),
  geopolitical: (<><circle cx="12" cy="12" r="8.5" /><path d="M3.5 12h17M12 3.5c2.4 2.3 3.6 5.1 3.6 8.5s-1.2 6.2-3.6 8.5c-2.4-2.3-3.6-5.1-3.6-8.5s1.2-6.2 3.6-8.5z" /></>),
  cyber: (<><rect x="5.5" y="10.5" width="13" height="10" rx="2" /><path d="M8.5 10.5V8a3.5 3.5 0 0 1 7 0v2.5M12 14.5v2" /></>),
  financial: <path d="M3.5 19.5h17M5 15l4.5-4.5 3.5 3.5L19 7.5M15 7.5h4v4" />,
  hazard: <path d="M3.5 9.5c1.4 0 1.4-1.5 2.8-1.5s1.4 1.5 2.8 1.5 1.4-1.5 2.8-1.5 1.4 1.5 2.8 1.5 1.4-1.5 2.8-1.5 1.4 1.5 2.8 1.5M3.5 15.5c1.4 0 1.4-1.5 2.8-1.5s1.4 1.5 2.8 1.5 1.4-1.5 2.8-1.5 1.4 1.5 2.8 1.5 1.4-1.5 2.8-1.5 1.4 1.5 2.8 1.5" />,
  spreadsheet: (<><rect x="3.5" y="4.5" width="17" height="15" rx="2" /><path d="M3.5 9.5h17M3.5 14.5h17M9.5 9.5v10" /></>),
  database: (<><ellipse cx="12" cy="6" rx="7" ry="2.5" /><path d="M5 6v12c0 1.4 3.1 2.5 7 2.5s7-1.1 7-2.5V6M5 12c0 1.4 3.1 2.5 7 2.5s7-1.1 7-2.5" /></>),
  webhook: (<><path d="M10 13a5 5 0 0 0 7.5.5l3-3a5 5 0 0 0-7-7L11.8 5.2" /><path d="M14 11a5 5 0 0 0-7.5-.5l-3 3a5 5 0 0 0 7 7l1.7-1.7" /></>),
  upload: <path d="M12 15.5v-11M8 8.5l4-4 4 4M4.5 15.5v3a2 2 0 0 0 2 2h11a2 2 0 0 0 2-2v-3" />,
  fit: (<><circle cx="12" cy="12" r="7" /><circle cx="12" cy="12" r="1.5" {...dot} /><path d="M12 2.5v3M12 18.5v3M2.5 12h3M18.5 12h3" /></>),
  arrowUp: <path d="M12 19V5M6 11l6-6 6 6" />,
  arrowDown: <path d="M12 5v14M6 13l6 6 6-6" />,
} satisfies Record<string, ReactElement>;

export type IconName = keyof typeof paths;

export function Icon({ name, size = 16, stroke = 1.5, ...rest }: { name: IconName; size?: number; stroke?: number } & Omit<SVGProps<SVGSVGElement>, 'stroke'>) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={stroke} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false" {...rest}>
      {paths[name]}
    </svg>
  );
}
