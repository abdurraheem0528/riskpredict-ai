// Shared by the Risks list, the Overview and the risk page: a story's place phrase and lead
// headline, and the two-row receipt behind its score.
import type { Risk } from '../api/queries';
import { placePhrase } from '../lib/format';
import { countryName } from '../map/geo';

/** The row's first line: the place phrase (lib/format.ts), with country names from the map's gazetteer. */
export function rowPlace(r: Pick<Risk, 'category' | 'where' | 'entity' | 'lead' | 'title'>): string {
  return placePhrase(r.category, r.where, { city: r.entity.city, leadType: r.lead?.type, country: countryName, fallback: r.title });
}

/** The row's second line: the lead report's headline; a market move has none, so its title says what moved. */
export const rowHeadline = (r: Pick<Risk, 'lead' | 'title'>) => r.lead?.title ?? r.title;

/** Label, value (right, tabular) and reason, per row: Likely, then Impact. */
export function Receipt({ rows, large = false }: { rows: readonly { label: string; value: string; reason: string }[]; large?: boolean }) {
  return (
    <div className={`rcpt${large ? ' lg' : ''}`}>
      {rows.map((x) => (
        <div className="rl" key={x.label}>
          <span>{x.label}</span>
          <span>{x.value}</span>
          <span>{x.reason}</span>
        </div>
      ))}
    </div>
  );
}
