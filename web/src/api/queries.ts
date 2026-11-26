// One hook per endpoint the screens read. Keys start with the workspace id, so switching
// workspace can never show another workspace's rows, and live-update invalidation is by prefix.
import { keepPreviousData, useInfiniteQuery, useQuery, type QueryClient } from '@tanstack/react-query';
import type { components } from './schema';
import { api, unwrap } from './client';
import { wsh } from './workspace';
import type { StreamEventName, StreamEvents } from './stream';

export type S = components['schemas'];
export type Risk = S['Risk'];
export type RiskDetail = S['RiskDetail'];
export type Source = S['Source'];
export type Connection = S['Connection'];
export type Entity = S['Entity'];
export type Scenario = S['Scenario'];
export type EntityFC = S['EntityFeatureCollection'];
export type RiskPlaceFC = S['RiskPlaceFeatureCollection'];
export type EventFC = S['FeedEventFeatureCollection'];
export type DisruptionFC = S['DisruptionFeatureCollection'];

const MIN = 60_000;

export function useMe() {
  return useQuery({ queryKey: ['me'], queryFn: () => unwrap(api.GET('/v1/me')), staleTime: 5 * MIN });
}

export function useProviders() {
  return useQuery({ queryKey: ['providers'], queryFn: () => unwrap(api.GET('/v1/connections/providers')), staleTime: 10 * MIN });
}

export function useConnections(ws: string) {
  return useQuery({
    queryKey: [ws, 'connections'],
    queryFn: () => unwrap(api.GET('/v1/connections', { params: { header: wsh(ws) } })),
  });
}

export function useConnection(ws: string, id: string | null, poll = false) {
  return useQuery({
    queryKey: [ws, 'connection', id],
    enabled: !!id,
    queryFn: () => unwrap(api.GET('/v1/connections/{connection_id}', { params: { header: wsh(ws), path: { connection_id: id! } } })),
    refetchInterval: (q) => (poll && q.state.data?.status === 'pending' ? 2000 : false),
  });
}

export function useSources(ws: string) {
  return useQuery({
    queryKey: [ws, 'sources'],
    queryFn: () => unwrap(api.GET('/v1/sources', { params: { header: wsh(ws) } })),
  });
}

export function useSource(ws: string, id: string) {
  return useQuery({
    queryKey: [ws, 'source', id],
    queryFn: () => unwrap(api.GET('/v1/sources/{source_id}', { params: { header: wsh(ws), path: { source_id: id } } })),
  });
}

export function useMapping(ws: string, id: string) {
  return useQuery({
    queryKey: [ws, 'mapping', id],
    queryFn: () => unwrap(api.GET('/v1/sources/{source_id}/mapping', { params: { header: wsh(ws), path: { source_id: id } } })),
    // The suggestion is normally made before POST /v1/sources answers (and announced as an
    // event); refetch as a fallback while there is none yet.
    refetchInterval: (q) => (q.state.data && !q.state.data.suggested && !q.state.data.confirmed ? 4000 : false),
  });
}

export function useBrowse(ws: string, connectionId: string | null, parentId?: string) {
  return useQuery({
    queryKey: [ws, 'browse', connectionId, parentId ?? null],
    enabled: !!connectionId,
    queryFn: () =>
      unwrap(
        api.GET('/v1/connections/{connection_id}/browse', {
          params: { header: wsh(ws), path: { connection_id: connectionId! }, query: parentId ? { parent_id: parentId } : {} },
        }),
      ),
  });
}

export function useFeedHealth() {
  return useQuery({ queryKey: ['feedHealth'], queryFn: () => unwrap(api.GET('/v1/feeds/health')), staleTime: 5 * MIN, refetchInterval: 10 * MIN });
}

export function useMapEvents() {
  return useQuery({
    queryKey: ['mapEvents'],
    queryFn: () => unwrap(api.GET('/v1/map/events', { params: { query: { limit: 2000 } } })) as Promise<EventFC>,
    staleTime: 5 * MIN,
    refetchInterval: 15 * MIN,
  });
}

export function useDisruptions() {
  return useQuery({
    queryKey: ['disruptions'],
    queryFn: () => unwrap(api.GET('/v1/map/disruptions')) as Promise<DisruptionFC>,
    staleTime: 5 * MIN,
  });
}

export function useMapEntities(ws: string) {
  return useQuery({
    queryKey: [ws, 'mapEntities'],
    queryFn: () => unwrap(api.GET('/v1/map/entities', { params: { header: wsh(ws) } })) as Promise<EntityFC>,
  });
}

export function useMapRisks(ws: string, category: string | null) {
  return useQuery({
    queryKey: [ws, 'mapRisks', category],
    queryFn: () =>
      unwrap(api.GET('/v1/map/risks', { params: { header: wsh(ws), query: category ? { category } : {} } })) as Promise<RiskPlaceFC>,
    placeholderData: keepPreviousData,
  });
}

export function useRiskSummary(ws: string) {
  return useQuery({
    queryKey: [ws, 'riskSummary'],
    queryFn: () => unwrap(api.GET('/v1/risks/summary', { params: { header: wsh(ws) } })),
  });
}

export type RiskSort = '-score' | '-last_seen_at';
export function useRisks(ws: string, opts: { category?: string | null; sort?: RiskSort; limit?: number; entityId?: string | null; group?: 'story' | null } = {}) {
  const { category = null, sort = '-score', limit = 50, entityId = null, group = null } = opts;
  return useInfiniteQuery({
    queryKey: [ws, 'risks', { category, sort, limit, entityId, group }],
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) =>
      unwrap(
        api.GET('/v1/risks', {
          params: {
            header: wsh(ws),
            query: {
              sort,
              limit,
              ...(category ? { category } : {}),
              ...(entityId ? { entity_id: entityId } : {}),
              ...(group ? { group } : {}),
              ...(pageParam ? { cursor: pageParam } : {}),
            },
          },
        }),
      ),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    placeholderData: keepPreviousData,
  });
}

export function useRisk(ws: string, id: string | null) {
  return useQuery({
    queryKey: [ws, 'risk', id],
    enabled: !!id,
    queryFn: () => unwrap(api.GET('/v1/risks/{risk_id}', { params: { header: wsh(ws), path: { risk_id: id! } } })),
  });
}

const riskDetailFn = (ws: string, id: string) => () => unwrap(api.GET('/v1/risks/{risk_id}', { params: { header: wsh(ws), path: { risk_id: id } } }));

/** The risk's detail, fresh from the API (the live sequence reads its evidence), into the same cache entry. */
export function fetchRiskDetail(qc: QueryClient, ws: string, id: string): Promise<RiskDetail> {
  return qc.fetchQuery({ queryKey: [ws, 'risk', id], staleTime: 0, queryFn: riskDetailFn(ws, id) });
}

/** Load a risk's detail ahead of opening it (a row under the pointer), so its screen opens drawn. */
export function prefetchRiskDetail(qc: QueryClient, ws: string, id: string) {
  void qc.prefetchQuery({ queryKey: [ws, 'risk', id], staleTime: 30_000, queryFn: riskDetailFn(ws, id) });
}

export function useSeries(series: string | null, key: string | null) {
  return useQuery({
    queryKey: ['series', series, key],
    enabled: !!series && !!key,
    queryFn: () => unwrap(api.GET('/v1/feeds/series', { params: { query: { series: series!, key: key! } } })),
    staleTime: 10 * MIN,
  });
}

/** Alerts, newest first, 20 a page; `fetchNextPage` follows the cursor ("Show all"). */
export function useAlerts(ws: string) {
  return useInfiniteQuery({
    queryKey: [ws, 'alerts'],
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) =>
      unwrap(api.GET('/v1/alerts', { params: { header: wsh(ws), query: { limit: 20, ...(pageParam ? { cursor: pageParam } : {}) } } })),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
}

export function useScenarios(ws: string) {
  return useQuery({
    queryKey: [ws, 'scenarios'],
    queryFn: () => unwrap(api.GET('/v1/scenarios', { params: { header: wsh(ws), query: { limit: 10 } } })),
  });
}

export function useScenario(ws: string, id: string | null) {
  return useQuery({
    queryKey: [ws, 'scenario', id],
    enabled: !!id,
    queryFn: () => unwrap(api.GET('/v1/scenarios/{scenario_id}', { params: { header: wsh(ws), path: { scenario_id: id! } } })),
  });
}

export function useEntities(ws: string, q = '', enabled = true) {
  return useQuery({
    queryKey: [ws, 'entities', q],
    enabled,
    queryFn: () => unwrap(api.GET('/v1/entities', { params: { header: wsh(ws), query: { limit: 200, ...(q ? { q } : {}) } } })),
    placeholderData: keepPreviousData,
    staleTime: MIN,
  });
}

/** What each live event makes stale. */
export function applyStreamEvent<K extends StreamEventName>(qc: QueryClient, ws: string, name: K, data: StreamEvents[K]) {
  const inv = (...key: unknown[]) => void qc.invalidateQueries({ queryKey: key });
  switch (name) {
    case 'risk.created':
    case 'risk.updated':
    case 'risk.resolved': {
      const { risk } = data as StreamEvents['risk.created'];
      inv(ws, 'risks');
      inv(ws, 'riskSummary');
      inv(ws, 'mapRisks');
      inv(ws, 'mapEntities');
      inv(ws, 'risk', risk.id);
      inv(ws, 'alerts'); // an alert shows its risk's status, and is read once the risk resolves
      break;
    }
    case 'alert.created':
      inv(ws, 'alerts');
      break;
    case 'source.sync_started':
    case 'source.synced':
    case 'source.sync_failed': {
      const e = data as StreamEvents['source.synced'];
      inv(ws, 'sources');
      inv(ws, 'source', e.source_id);
      inv(ws, 'connections');
      inv(ws, 'mapEntities');
      break;
    }
    case 'mapping.suggested': {
      const e = data as StreamEvents['mapping.suggested'];
      inv(ws, 'mapping', e.source_id);
      inv(ws, 'source', e.source_id);
      inv(ws, 'sources');
      break;
    }
    case 'connection.updated': {
      const e = data as StreamEvents['connection.updated'];
      inv(ws, 'connections');
      inv(ws, 'connection', e.connection_id);
      break;
    }
    case 'feeds.updated':
      inv('feedHealth');
      inv('mapEvents');
      inv('disruptions');
      inv(ws, 'riskSummary');
      break;
    case 'resync':
      void qc.invalidateQueries();
      break;
  }
}
