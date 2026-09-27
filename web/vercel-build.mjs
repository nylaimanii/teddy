// Vercel build: static copy of web/static + a config.js from env vars. No framework, no Python.
//   TEDDY_API_URL    the Mac's cloudflared tunnel, e.g. https://abc-def.trycloudflare.com (optional:
//                    ?api=... in the link / QR overrides it, since quick-tunnel URLs change every run)
//   TEDDY_VIDEO_URL  demo video (YouTube, Vimeo, or .mp4) for the landing page (optional)
//   TEDDY_PARENT_URL the Parent dashboard on Vultr (optional; the landing page links to it)
import fs from 'node:fs';

const OUT = 'dist';
fs.rmSync(OUT, { recursive: true, force: true });
fs.mkdirSync(OUT);
const copy = (from, to) => fs.cpSync(`static/${from}`, `${OUT}/${to}`, { recursive: true });
copy('landing.html', 'index.html');
copy('index.html', 'teddy.html');
copy('teddy-api.js', 'teddy-api.js');
copy('demo', 'demo');

const cfg = {
  api: (process.env.TEDDY_API_URL || '').trim().replace(/\/+$/, '') || null,
  video: (process.env.TEDDY_VIDEO_URL || '').trim(),
  parent: (process.env.TEDDY_PARENT_URL || '').trim(),   // the Vultr Parent dashboard, e.g. https://1-2-3-4.sslip.io
};
fs.writeFileSync(`${OUT}/config.js`, `window.TEDDY_CONFIG = ${JSON.stringify(cfg)};\n`);
console.log('Teddy static site ->', OUT, cfg);
