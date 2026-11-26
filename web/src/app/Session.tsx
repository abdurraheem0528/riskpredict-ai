import { createContext, useContext, useEffect, useMemo, useRef, useState, useSyncExternalStore, type ReactNode } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { getAuthStatus, refresh, subscribeAuth } from '../api/auth';
import { getWorkspaceId, setWorkspaceId, subscribeWorkspace } from '../api/workspace';
import { applyStreamEvent, useMe, type S } from '../api/queries';
import { LiveStream, type StreamEventName, type StreamEvents, type StreamStatus } from '../api/stream';
import { useMap } from '../map/MapRoot';
import { LivePlayer } from '../live/player';
import type { FxMap } from '../live/effects';

type Ctx = {
  user: S['User'];
  workspace: S['Workspace'];
  role: S['Role'];
  memberships: { workspace: S['Workspace']; role: S['Role'] }[];
  stream: StreamStatus;
  switchWorkspace: (id: string) => void;
};
const SessionContext = createContext<Ctx | null>(null);

export function useSession(): Ctx {
  const c = useContext(SessionContext);
  if (!c) throw new Error('useSession outside a signed-in workspace');
  return c;
}
/** True when the role may change sources, mappings, risks and scenarios. */
export const canEdit = (role: S['Role']) => role !== 'viewer';
export const canAdmin = (role: S['Role']) => role === 'admin' || role === 'owner';

export function useAuthStatus() {
  const status = useSyncExternalStore(subscribeAuth, getAuthStatus, getAuthStatus);
  useEffect(() => {
    if (status === 'unknown') void refresh();
  }, [status]);
  return status;
}

export function useWorkspaceId() {
  return useSyncExternalStore(subscribeWorkspace, getWorkspaceId, getWorkspaceId);
}

/** Resolves the user and workspace, and keeps its live updates coming (polled while visible). */
export function SessionProvider({
  children,
  loading,
  error,
  noWorkspace,
}: {
  children: ReactNode;
  loading: ReactNode;
  error: (retry: () => void, error: unknown) => ReactNode;
  noWorkspace: ReactNode;
}) {
  const me = useMe();
  const wsId = useWorkspaceId();
  const qc = useQueryClient();
  const [stream, setStream] = useState<StreamStatus>('off');

  const memberships = me.data?.memberships;
  const chosen = useMemo(() => {
    if (!memberships?.length) return null;
    return memberships.find((m) => m.workspace.id === wsId) ?? memberships[0];
  }, [memberships, wsId]);

  useEffect(() => {
    if (chosen && chosen.workspace.id !== wsId) setWorkspaceId(chosen.workspace.id);
  }, [chosen, wsId]);

  // The live sequence draws on the map (ripples, lines, pulses) when there is one.
  const { map, lib } = useMap();
  const fx = useRef<FxMap | null>(null);
  useEffect(() => {
    fx.current = map && lib ? { map, lib } : null;
  }, [map, lib]);

  const activeId = chosen?.workspace.id ?? null;
  useEffect(() => {
    if (!activeId) return;
    // Polls GET /v1/events while the tab is visible; a resync refetches everything. Each
    // poll's events go to the player, which applies them at once or after their sequence.
    const apply = <K extends StreamEventName>(name: K, data: StreamEvents[K]) => applyStreamEvent(qc, activeId, name, data);
    const player = new LivePlayer({ qc, ws: activeId, getMap: () => fx.current, apply });
    const s = new LiveStream(activeId, apply, setStream, document, (b) => player.handle(b));
    s.start();
    return () => {
      s.stop();
      player.stop();
    };
  }, [activeId, qc]);

  if (me.isPending) return <>{loading}</>;
  if (me.isError) return <>{error(() => void me.refetch(), me.error)}</>;
  if (!chosen) return <>{noWorkspace}</>;

  const value: Ctx = {
    user: me.data.user,
    workspace: chosen.workspace,
    role: chosen.role,
    memberships: me.data.memberships,
    stream,
    switchWorkspace: (id) => {
      if (id === chosen.workspace.id) return;
      setWorkspaceId(id);
    },
  };
  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}
