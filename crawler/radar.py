from __future__ import annotations

import copy
import datetime as dt
import importlib.util
import json
import os
import sys
import urllib.request
from pathlib import Path

# Cloudflare can be stricter with requests coming from default Python urllib User-Agent.
opener = urllib.request.build_opener()
opener.addheaders = [
    ("User-Agent", "GeminiDNSRadar/1.0"),
    ("Accept", "application/json"),
]
urllib.request.install_opener(opener)

CORE_PATH = Path(__file__).with_name("crawler.py")
spec = importlib.util.spec_from_file_location("gemini_dns_crawler_core", CORE_PATH)
assert spec and spec.loader
core = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = core
spec.loader.exec_module(core)

BASE_SCAN_LIMIT = int(os.environ.get("SCAN_LIMIT", "90"))
EMERGENCY_SCAN_LIMIT = int(os.environ.get("EMERGENCY_SCAN_LIMIT", "320"))


def normalize_state(state: dict) -> tuple[dict, bool]:
    """Make state written by older crawler versions safe for the current crawler."""
    if not isinstance(state, dict):
        return {"version": 1, "candidates": {}, "top10": []}, True

    changed = False
    candidates = state.get("candidates")
    if not isinstance(candidates, dict):
        candidates = {}
        state["candidates"] = candidates
        changed = True

    for host, record in list(candidates.items()):
        if not isinstance(record, dict):
            candidates[host] = {
                "host": host,
                "sources": [],
                "special_source": False,
                "history": [],
                "last": {},
                "score": 0,
            }
            changed = True
            continue

        if not isinstance(record.get("last"), dict):
            record["last"] = {}
            changed = True

        if not isinstance(record.get("history"), list):
            record["history"] = []
            changed = True

        if not isinstance(record.get("sources"), list):
            record["sources"] = []
            changed = True

        if "host" not in record:
            record["host"] = host
            changed = True

        if "special_source" not in record:
            record["special_source"] = False
            changed = True

        if "score" not in record:
            record["score"] = 0
            changed = True

    if not isinstance(state.get("top10"), list):
        state["top10"] = []
        changed = True

    return state, changed


# Keep the state just published by crawler.py in memory. Reading it back from
# Cloudflare immediately after a write is unnecessary and was causing transient
# 500/503 failures to abort otherwise successful scans.
_original_api_json = core.api_json
_cached_state: dict | None = None


def safe_api_json(path: str, method: str = "GET", payload=None):
    global _cached_state

    if path == "/api/state" and method == "GET" and _cached_state is not None:
        return copy.deepcopy(_cached_state)

    data = _original_api_json(path, method=method, payload=payload)

    if path == "/api/state" and method == "GET":
        normalized, _ = normalize_state(data)
        _cached_state = copy.deepcopy(normalized)
        return normalized

    if path == "/api/publish" and method == "POST" and isinstance(payload, dict):
        normalized, _ = normalize_state(copy.deepcopy(payload))
        _cached_state = normalized

    return data


core.api_json = safe_api_json


def working_hosts(state: dict) -> set[str]:
    result: set[str] = set()
    normalized, _ = normalize_state(state)
    for host, record in normalized.get("candidates", {}).items():
        if (record.get("last") or {}).get("gemini_likely"):
            result.add(host)
    return result


def build_radar(previous: dict, current: dict, emergency_used: bool, base_scanned: int) -> dict:
    before = working_hosts(previous)
    after = working_hosts(current)

    failed = sorted(before - after)
    recovered_or_new = sorted(after - before)
    retained = sorted(before & after)

    before_count = len(before)
    after_count = len(after)
    failure_ratio = (len(failed) / before_count) if before_count else 0.0

    if before_count == 0:
        level = "learning"
        label = "LEARNING"
        title = "Набираем базовую линию"
        message = "Система только начала накапливать историю. После нескольких почасовых запусков Radar сможет уверенно отличать массовый сбой от обычной ротации DNS."
    elif before_count >= 3 and (after_count == 0 or failure_ratio >= 0.60):
        level = "critical"
        label = "MASS OUTAGE"
        title = "Обнаружен массовый отвал"
        message = f"За один цикл перестали подтверждаться {len(failed)} из {before_count} ранее рабочих DNS. Включён аварийный поиск новых вариантов."
    elif before_count >= 3 and failure_ratio >= 0.30:
        level = "degraded"
        label = "INSTABILITY"
        title = "Обнаружена нестабильность"
        message = f"Потеряно {len(failed)} из {before_count} ранее рабочих DNS. Radar расширяет поиск кандидатов."
    elif failed:
        level = "watch"
        label = "WATCH"
        title = "Есть локальные изменения"
        message = f"Отвалилось DNS: {len(failed)}. Новых или восстановившихся: {len(recovered_or_new)}. Массового сбоя пока не видно."
    else:
        level = "normal"
        label = "NORMAL"
        title = "Система стабильна"
        message = "Массовых отказов среди ранее подтверждённых DNS не обнаружено."

    old_outage = ((previous.get("scan") or {}).get("outage") or {})
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    if level in {"critical", "degraded"}:
        if old_outage.get("level") in {"critical", "degraded"}:
            detected_at = old_outage.get("detected_at") or now
        else:
            detected_at = now
    else:
        detected_at = None

    return {
        "level": level,
        "label": label,
        "title": title,
        "message": message,
        "detected_at": detected_at,
        "previous_working": before_count,
        "working_now": after_count,
        "failed_count": len(failed),
        "new_count": len(recovered_or_new),
        "retained_count": len(retained),
        "failure_percent": round(failure_ratio * 100, 1),
        "failed_hosts": failed[:12],
        "new_hosts": recovered_or_new[:12],
        "emergency_mode": emergency_used,
        "base_scanned": base_scanned,
    }


def emergency_needed(previous: dict, current: dict) -> bool:
    before = working_hosts(previous)
    after = working_hosts(current)
    if len(before) < 3:
        return False
    failed = len(before - after)
    ratio = failed / len(before)
    return len(after) == 0 or ratio >= 0.30


def attach_radar(previous: dict, state: dict, emergency_used: bool, base_scanned: int) -> dict:
    scan = dict(state.get("scan") or {})
    radar = build_radar(previous, state, emergency_used, base_scanned)

    scan["mode"] = "emergency" if emergency_used else "normal"
    scan["scan_limit"] = EMERGENCY_SCAN_LIMIT if emergency_used else BASE_SCAN_LIMIT
    scan["outage"] = radar
    scan["changes"] = {
        "failed": radar["failed_hosts"],
        "new": radar["new_hosts"],
        "failed_count": radar["failed_count"],
        "new_count": radar["new_count"],
    }

    state["scan"] = scan
    return state


def main() -> None:
    global _cached_state

    previous_raw = _original_api_json("/api/state")
    previous, changed = normalize_state(previous_raw)
    _cached_state = copy.deepcopy(previous)

    # Persist the cleanup too, but crawler safety does not depend on this write.
    if changed:
        print("Radar: legacy state detected; normalizing before scan")
        _original_api_json("/api/publish", method="POST", payload=previous)

    core.SCAN_LIMIT = BASE_SCAN_LIMIT
    print(f"Radar mode=normal scan_limit={BASE_SCAN_LIMIT}")
    core.main()

    # Served from the in-process cache populated by the successful publish above.
    first_state = core.api_json("/api/state")
    base_scanned = int((first_state.get("scan") or {}).get("scanned_this_run", 0))

    emergency_used = emergency_needed(previous, first_state)

    if emergency_used:
        print(
            "OUTAGE RADAR: instability detected; "
            f"switching to emergency scan_limit={EMERGENCY_SCAN_LIMIT}"
        )
        core.SCAN_LIMIT = max(BASE_SCAN_LIMIT, EMERGENCY_SCAN_LIMIT)
        core.main()
        final_state = core.api_json("/api/state")
    else:
        final_state = first_state

    final_state = attach_radar(previous, final_state, emergency_used, base_scanned)
    result = _original_api_json("/api/publish", method="POST", payload=final_state)

    outage = final_state["scan"]["outage"]
    print(
        "OUTAGE RADAR: "
        f"{outage['label']} | previous={outage['previous_working']} "
        f"now={outage['working_now']} failed={outage['failed_count']} "
        f"new={outage['new_count']} mode={final_state['scan']['mode']}"
    )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
