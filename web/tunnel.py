"""Public URL for the phone page via a free Cloudflare quick tunnel, plus a QR code PNG.

    python -m web.server --mock      # in one terminal
    python -m web.tunnel             # in another -> prints URL, writes web/qr.png, opens it
"""
import os
import re
import subprocess
import sys
from pathlib import Path

import qrcode
import requests

PORT = int(os.getenv("PORT", "8000"))
OUT = Path(__file__).parent / "qr.png"


def main():
    try:
        requests.get(f"http://localhost:{PORT}/api/health", timeout=2)
    except Exception:
        print(f"! Nothing on localhost:{PORT} yet. Start it: python -m web.server --mock")
    proc = subprocess.Popen(["cloudflared", "tunnel", "--no-autoupdate", "--url", f"http://localhost:{PORT}"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    url = None
    for line in proc.stdout:
        m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
        if m and not url:
            url = m.group(0)
            qr = qrcode.QRCode(border=2, box_size=14, error_correction=qrcode.constants.ERROR_CORRECT_M)
            qr.add_data(url)
            qr.make_image(fill_color="black", back_color="white").save(OUT)
            print(f"\n  Phone remote:  {url}\n  Caregiver:     {url}/caregiver\n  QR code:       {OUT}\n", flush=True)
            if sys.platform == "darwin" and "--no-open" not in sys.argv:
                subprocess.run(["open", str(OUT)])
            print("  (tunnel running, Ctrl+C to stop; takes ~10s before the URL answers)")
    proc.wait()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
