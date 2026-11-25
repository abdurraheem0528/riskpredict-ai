// The workspace the dashboard acts on (sent as X-Workspace-Id). Remembered per browser.
const KEY = 'rp.workspace';
let current: string | null = (() => {
  try {
    return localStorage.getItem(KEY);
  } catch {
    return null;
  }
})();
const listeners = new Set<() => void>();

export const getWorkspaceId = () => current;
export function setWorkspaceId(id: string | null) {
  if (id === current) return;
  current = id;
  try {
    if (id) localStorage.setItem(KEY, id);
    else localStorage.removeItem(KEY);
  } catch {
    /* ignore */
  }
  listeners.forEach((l) => l());
}
export function subscribeWorkspace(l: () => void) {
  listeners.add(l);
  return () => listeners.delete(l);
}
/** The header param every workspace endpoint requires. */
export function wsh(id: string) {
  return { 'X-Workspace-Id': id };
}
