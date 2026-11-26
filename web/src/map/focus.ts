// Linked hover and selection between the sheet's list and the map, by entity id. A list row
// sets the entity it is about; the map lifts that dot, shows its pill and draws its lanes.
// Hovering a dot sets it here too, and the row for that entity highlights.
import { useSyncExternalStore } from 'react';

export type Focus = { hovered: string | null; selected: string | null; source: 'list' | 'map' | null };

let state: Focus = { hovered: null, selected: null, source: null };
const listeners = new Set<() => void>();
const emit = () => listeners.forEach((l) => l());

export function getFocus(): Focus {
  return state;
}
export function setHovered(id: string | null, source: 'list' | 'map' = 'list') {
  if (state.hovered === id) return;
  state = { ...state, hovered: id, source: id ? source : state.source };
  emit();
}
export function setSelected(id: string | null, source: 'list' | 'map' = 'list') {
  if (state.selected === id) return;
  state = { ...state, selected: id, source: id ? source : state.source };
  emit();
}
export function clearFocus() {
  if (!state.hovered && !state.selected) return;
  state = { hovered: null, selected: null, source: null };
  emit();
}
export function subscribeFocus(l: () => void): () => void {
  listeners.add(l);
  return () => {
    listeners.delete(l);
  };
}
export function useFocus(): Focus {
  return useSyncExternalStore(subscribeFocus, getFocus, getFocus);
}
