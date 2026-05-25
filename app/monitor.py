"""
monitor.py — ping targets and report their status.

Responsibilities:
1. load_config()       -> read config.yaml
2. ping_target()       -> ping one host, return dict with status + latency
3. ping_all_targets()  -> ping every target in config, return list of results
"""

from icmplib import ping
from datetime import datetime
import yaml


def load_config(path: str = "config.yaml") -> dict:
    """Read config.yaml and return it as a dict."""
    with open(path, "r") as f:
        return yaml.safe_load(f)


def ping_target(host: str, timeout: int = 2) -> dict:
    """
    Ping a single host once. Returns a dict like:
        {"is_up": True, "latency_ms": 12.4, "error": None}
    Never raises — failures are returned as is_up=False.
    """
    try:
        result = ping(host, count=1, timeout=timeout, privileged=True)
        return {
            "is_up": result.is_alive,
            "latency_ms": round(result.avg_rtt, 2) if result.is_alive else None,
            "error": None,
        }
    except Exception as e:
        return {
            "is_up": False,
            "latency_ms": None,
            "error": str(e),
        }


def ping_all_targets(config: dict) -> list[dict]:
    """
    Ping every gateway and VM listed in config.
    Returns a list of result dicts, one per target.
    """
    timeout = config.get("ping_timeout_seconds", 2)
    results = []

    # Combine gateways and vms into one iterable list
    all_targets = config.get("gateways", []) + config.get("vms", [])

    for target in all_targets:
        ping_result = ping_target(target["host"], timeout=timeout)
        results.append({
            "name": target["name"],
            "host": target["host"],
            "type": target["type"],
            "is_up": ping_result["is_up"],
            "latency_ms": ping_result["latency_ms"],
            "error": ping_result["error"],
            "checked_at": datetime.now().isoformat(),
        })

    return results


# Standalone test runner — `python -m app.monitor`
if __name__ == "__main__":
    cfg = load_config()
    total = len(cfg.get("gateways", [])) + len(cfg.get("vms", []))
    print(f"Pinging {total} targets...\n")

    for r in ping_all_targets(cfg):
        status = "✅ UP  " if r["is_up"] else "❌ DOWN"
        latency = f"{r['latency_ms']}ms" if r["latency_ms"] is not None else "—"
        print(f"{status} {r['name']:30} {r['host']:18} {latency}")
