// Sign-in with Google and the ROAM session.
//
// The Google button gives an ID token; POST /auth/google turns it into a ROAM session token, kept in this
// browser and sent as `Authorization: Bearer ...` on every request to the API (installed once, globally,
// by wrapping fetch -- so existing calls need no changes).
import { writable } from 'svelte/store';

export const API_BASE: string = import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000';
const TOKEN_KEY = 'roam.session';
const USER_KEY = 'roam.user';

export type User = { email: string; name: string; picture?: string | null; admin?: boolean };

function read<T>(key: string): T | null {
  try {
    const v = localStorage.getItem(key);
    return v ? (JSON.parse(v) as T) : null;
  } catch {
    return null;
  }
}

export const user = writable<User | null>(null);
let token: string | null = null;

let installed = false;
export function installAuth() {
  if (installed || typeof window === 'undefined') return;
  installed = true;
  token = read<string>(TOKEN_KEY);
  user.set(token ? read<User>(USER_KEY) : null);
  const original = window.fetch.bind(window);
  window.fetch = (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
    if (token && url.startsWith(API_BASE)) {
      const headers = new Headers(init?.headers ?? (input instanceof Request ? input.headers : undefined));
      if (!headers.has('Authorization')) headers.set('Authorization', `Bearer ${token}`);
      return original(input, { ...init, headers }).then((res) => {
        if (res.status === 401 && url !== `${API_BASE}/auth/google`) {
          // the session expired: forget it so the UI offers sign-in again
          void original(`${API_BASE}/auth/me`, { headers: { Authorization: `Bearer ${token}` } }).then((me) => {
            if (me.status === 401) signOut();
          });
        }
        return res;
      });
    }
    return original(input, init);
  };
}

function save(t: string | null, u: User | null) {
  token = t;
  user.set(u);
  try {
    if (t && u) {
      localStorage.setItem(TOKEN_KEY, JSON.stringify(t));
      localStorage.setItem(USER_KEY, JSON.stringify(u));
    } else {
      localStorage.removeItem(TOKEN_KEY);
      localStorage.removeItem(USER_KEY);
    }
  } catch {
    // storage blocked: signed in for this page only
  }
}

export function signOut() {
  save(null, null);
  (window as any).google?.accounts?.id?.disableAutoSelect?.();
}

let clientIdPromise: Promise<string | null> | null = null;
function clientId(): Promise<string | null> {
  clientIdPromise ??= fetch(`${API_BASE}/auth/config`)
    .then((r) => (r.ok ? r.json() : null))
    .then((c) => c?.google_client_id ?? null)
    .catch(() => null);
  return clientIdPromise;
}

let gsiPromise: Promise<void> | null = null;
function loadGsi(): Promise<void> {
  gsiPromise ??= new Promise((resolve, reject) => {
    const s = document.createElement('script');
    s.src = 'https://accounts.google.com/gsi/client';
    s.async = true;
    s.onload = () => resolve();
    s.onerror = () => reject(new Error('Could not load Google sign-in'));
    document.head.appendChild(s);
  });
  return gsiPromise;
}

/** Renders Google's sign-in button into `el`. Resolves to an error message when sign-in is unavailable. */
export async function renderGoogleButton(el: HTMLElement, opts: { text?: string; width?: number } = {}): Promise<string | null> {
  const id = await clientId();
  if (!id) return 'Google sign-in is not configured on this server yet.';
  try {
    await loadGsi();
  } catch (e) {
    return e instanceof Error ? e.message : 'Could not load Google sign-in';
  }
  const google = (window as any).google;
  google.accounts.id.initialize({
    client_id: id,
    callback: async (resp: { credential: string }) => {
      const res = await fetch(`${API_BASE}/auth/google`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ credential: resp.credential })
      });
      const body = await res.json().catch(() => null);
      if (res.ok && body?.token) save(body.token, body.user);
      else alert(body?.detail ?? 'Google sign-in failed');
    }
  });
  google.accounts.id.renderButton(el, {
    theme: 'outline', size: 'large', shape: 'pill', text: opts.text ?? 'continue_with', width: opts.width ?? 260
  });
  return null;
}
