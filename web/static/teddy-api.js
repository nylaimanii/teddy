/* Where is Teddy's brain? Shared by every page.
 *
 * On the Mac (python -m web.server) the API is the same origin. On Vercel it's the Mac's cloudflared
 * tunnel, which changes every run, so we try, in order:
 *   1. ?api=https://xyz.trycloudflare.com   (web/tunnel.py bakes this into the QR code)
 *   2. the last address that worked on this device
 *   3. TEDDY_CONFIG.api                     (TEDDY_API_URL env var at deploy, "" on the Mac)
 * If none answers, pages show "Teddy is sleeping" with demo data.
 */
window.TEDDY = (() => {
  const cfg = window.TEDDY_CONFIG || {};
  const KEY = 'teddy.api';
  const clean = u => (u == null ? null : String(u).trim().replace(/\/+$/, ''));
  const store = {
    get() { try { return localStorage.getItem(KEY); } catch { return null; } },
    set(v) { try { v ? localStorage.setItem(KEY, v) : localStorage.removeItem(KEY); } catch {} },
  };
  let base = null;

  async function healthy(u) {
    try {
      const ctl = new AbortController(); const t = setTimeout(() => ctl.abort(), 5000);
      const r = await fetch(u + '/api/health', { cache: 'no-store', signal: ctl.signal });
      clearTimeout(t);
      return r.ok && (await r.json()).ok === true;
    } catch { return false; }
  }

  /** Find a live brain. -> base URL ('' = same origin) or null when Teddy is sleeping. */
  async function connect() {
    const fromLink = clean(new URLSearchParams(location.search).get('api'));
    const tries = [fromLink, clean(store.get()), clean(cfg.api)].filter((u, i, a) => u != null && a.indexOf(u) === i);
    for (const u of tries) {
      if (u === '' && cfg.api !== '') continue;  // '' only means "same origin" when the Mac serves the page
      if (await healthy(u)) {
        base = u;
        if (u) store.set(u);
        return u;
      }
    }
    base = null;
    return null;
  }

  /** Remember an address typed by hand ("Teddy's address" box). */
  function remember(u) { store.set(clean(u)); }

  /** Keep ?api= on links between pages so a QR-opened session stays connected. */
  function link(path) {
    const u = new URL(path, location.href);
    if (base) u.searchParams.set('api', base);
    return u.pathname + u.search;
  }

  const url = p => (base || '') + p;
  const post = (p, body) => fetch(url(p), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
  return { connect, remember, link, url, post, cfg, get base() { return base; }, get awake() { return base !== null; } };
})();
