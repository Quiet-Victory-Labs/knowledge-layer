import { useEffect, useRef, useState, type FormEvent, type ReactNode } from 'react';
import { Button, Callout, Heading, Text, TextField } from './ui';
import { getAuthStatus, setUpLocalOwner, signInLocally, type AuthStatus } from './platformApi';
import './setup.css';

type AuthContext = { status: AuthStatus; firstRun: boolean };

function loginURL(value?: string): string | null {
  if (!value) return null;
  try {
    const url = new URL(value, window.location.origin);
    return ['http:', 'https:'].includes(url.protocol) ? url.href : null;
  } catch { return null; }
}

export default function SetupAuthGate({ children }: { children: (context: AuthContext) => ReactNode }) {
  const [status, setStatus] = useState<AuthStatus | null>(null);
  const [revision, setRevision] = useState(0);
  const [firstRun, setFirstRun] = useState(false);
  const [name, setName] = useState('');
  const [password, setPassword] = useState('');
  const [bootstrapToken, setBootstrapToken] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const requestInFlight = useRef(false);
  const mounted = useRef(true);
  const setupRequired = status?.mode === 'local' && status.bootstrap_required;

  useEffect(() => {
    mounted.current = true;
    const expire = () => {
      setStatus(null);
      setFirstRun(false);
      setPassword('');
      setBootstrapToken('');
      setRevision((current) => current + 1);
    };
    window.addEventListener('knowledge-session-expired', expire);
    return () => {
      mounted.current = false;
      window.removeEventListener('knowledge-session-expired', expire);
    };
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    setError('');
    void getAuthStatus(controller.signal).then((result) => {
      if (!controller.signal.aborted) setStatus(result);
    }).catch((reason: unknown) => {
      if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : 'Sign-in could not be loaded.');
    });
    return () => controller.abort();
  }, [revision]);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (requestInFlight.current || !status || status.mode !== 'local') return;
    if (!name.trim() || name.trim().length > 120 || password.length < 12 || password.length > 1024
      || (setupRequired && (!bootstrapToken.trim() || bootstrapToken.length > 4096))) {
      setError('Enter a name and a password of at least 12 characters. First-time setup also needs the operator’s bootstrap token.');
      return;
    }
    requestInFlight.current = true;
    setBusy(true);
    setError('');
    try {
      if (setupRequired) await setUpLocalOwner({ name: name.trim(), password, bootstrap_token: bootstrapToken.trim() });
      else await signInLocally({ name: name.trim(), password });
      if (!mounted.current) return;
      setFirstRun(Boolean(setupRequired));
      setStatus({ ...status, configured: true, authenticated: true, bootstrap_required: false });
    } catch (reason) {
      if (mounted.current) setError(reason instanceof Error ? reason.message : 'Sign-in could not be completed.');
    } finally {
      requestInFlight.current = false;
      if (mounted.current) {
        setPassword('');
        setBootstrapToken('');
        setBusy(false);
      }
    }
  }

  if (status && (status.mode === 'demo' || status.authenticated)) return children({ status, firstRun });
  const login = loginURL(status?.login_url);

  return <main className="setup-auth">
    <Text size="2" color="gray">knowledge layer</Text>
    <Heading as="h1" size="6">{setupRequired ? 'Create your local account' : 'Sign in'}</Heading>
    {error && <Callout.Root color="red" role="alert"><Callout.Text>{error}</Callout.Text></Callout.Root>}
    {!status ? error ? <Button onClick={() => setRevision((current) => current + 1)}>Retry</Button>
      : <Text role="status" color="gray">Loading sign-in…</Text>
      : status.mode === 'local' ? <>
        {setupRequired && <Text as="p" size="2" color="gray">Use the bootstrap token from the installation operator to claim this instance.</Text>}
        <form className="setup-auth-form" onSubmit={(event) => void submit(event)}>
          <label className="setup-field"><Text size="2" weight="medium">Name</Text><TextField.Root name="name" value={name} onChange={(event) => setName(event.target.value)} required maxLength={120} autoComplete="off" disabled={busy} /></label>
          <div className="setup-field"><label htmlFor="local-owner-password"><Text size="2" weight="medium">Password</Text></label><TextField.Root id="local-owner-password" name="password" type="password" value={password} onChange={(event) => setPassword(event.target.value)} required minLength={12} maxLength={1024} autoComplete="off" disabled={busy} aria-describedby="local-password-hint" /><Text id="local-password-hint" size="1" color="gray">At least 12 characters.</Text></div>
          {setupRequired && <label className="setup-field"><Text size="2" weight="medium">Bootstrap token</Text><TextField.Root name="bootstrap_token" type="password" value={bootstrapToken} onChange={(event) => setBootstrapToken(event.target.value)} required maxLength={4096} autoComplete="off" disabled={busy} /></label>}
          <Button type="submit" loading={busy}>{setupRequired ? 'Create account' : 'Sign in'}</Button>
        </form>
      </> : <>
        <Text as="p" size="2" color="gray">{!status.configured ? 'Ask the installation operator to finish sign-in configuration.' : status.mode === 'iap' ? 'Sign in through this installation’s access gateway.' : 'Use your organization’s configured sign-in.'}</Text>
        {status.configured && login && <Button href={login}>Continue to sign in</Button>}
      </>}
  </main>;
}
