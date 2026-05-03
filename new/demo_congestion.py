#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
SDN Demo 1: Gradual link congestion and recovery.
- Stage 0: all links clean.
- Stage 1: two switches congestion (delay 40ms, loss 5%) → orange.
- Stage 2: multi-switch congestion (delay 120ms, loss 15%) → red.
- Stage 3: recover step by step → green.

Edges used (inter‑domain):
  e1: s2↔s3  (2-3)
  e2: s4↔s5  (4-5)
  e3: s5↔s6  (5-6)
  e4: s7↔s8  (7-8)

Prerequisites:
1) server_agent.py running
2) controllers running (start_controllers.py start -n)
3) Mininet topology up and hosts learned (pingall)
4) Web UI open
"""

import json, time, datetime, urllib.request
from pathlib import Path

ROOT = "http://127.0.0.1:5000"
ART_DIR = Path("./demo_artifacts")

def http_req(method, path, json_data=None, timeout=5):
    url = ROOT + path
    data = None
    if json_data is not None:
        data = json.dumps(json_data).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "ignore"))

def http_get(path, timeout=3):
    return http_req("GET", path, timeout=timeout)

def inject_link_metrics(src, dst, delay_ms=None, loss_frac=None, bw_mbps=None):
    payload = {"src": str(src), "dst": str(dst)}
    if delay_ms is not None: payload["delay_ms"] = delay_ms
    if loss_frac is not None: payload["loss_frac"] = loss_frac
    if bw_mbps is not None: payload["bw_mbps"] = bw_mbps
    return http_req("POST", "/api/demo/inject_link_metrics", payload)

def clear_injected_metrics(src, dst):
    return inject_link_metrics(src, dst)  # body only src/dst → clear

def dump_snapshot(tag):
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = ART_DIR / f"{ts}_{tag}"
    out_dir.mkdir(parents=True, exist_ok=True)
    for ep in ["/api/statistics", "/api/graph"]:
        try:
            data = http_get(ep, timeout=6)
            (out_dir / f"{ep.split('/')[-1]}.json").write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            print(f"[{ep}] error: {e}")
    print(f"Snapshot saved: {out_dir}")

def main():
    ART_DIR.mkdir(parents=True, exist_ok=True)

    # Verify root ready
    try:
        if http_get("/api/health").get("status") != "ok":
            raise RuntimeError("Server not ready")
    except Exception as e:
        print(f"Root controller not ready: {e}")
        return

    # Edges to congest
    edges = [
        ("2", "3"),   # s2 - s3
        ("4", "5"),   # s4 - s5
        ("5", "6"),   # s5 - s6
        ("7", "8"),   # s7 - s8
    ]

    dump_snapshot("0_initial")

    # ---- Stage 1: mild congestion on first two edges ----
    print("\n=== Stage 1: Mild congestion (orange) on s2-s3 and s4-s5 ===")
    for src, dst in edges[:2]:
        inject_link_metrics(src, dst, delay_ms=40, loss_frac=0.05, bw_mbps=200)
    time.sleep(12)   # wait for frontend to refresh edge colors
    dump_snapshot("1_mild_congestion")

    # ---- Stage 2: severe congestion on all four edges ----
    print("\n=== Stage 2: Severe congestion (red) on all edges ===")
    for src, dst in edges:
        inject_link_metrics(src, dst, delay_ms=120, loss_frac=0.15, bw_mbps=50)
    time.sleep(12)
    dump_snapshot("2_severe_congestion")

    # ---- Stage 3: recover to mild ----
    print("\n=== Stage 3: Recover to mild (clear severe, reinject mild) ===")
    for src, dst in edges:
        clear_injected_metrics(src, dst)
    for src, dst in edges[:2]:
        inject_link_metrics(src, dst, delay_ms=40, loss_frac=0.05, bw_mbps=200)
    time.sleep(12)
    dump_snapshot("3_mild_again")

    # ---- Stage 4: full recovery ----
    print("\n=== Stage 4: Full recovery (clear all injections) ===")
    for src, dst in edges:
        clear_injected_metrics(src, dst)
    time.sleep(12)
    dump_snapshot("4_recovered")

    print("\nDemo complete. Check Web UI for colour changes: green → orange → red → orange → green.")

if __name__ == "__main__":
    main()
