#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
SDN platform demo/acceptance runner (for thesis presentation).

Prerequisites (you do these manually in 4 terminals):
1) conda activate sdn
2) python3.6 server_agent.py
3) python3.6 start_controllers.py start -n
4) sudo python3.6 create_complex_topo.py  (keep the Mininet running)

Then open a NEW terminal and run:
  sudo python3.6 demo_runner.py

What it does:
- Poll Root REST APIs and save snapshots into ./demo_artifacts/
- Generate traffic (ping + iperf) between selected hosts
- Apply netem delay/loss on one switch-switch link interface
- Flap a core link down/up
- Bring a host interface down/up
"""

import os
import json
import time
import datetime
import subprocess
import urllib.request
import urllib.error
from pathlib import Path

ROOT = os.environ.get("SDN_ROOT_URL", "http://127.0.0.1:5000")
ART_DIR = Path("./demo_artifacts")

# Topology choices based on your mininet dump:
# linear-ish backbone with a special s2<->s3 link on eth20, and s5<->s6 via eth21/eth20, s8<->s9 via eth21/eth20
# We'll use:
#  - traffic: h1 <-> h26 (goes across many switches)
#  - netem: apply to s2-eth20 (one side of s2<->s3)
#  - flap link: s8-eth21 <-> s9-eth20 (mid-network) using ip link down/up
#  - host flap: h13-eth1 down/up

def sh(cmd, check=True):
    print(f"[CMD] {cmd}")
    p = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    out = p.stdout.decode("utf-8", "ignore")
    if check and p.returncode != 0:
        raise RuntimeError(f"Command failed rc={p.returncode}\n{cmd}\n{out}")
    return out

def http_get(path, timeout=3):
    url = ROOT + path
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "ignore"))

def wait_root_ready(max_wait=40):
    t0 = time.time()
    last_err = None
    while time.time() - t0 < max_wait:
        try:
            h = http_get("/api/health", timeout=2)
            if h.get("status") == "ok":
                return True
        except Exception as e:
            last_err = e
        time.sleep(1)
    print(f"[WARN] Root not ready after {max_wait}s, last_err={last_err}")
    return False

def dump_snapshot(tag):
    """Save multiple API snapshots into a timestamped directory."""
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = ART_DIR / f"{ts}_{tag}"
    out_dir.mkdir(parents=True, exist_ok=True)

    def save(name, data):
        (out_dir / name).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n=== SNAPSHOT {tag} -> {out_dir} ===")
    for ep, fn in [
        ("/api/health", "health.json"),
        ("/api/statistics", "statistics.json"),
        ("/api/topo", "topo.json"),
        ("/api/graph", "graph.json"),
    ]:
        try:
            data = http_get(ep, timeout=6)
            save(fn, data)
            if ep == "/api/graph":
                nodes = data.get("nodes", [])
                edges = data.get("edges", [])
                type_count = {}
                for n in nodes:
                    nt = (n.get("data") or {}).get("node_type", "unknown")
                    type_count[nt] = type_count.get(nt, 0) + 1
                print(f"graph nodes={len(nodes)} edges={len(edges)} types={type_count}")
            else:
                print(f"saved {ep} -> {fn}")
        except Exception as e:
            print(f"[SNAPSHOT-ERR] {ep}: {e}")
    print("=== END SNAPSHOT ===\n")

def mn(host, cmd, check=True):
    # Use `mnexec -a <pid>` to run inside a Mininet host namespace (no need to control Mininet CLI).
    # We can discover pid via `ps`? In your dump you had PIDs, but they change every run.
    # So we fetch pid dynamically with `pgrep -f 'mininet:h1'` style: mininet creates processes with name 'bash' etc.
    # Most robust: use `mnexec` with `-a $(pgrep -f "mininet:${host}")` doesn't always work.
    #
    # Instead, Mininet provides `pgrep -f "mnexec.*<host>"` is unreliable.
    #
    # Practical approach for your environment: Mininet creates host processes with namespace name,
    # but easiest is to use `ip netns` if your mininet uses netns (it does).
    #
    # We'll use: `ip netns exec <host> <cmd>` (Mininet usually creates netns with name like "h1").
    # If your setup doesn't, you'll see error; then tell me and I’ll adjust to mnexec pid mode.

    full = f"ip netns exec {host} bash -lc {json.dumps(cmd)}"
    return sh(full, check=check)

def tc_netem(dev, delay_ms=40, loss_pct=5):
    sh(f"tc qdisc del dev {dev} root || true", check=False)
    sh(f"tc qdisc add dev {dev} root netem delay {delay_ms}ms loss {loss_pct}%")

def tc_clear(dev):
    sh(f"tc qdisc del dev {dev} root || true", check=False)

def link_down(dev):
    sh(f"ip link set dev {dev} down")

def link_up(dev):
    sh(f"ip link set dev {dev} up")

def main():
    ART_DIR.mkdir(parents=True, exist_ok=True)

    print(f"[INFO] Root URL: {ROOT}")
    if not wait_root_ready():
        print("[FATAL] Root API not ready. Ensure server_agent.py is running.")
        return

    print("[INFO] Quick API check ok.")
    dump_snapshot("00_initial")

    # 1) Warm up ARP + baseline ping (end-to-end)
    print("Step1: baseline pings (expect paths discovered / host learning).")
    mn("h1", "ping -c 3 10.0.0.26 || true", check=False)
    mn("h13", "ping -c 3 10.0.0.14 || true", check=False)
    time.sleep(3)
    dump_snapshot("01_after_ping")

    # 2) Generate throughput with iperf (traffic should make port stats change)
    print("Step2: iperf traffic (throughput).")
    # start server on h26
    mn("h26", "pkill -f 'iperf -s' || true; iperf -s -p 5001 >/tmp/iperf_s.log 2>&1 &", check=False)
    time.sleep(1)
    # client from h1 and h9 in parallel-ish
    mn("h1", "iperf -c 10.0.0.26 -p 5001 -t 8 -i 1 >/tmp/iperf_c_h1.log 2>&1 || true", check=False)
    mn("h9", "iperf -c 10.0.0.26 -p 5001 -t 8 -i 1 >/tmp/iperf_c_h9.log 2>&1 || true", check=False)
    time.sleep(2)
    dump_snapshot("02_after_iperf")

    # 3) Add delay/loss on a backbone link interface to guarantee non-zero metrics and trigger color change
    # Choose s2-eth20 which connects to s3-eth20
    print("Step3: apply netem delay/loss on s2-eth20 (should reflect in link metrics/weight).")
    tc_netem("s2-eth20", delay_ms=60, loss_pct=8)
    time.sleep(20)

    # re-run ping under impairment
    mn("h4", "ping -c 6 10.0.0.8 || true", check=False)
    time.sleep(2)
    dump_snapshot("03_after_netem")

    # 4) Flap a core link to trigger topo change
    # s8-eth21 <-> s9-eth20
    print("Step4: flap link between s8 and s9 (down 8s then up). Expect edge removal/addition in /api/graph.")
    link_down("s8-eth21")
    link_down("s9-eth20")
    time.sleep(8)
    dump_snapshot("04_link_down")

    link_up("s8-eth21")
    link_up("s9-eth20")
    time.sleep(8)
    dump_snapshot("05_link_up")

    # 5) Host interface down/up (simulate host offline/online)
    print("Step5: host flap h13-eth1 down/up. Expect host disappear/appear (depending on your host aging logic).")
    mn("h13", "ip link set dev h13-eth1 down", check=False)
    time.sleep(6)
    dump_snapshot("06_h13_down")
    mn("h13", "ip link set dev h13-eth1 up", check=False)
    time.sleep(6)
    dump_snapshot("07_h13_up")

    # cleanup netem
    print("Cleanup: clear tc qdisc on s2-eth20.")
    tc_clear("s2-eth20")

    dump_snapshot("99_final")
    print("\n[OK] Demo script finished. Artifacts saved under ./demo_artifacts/")
    print("Open your web UI during the run to narrate: topology change + metrics change + link status change.\n")

if __name__ == "__main__":
    main()
