// The map's layer stack in bands, bottom to top. The base style (MapRoot) ends with one
// empty, hidden marker layer per band; a layer added "in" a band goes just under its
// marker, so bands keep their order whichever screen or component adds layers first.
//
//   base      sea, graticule, land, borders, coast (MapRoot)
//   tint      countries with open stories
//   lanes     sea lanes, their flow, the selected entity's routes
//   heat      the risk heatmap
//   dots      entity dots, their shadow and selection ring, feed signals
//   labels    MapLibre-drawn text (city labels, cluster counts)
//   (top)     anything added without a band: short-lived effects such as travelling lines
export const SLOTS = ['tint', 'lanes', 'heat', 'dots', 'labels'] as const;
export type Slot = (typeof SLOTS)[number];
export const slotId = (s: Slot) => `slot-${s}`;
