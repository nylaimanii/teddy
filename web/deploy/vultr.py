"""Put the Parent dashboard + API on a Vultr server, with HTTPS.

    python -m web.deploy.vultr --build          # just build + run the container locally on :8080 (no Vultr)
    python -m web.deploy.vultr --create         # new Vultr VPS (vc2-1c-1gb, ~$5/mo, New Jersey) + deploy
    python -m web.deploy.vultr --update         # re-ship the code/env to the existing server
    python -m web.deploy.vultr --destroy        # delete the server (stops the billing)

Needs in .env: VULTR_API_KEY, PARENT_PIN, SNOWFLAKE_*, and TIGER_DATABASE_URL (optional).
HTTPS comes from Caddy + Let's Encrypt on <ip>.sslip.io (a free wildcard DNS name that points at the IP),
or set PARENT_DOMAIN to your own domain. The server only gets what the parent page needs: no ElevenLabs,
Gemini or Solana keys, and no camera frames (photos come from Snowflake as short-lived signed URLs).
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).parent
STATE = ROOT / "data" / "vultr.json"  # instance id + ip (git-ignored folder)
API = "https://api.vultr.com/v2"
ENV_KEYS = ["SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER", "SNOWFLAKE_PASSWORD", "SNOWFLAKE_ROLE", "SNOWFLAKE_WAREHOUSE",
            "TIGER_DATABASE_URL", "PARENT_PIN", "TEDDY_KID", "TEDDY_TZ"]
REGION, PLAN, LABEL = os.getenv("VULTR_REGION", "ewr"), os.getenv("VULTR_PLAN", "vc2-1c-1gb"), "teddy-parent"
CLOUD_INIT = """#cloud-config
package_update: true
packages: [docker.io, rsync]
runcmd:
  - systemctl enable --now docker
  - ufw allow 22 && ufw allow 80 && ufw allow 443 && ufw --force enable || true
"""


def env():
    e = {**dotenv_values(ROOT / ".env"), **{k: v for k, v in os.environ.items() if k in ENV_KEYS + ["VULTR_API_KEY"]}}
    return e


def api(method, path, **kw):
    key = env().get("VULTR_API_KEY")
    if not key:
        sys.exit("Add VULTR_API_KEY to .env (Vultr console -> Account -> API -> enable, allow your IP).")
    r = requests.request(method, API + path, headers={"Authorization": f"Bearer {key}"}, timeout=30, **kw)
    if r.status_code >= 300:
        sys.exit(f"Vultr {method} {path}: {r.status_code} {r.text[:300]}")
    return r.json() if r.text else {}


def stage():
    """Only what the parent app needs -> a temp build dir."""
    d = Path(tempfile.mkdtemp(prefix="teddy-parent-"))
    shutil.copy(HERE / "Dockerfile.parent", d / "Dockerfile")
    shutil.copy(HERE / "requirements-parent.txt", d)
    shutil.copytree(ROOT / "brain", d / "brain", ignore=shutil.ignore_patterns("__pycache__", "*.db", "*.pyc"))
    (d / "web" / "static").mkdir(parents=True)
    (d / "web" / "__init__.py").write_text("")
    shutil.copy(ROOT / "web" / "parent.py", d / "web")
    shutil.copy(ROOT / "web" / "static" / "parent.html", d / "web" / "static")
    e = env()
    missing = [k for k in ("SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER", "SNOWFLAKE_PASSWORD", "PARENT_PIN") if not e.get(k)]
    if missing:
        sys.exit(f"Missing in .env: {', '.join(missing)} (PARENT_PIN guards your kid's data on the public internet)")
    (d / "parent.env").write_text("".join(f"{k}={e[k]}\n" for k in ENV_KEYS if e.get(k)))
    os.chmod(d / "parent.env", 0o600)
    return d


def build_local():
    d = stage()
    subprocess.run(["docker", "build", "-q", "-t", "teddy-parent", str(d)], check=True)
    subprocess.run(["docker", "rm", "-f", "teddy-parent"], capture_output=True)
    subprocess.run(["docker", "run", "-d", "--name", "teddy-parent", "--env-file", str(d / "parent.env"),
                    "-p", "8080:8080", "teddy-parent"], check=True)
    shutil.rmtree(d)
    print("Parent dashboard (container) on http://localhost:8080")


def ssh(ip, cmd, check=True):
    return subprocess.run(["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10", f"root@{ip}", cmd],
                          check=check, capture_output=not check, text=True)


def ship(ip):
    """rsync the staged app to the server, build it there, run it + Caddy (auto HTTPS)."""
    host = env().get("PARENT_DOMAIN") or f"{ip.replace('.', '-')}.sslip.io"
    d = stage()
    (d / "Caddyfile").write_text((HERE / "Caddyfile.tmpl").read_text().replace("{HOST}", host))
    for _ in range(30):  # cloud-init installs docker on first boot
        if ssh(ip, "command -v docker && systemctl is-active docker", check=False).returncode == 0:
            break
        time.sleep(10)
    subprocess.run(["rsync", "-az", "--delete", "-e", "ssh -o StrictHostKeyChecking=accept-new", f"{d}/", f"root@{ip}:/opt/teddy/"],
                   check=True)
    shutil.rmtree(d)
    ssh(ip, " && ".join([
        "cd /opt/teddy", "chmod 600 parent.env",
        "docker network create teddy 2>/dev/null || true",
        "docker build -q -t teddy-parent .",
        "docker rm -f parent caddy 2>/dev/null || true",
        "docker run -d --name parent --network teddy --restart unless-stopped --env-file parent.env teddy-parent",
        "docker run -d --name caddy --network teddy --restart unless-stopped -p 80:80 -p 443:443 "
        "-v /opt/teddy/Caddyfile:/etc/caddy/Caddyfile -v caddy_data:/data caddy:2",
    ]))
    url = f"https://{host}"
    for _ in range(30):
        try:
            if requests.get(url + "/api/parent/health", timeout=5).ok:
                break
        except Exception:
            pass
        time.sleep(5)
    print(f"\nParent dashboard: {url}\n(PIN = PARENT_PIN from .env)")
    return url


def create():
    keys = api("GET", "/ssh-keys")["ssh_keys"]
    pub = next((p for p in (Path.home() / ".ssh").glob("id_*.pub")), None)
    if not pub:
        sys.exit("No SSH key found. Make one: ssh-keygen -t ed25519")
    key = next((k for k in keys if k["ssh_key"].strip() == pub.read_text().strip()), None) or \
        api("POST", "/ssh-keys", json={"name": "teddy-mac", "ssh_key": pub.read_text().strip()})["ssh_key"]
    os_id = next(o["id"] for o in api("GET", "/os?per_page=500")["os"] if o["name"].startswith("Ubuntu 24.04") and o["arch"] == "x64")
    import base64
    inst = api("POST", "/instances", json={
        "region": REGION, "plan": PLAN, "os_id": os_id, "label": LABEL, "hostname": LABEL, "sshkey_id": [key["id"]],
        "backups": "disabled", "enable_ipv6": True, "tags": ["teddy"],
        "user_data": base64.b64encode(CLOUD_INIT.encode()).decode()})["instance"]
    print(f"Creating {PLAN} in {REGION} ({inst['id']})…")
    while True:
        i = api("GET", f"/instances/{inst['id']}")["instance"]
        if i["status"] == "active" and i["main_ip"] not in ("", "0.0.0.0"):
            break
        time.sleep(8)
    STATE.write_text(json.dumps({"id": i["id"], "ip": i["main_ip"]}))
    print(f"Server up at {i['main_ip']}; waiting for first boot…")
    time.sleep(45)
    return ship(i["main_ip"])


def state():
    if not STATE.exists():
        sys.exit("No server yet: python -m web.deploy.vultr --create")
    return json.loads(STATE.read_text())


if __name__ == "__main__":
    if "--build" in sys.argv:
        build_local()
    elif "--create" in sys.argv:
        create()
    elif "--update" in sys.argv:
        ship(state()["ip"])
    elif "--destroy" in sys.argv:
        s = state()
        api("DELETE", f"/instances/{s['id']}")
        STATE.unlink()
        print(f"Deleted {s['id']} ({s['ip']}).")
    else:
        print(__doc__)
