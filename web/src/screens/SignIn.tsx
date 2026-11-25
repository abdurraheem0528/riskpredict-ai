// Not in the designs: sign-in and sign-up use the Empty screen's centred glass card over
// the quiet map, with the System-Controls form fields.
import { useState, type FormEvent } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { signIn, signUp } from '../api/auth';
import { setWorkspaceId } from '../api/workspace';
import { ApiError, errorText } from '../api/errors';
import { Link, navigate } from '../lib/router';
import { Icon } from '../ui/Icon';

export function SignIn({ mode }: { mode: 'sign-in' | 'sign-up' }) {
  const up = mode === 'sign-up';
  const qc = useQueryClient();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [name, setName] = useState('');
  const [company, setCompany] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const fieldError = (f: string) => (error instanceof ApiError ? error.errors.find((e) => e.field === `/${f}`)?.message : undefined);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const r = up
        ? await signUp({ email, password, workspace_name: company.trim(), ...(name.trim() ? { display_name: name.trim() } : {}) })
        : await signIn(email, password);
      qc.clear();
      if (r.workspace_id) setWorkspaceId(r.workspace_id);
      navigate('/', { replace: true });
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  const short = up && password.length > 0 && password.length < 12;
  return (
    <section className="card glass" aria-label={up ? 'Create an account' : 'Sign in'}>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
        <span className="lbl" style={{ display: 'inline-flex', gap: 8, alignItems: 'center' }}>
          <span className="wsmark" style={{ width: 24, height: 24, borderRadius: 12 }}><Icon name="signals" size={14} stroke={1.75} /></span>
          RiskPredict
        </span>
        <h1 className="h1">{up ? 'Create your workspace' : 'Sign in'}</h1>
        <span className="body15">
          {up ? 'One workspace per company. You can invite people who already have an account.' : 'Risks that touch your suppliers, routes and tools, on one map.'}
        </span>
      </div>
      <form onSubmit={submit} style={{ display: 'flex', flexDirection: 'column', gap: 16 }} noValidate>
        {up && (
          <>
            <div className="field">
              <label htmlFor="company">Company</label>
              <input id="company" className="inp" required maxLength={120} value={company} onChange={(e) => setCompany(e.target.value)} autoComplete="organization" aria-invalid={!!fieldError('workspace_name')} />
              {fieldError('workspace_name') && <span className="err">{fieldError('workspace_name')}</span>}
            </div>
            <div className="field">
              <label htmlFor="name">Your name <span className="t12">(optional)</span></label>
              <input id="name" className="inp" maxLength={120} value={name} onChange={(e) => setName(e.target.value)} autoComplete="name" />
            </div>
          </>
        )}
        <div className="field">
          <label htmlFor="email">Email</label>
          <input id="email" className="inp" type="email" required value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="email" aria-invalid={!!fieldError('email')} />
          {fieldError('email') && <span className="err">{fieldError('email')}</span>}
        </div>
        <div className="field">
          <label htmlFor="password">Password</label>
          <input
            id="password"
            className="inp"
            type="password"
            required
            minLength={up ? 12 : undefined}
            maxLength={128}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete={up ? 'new-password' : 'current-password'}
            aria-invalid={short || !!fieldError('password')}
            aria-describedby="pw-note"
          />
          <span id="pw-note" className={short || fieldError('password') ? 'err' : 't12'}>
            {fieldError('password') ?? (up ? 'At least 12 characters.' : '')}
          </span>
        </div>
        {error != null && !(error instanceof ApiError && error.errors.length) && (
          <div className="note" role="alert"><Icon name="warning" style={{ flex: 'none', marginTop: 1 }} />{error instanceof ApiError && error.code === 'email_taken' ? 'An account with this email exists. Sign in instead.' : errorText(error)}</div>
        )}
        <button type="submit" className="b pri" disabled={busy || !email || !password || (up && (!company.trim() || password.length < 12))}>
          {busy && <span className="spin" />}
          {up ? 'Create workspace' : 'Sign in'}
        </button>
      </form>
      <span className="t13" style={{ textAlign: 'center', marginTop: -8 }}>
        {up ? <>Have an account? <Link to="/sign-in">Sign in</Link></> : <>New here? <Link to="/sign-up">Create a workspace</Link></>}
      </span>
    </section>
  );
}
