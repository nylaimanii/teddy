// Presage SmartSpectra bridge: Python pipes raw RGB frames on stdin, we print JSON vitals on stdout.
// Frame packet: uint32 width, uint32 height, float64 timestamp_us (little endian), then width*height*3 RGB bytes.
import {
  SmartSpectraSDK, PixelFormat, FrameTransform,
  breathingMetrics, cardioMetrics, decodeMetrics,
} from '@smartspectra/node-sdk';

const apiKey = process.env.PRESAGE_API_KEY;
if (!apiKey) { console.log(JSON.stringify({ error: 'PRESAGE_API_KEY not set' })); process.exit(1); }

const out = (o) => process.stdout.write(JSON.stringify(o) + '\n');
const sdk = new SmartSpectraSDK({ apiKey, requestedMetrics: [...breathingMetrics, ...cardioMetrics] });

sdk.on('metrics', (buf) => {
  const m = decodeMetrics(buf);
  const hr = m.cardio?.pulseRate?.at(-1)?.value;
  const br = m.breathing?.rate?.at(-1)?.value;
  if (hr || br) out({ heart_rate: hr ?? null, breathing_rate: br ?? null });
});
sdk.on('validationStatus', (code, ts, hint) => out({ status: String(code), hint: hint ?? '' }));
sdk.on('error', (code, message, retryable) => out({ error: `${code} ${message}`, retryable }));

sdk.useCustomInput(FrameTransform.kNone);
sdk.start();
out({ status: 'started' });

let pending = Buffer.alloc(0);
process.stdin.on('data', (chunk) => {
  pending = Buffer.concat([pending, chunk]);
  while (pending.length >= 16) {
    const w = pending.readUInt32LE(0), h = pending.readUInt32LE(4), ts = pending.readDoubleLE(8);
    const size = 16 + w * h * 3;
    if (pending.length < size) break;
    const rgb = pending.subarray(16, size);
    sdk.sendFrame(Buffer.from(rgb), w, h, w * 3, PixelFormat.kRGB, Math.round(ts));
    pending = pending.subarray(size);
  }
});
process.stdin.on('end', async () => { await sdk.destroy(); process.exit(0); });
