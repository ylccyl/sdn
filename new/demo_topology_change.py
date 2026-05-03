#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
SDN Demo 2: Topology changes (switch leave/return, two hosts down/up)
- Remove h13 and h12 via API → hosts disappear.
- Restore both hosts → they reappear.
- Detach switch s5 from its controller → switch & incident edges removed.
- Reattach s5 to its controller → switch & edges restored.

Prerequisites:
1) server_agent.py running (with demo endpoints and event logging)
2) controllers running
3) Mininet topology up, hosts learned (pingall)
4) Web UI open – manually refresh after each step to see changes
"""

import json, time, datetime, subprocess, urllib.request
from pathlib import Path

ROOT = "http://127.0.0.1:5000"
ART_DIR = Path("./demo_artifacts")

def sh(cmd, check=True):
    print(f"[CMD] {cmd}")
    p = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    out = p.stdout.decode("utf-8", "ignore")
    if check and p.returncode != 0:
        raise RuntimeError(f"Command failed rc={p.returncode}\n{cmd}\n{out}")
    return out

def http_req(method, path, json_data=None, timeout=5):
    url = ROOT + path
    data = None
    if json_data is not None:
        data = json.dumps(json_data).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")
        print(f"HTTP Error {e.code} for {method} {path}")
        print(f"Request data: {json_data}")
        print(f"Response body: {body}")
        raise

def http_get(path, timeout=3):
    return http_req("GET", path, timeout=timeout)

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

def get_host_info(ip):
    """Fetch MAC / dpid / port of a host from the current graph."""
    graph = http_get("/api/graph")
    for node in graph.get("nodes", []):
        if node["id"] == ip:
            data = node.get("data", {})
            return data.get("mac"), data.get("dpid"), data.get("port")
    return None, None, None

def main():
    ART_DIR.mkdir(parents=True, exist_ok=True)

    # Check server
    try:
        if http_get("/api/health").get("status") != "ok":
            raise RuntimeError("Server not ok")
    except Exception as e:
        print(f"Root controller not ready: {e}")
        return

    # Two hosts to manipulate: h12 (10.0.0.12) and h13 (10.0.0.13)
    hosts = [
        {"ip": "10.0.0.12", "fallback_dpid": 5, "fallback_port": 12},
        {"ip": "10.0.0.13", "fallback_dpid": 5, "fallback_port": 13},
    ]

    # Collect host info before any changes
    host_data = []
    for h in hosts:
        ip = h["ip"]
        mac, dpid, port = get_host_info(ip)
        if mac is None:
            print(f"Host {ip} not found in graph. Run pingall and ensure topology is complete.")
            return
        if dpid is None:
            dpid = h["fallback_dpid"]
        if port is None:
            port = h["fallback_port"]
        host_data.append({"ip": ip, "mac": mac, "dpid": int(dpid), "port": int(port)})
        print(f"Host {ip}: switch {dpid}, port {port}, MAC {mac}")

    dump_snapshot("0_initial")

    # ========== Host Down (both) ==========
    input("\nPress Enter to remove h12 and h13 (both will disappear)...")
    for h in host_data:
        resp = http_req("POST", "/api/demo/host_down", {"ip": h["ip"]})
        print(f"Removed {h['ip']}: {resp}")
    print(">>> Please MANUALLY REFRESH the web page NOW to see the hosts disappear!")
    time.sleep(5)
    dump_snapshot("1_two_hosts_down")

    # ========== Host Up (both) ==========
    input("\nPress Enter to restore both hosts...")
    for h in host_data:
        resp = http_req("POST", "/api/demo/host_up", {
            "ip": h["ip"],
            "dpid": h["dpid"],
            "port": h["port"],
            "mac": h["mac"]
        })
        print(f"Restored {h['ip']}: {resp}")
    print(">>> MANUALLY REFRESH the page NOW – both hosts should reappear!")
    time.sleep(5)
    dump_snapshot("2_two_hosts_up")

    # ========== Switch Leave ==========
    input("\nPress Enter to detach switch s5 from its controller...")
    sh("ovs-vsctl del-controller s5")
    print("Controller removed from s5. Waiting 30 seconds for topology update...")
    print(">>> After 30s, refresh the page – s5 and its links will vanish.")
    time.sleep(30)
    dump_snapshot("3_switch_leave")

    # ========== Switch Return ==========
    input("\nPress Enter to reattach s5...")
    sh("ovs-vsctl set-controller s5 tcp:127.0.0.1:6656")
    print("Controller reconnected. Waiting 30 seconds for topology to rebuild...")
    print(">>> Refresh the page after 30s – s5 and its edges should be back.")
    time.sleep(30)
    dump_snapshot("4_switch_return")

    print("\nDemo finished. Check web UI and event log panel.")

if __name__ == "__main__":
    main()
