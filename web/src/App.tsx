import { useEffect, useState, type ReactNode } from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ApiError } from './api/errors';
import { API_CONFIGURED } from './api/config';
import { MapRoot } from './map/MapRoot';
import { SessionProvider, useAuthStatus, useSession } from './app/Session';
import { ToastProvider } from './app/Toast';
import { TopBar, useWorkspaceState } from './app/TopBar';
import { JumpTo } from './app/JumpTo';
import { match, navigate, useLocation } from './lib/router';
import { SignIn } from './screens/SignIn';
import { Empty } from './screens/Empty';
import { Main } from './screens/Main';
import { RiskView } from './screens/RiskView';
import { Scenario } from './screens/Scenario';
import { Connect } from './screens/Connect';
import { Confirm } from './screens/Confirm';
import { ErrorState } from './ui/parts';

export function makeQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: (n, e) => !(e instanceof ApiError && e.status >= 400 && e.status < 500) && n < 2,
        refetchOnWindowFocus: false,
        staleTime: 30_000,
      },
      mutations: { retry: false },
    },
  });
}

export function App({ client }: { client?: QueryClient }) {
  const [qc] = useState(() => client ?? makeQueryClient());
  return (
    <QueryClientProvider client={qc}>
      <ToastProvider>
        <div className="app">
          <Root />
        </div>
      </ToastProvider>
    </QueryClientProvider>
  );
}

function Card({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <section className="card glass" aria-label={title}>
      <h1 className="h2">{title}</h1>
      {children}
    </section>
  );
}

function Root() {
  const status = useAuthStatus();
  const { path } = useLocation();
  const authPath = path === '/sign-in' || path === '/sign-up';

  useEffect(() => {
    if (status === 'signed-out' && !authPath) navigate('/sign-in', { replace: true });
    if (status === 'signed-in' && authPath) navigate('/', { replace: true });
  }, [status, authPath]);

  if (!API_CONFIGURED)
    return (
      <MapRoot veil>
        <Card title="The API address is missing">
          <span className="body15">Build the dashboard with VITE_API_URL set to the API’s base URL.</span>
        </Card>
      </MapRoot>
    );
  if (status === 'unknown') return <MapRoot />;
  if (status === 'signed-out')
    return (
      <MapRoot veil>
        <SignIn mode={path === '/sign-up' ? 'sign-up' : 'sign-in'} />
      </MapRoot>
    );
  return (
    <MapRoot>
      <SessionProvider
        loading={null}
        error={(retry, err) => (
          <Card title="Your workspace did not load">
            <ErrorState error={err} retry={retry} title="" />
          </Card>
        )}
        noWorkspace={
          <Card title="You are not in a workspace">
            <span className="body15">Ask an admin to add your email to theirs, or sign out and create a new account for your company.</span>
          </Card>
        }
      >
        <Shell />
      </SessionProvider>
    </MapRoot>
  );
}

function Shell() {
  const [jump, setJump] = useState(false);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        setJump((j) => !j);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);
  return (
    <>
      <TopBar onJump={() => setJump(true)} />
      <Screen />
      {jump && <JumpTo onClose={() => setJump(false)} />}
    </>
  );
}

function Screen() {
  const { path } = useLocation();
  const { workspace } = useSession();
  const { ready, sources, connections } = useWorkspaceState();

  let m: Record<string, string> | null;
  if ((m = match('/risks/:id', path))) return <RiskView key={m.id} id={m.id} />;
  if ((m = match('/sources/:id/confirm', path))) return <Confirm key={m.id} id={m.id} />;
  if (path === '/connect') return <Connect />;

  if (path === '/' || path === '/register' || path === '/risks' || path === '/scenarios') {
    if (sources.isPending || connections.isPending) return null;
    if (sources.isError || connections.isError)
      return (
        <Card title="Your sources did not load">
          <ErrorState error={sources.error ?? connections.error} retry={() => { void sources.refetch(); void connections.refetch(); }} title="" />
        </Card>
      );
    if (!ready) {
      const pending = sources.data.items.find((s) => s.status === 'new' || s.status === 'mapping_suggested') ?? null;
      return (
        <>
          <div className="veil" aria-hidden="true" />
          <Empty key={workspace.id} pending={pending} hasConnection={connections.data.items.some((c) => c.status === 'active')} />
        </>
      );
    }
    if (path === '/scenarios') return <Scenario key={workspace.id} />;
    // The Risks list answers at /register (its first address) and at /risks.
    const list = path === '/register' || path === '/risks';
    return <Main key={`${workspace.id}:${list ? 'risks' : path}`} mode={list ? 'register' : 'overview'} />;
  }
  return (
    <Card title="Nothing here">
      <span className="body15">This address is not part of RiskPredict.</span>
      <button type="button" className="b pri" onClick={() => navigate('/')}>Go to the overview</button>
    </Card>
  );
}
