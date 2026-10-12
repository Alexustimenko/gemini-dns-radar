import base64
import datetime as dt
import json
import re
import struct
import time
from urllib.parse import parse_qs, urlparse

from js import Uint8Array
from workers import Response, WorkerEntrypoint, fetch

STATE_KEY = "radar_state_v1"
QUEUE_KEY = "manual_queue_v1"
SCHEDULER_KEY = "scheduler_status_v1"
GITHUB_DISPATCH_URL = "https://api.github.com/repos/Alexustimenko/gemini-dns-radar/actions/workflows/refresh.yml/dispatches"
PRIMARY_CRON = "7 * * * *"
WATCHDOG_CRON = "*/10 * * * *"
WATCHDOG_STALE_MINUTES = 65
HOST_RE = re.compile(r"^(?=.{1,253}$)(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,63}$")
MANUAL_DOMAINS = [
    "gemini.google.com",
    "aistudio.google.com",
    "notebooklm.google.com",
]
BASELINE_DOH = "https://cloudflare-dns.com/dns-query"


def _json_response(data, status=200):
    return Response(
        json.dumps(data, ensure_ascii=False),
        status=status,
        headers={
            "content-type": "application/json; charset=utf-8",
            "cache-control": "no-store",
            "x-content-type-options": "nosniff",
        },
    )


def _authorized(request, env):
    configured = str(getattr(env, "PUBLISH_TOKEN", ""))
    if not configured:
        return False
    header = request.headers.get("Authorization") or ""
    return header == f"Bearer {configured}"


def _build_dns_query(domain):
    txid = int(time.time() * 1000) & 0xFFFF
    header = struct.pack("!HHHHHH", txid, 0x0100, 1, 0, 0, 0)
    qname = b"".join(bytes([len(label)]) + label.encode("ascii") for label in domain.split(".")) + b"\x00"
    return header + qname + struct.pack("!HH", 1, 1)


def _skip_name(data, offset):
    while offset < len(data):
        length = data[offset]
        if length & 0xC0 == 0xC0:
            return offset + 2
        if length == 0:
            return offset + 1
        offset += length + 1
    return offset


def _parse_a_records(data):
    if len(data) < 12:
        return []
    _, _, qdcount, ancount, _, _ = struct.unpack("!HHHHHH", data[:12])
    offset = 12
    for _ in range(qdcount):
        offset = _skip_name(data, offset)
        if offset + 4 > len(data):
            return []
        offset += 4

    records = []
    for _ in range(ancount):
        offset = _skip_name(data, offset)
        if offset + 10 > len(data):
            break
        rtype, rclass, _, rdlength = struct.unpack("!HHIH", data[offset : offset + 10])
        offset += 10
        rdata = data[offset : offset + rdlength]
        offset += rdlength
        if rtype == 1 and rclass == 1 and len(rdata) == 4:
            records.append(".".join(str(x) for x in rdata))
    return sorted(set(records))


async def _doh_query(endpoint, domain):
    packet = _build_dns_query(domain)
    encoded = base64.urlsafe_b64encode(packet).decode("ascii").rstrip("=")
    sep = "&" if "?" in endpoint else "?"
    response = await fetch(
        f"{endpoint}{sep}dns={encoded}",
        headers={"Accept": "application/dns-message"},
        cache="no-store",
    )
    if response.status < 200 or response.status >= 300:
        raise RuntimeError(f"DoH HTTP {response.status}")
    buffer = await response.arrayBuffer()
    raw = bytes(Uint8Array.new(buffer).to_py())
    return _parse_a_records(raw)


async def _manual_probe(host):
    candidate_endpoint = f"https://{host}/dns-query"
    started = time.perf_counter()
    changed = 0
    answered = 0
    details = {}

    for domain in MANUAL_DOMAINS:
        try:
            candidate = await _doh_query(candidate_endpoint, domain)
            baseline = await _doh_query(BASELINE_DOH, domain)
            if candidate:
                answered += 1
            if candidate and baseline and candidate != baseline:
                changed += 1
            details[domain] = {"candidate": candidate, "baseline": baseline}
        except Exception as exc:
            details[domain] = {"error": str(exc)}

    elapsed = round((time.perf_counter() - started) * 1000)
    return {
        "host": host,
        "doh_endpoint": candidate_endpoint,
        "doh_works": answered >= 2,
        "smartdns_signal": changed >= 2,
        "changed_domains": changed,
        "answered_domains": answered,
        "latency_ms": elapsed,
        "details": details,
        "note": "Это быстрая DoH-проверка. Основной рейтинг строится hourly crawler'ом через Android Private DNS / DoT.",
    }


def _state_age_minutes(state):
    updated_at = state.get("updated_at") if isinstance(state, dict) else None
    if not updated_at:
        return None
    try:
        updated = dt.datetime.fromisoformat(str(updated_at).replace("Z", "+00:00"))
        now = dt.datetime.now(dt.timezone.utc)
        return max(0.0, (now - updated).total_seconds() / 60)
    except Exception:
        return None


async def _dispatch_github(env, reason, cron="manual-test"):
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    status = {
        "checked_at": now,
        "cron": cron,
        "dispatched": False,
        "reason": reason,
    }

    token = str(getattr(env, "GITHUB_DISPATCH_TOKEN", ""))
    if not token:
        status["error"] = "GITHUB_DISPATCH_TOKEN is not configured"
        await env.STATE.put(SCHEDULER_KEY, json.dumps(status, ensure_ascii=False))
        return status

    response = await fetch(
        GITHUB_DISPATCH_URL,
        method="POST",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "GeminiDNSRadar-Cloudflare-Scheduler/1.0",
            "Content-Type": "application/json",
        },
        body=json.dumps({"ref": "main", "inputs": {"source": "cloudflare"}}),
    )

    status["github_status"] = int(response.status)
    if 200 <= response.status < 300:
        status["dispatched"] = True
        status["reason"] = reason + "; GitHub workflow dispatched"
    else:
        body = await response.text()
        status["error"] = f"GitHub dispatch HTTP {response.status}: {str(body)[:500]}"

    await env.STATE.put(SCHEDULER_KEY, json.dumps(status, ensure_ascii=False))
    return status


class Default(WorkerEntrypoint):
    async def scheduled(self, controller, env, ctx):
        env = env or self.env
        cron = str(controller.cron)
        now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

        raw = await env.STATE.get(STATE_KEY)
        state = json.loads(str(raw)) if raw else {}
        age_minutes = _state_age_minutes(state)

        should_dispatch = cron == PRIMARY_CRON
        reason = "primary hourly trigger"

        if cron == WATCHDOG_CRON:
            should_dispatch = age_minutes is None or age_minutes >= WATCHDOG_STALE_MINUTES
            if age_minutes is None:
                reason = "watchdog: no successful state yet"
            elif should_dispatch:
                reason = f"watchdog: state is stale ({age_minutes:.1f} min)"
            else:
                reason = f"watchdog: state still fresh ({age_minutes:.1f} min)"

        if not should_dispatch:
            status = {
                "checked_at": now,
                "cron": cron,
                "state_age_minutes": round(age_minutes, 1) if age_minutes is not None else None,
                "dispatched": False,
                "reason": reason,
            }
            await env.STATE.put(SCHEDULER_KEY, json.dumps(status, ensure_ascii=False))
            return

        status = await _dispatch_github(env, reason, cron)
        status["state_age_minutes"] = round(age_minutes, 1) if age_minutes is not None else None
        await env.STATE.put(SCHEDULER_KEY, json.dumps(status, ensure_ascii=False))
        if status.get("error"):
            raise RuntimeError(status["error"])

    async def fetch(self, request):
        url = urlparse(request.url)
        path = url.path

        if path == "/api/top" and request.method == "GET":
            raw = await self.env.STATE.get(STATE_KEY)
            scheduler_raw = await self.env.STATE.get(SCHEDULER_KEY)
            scheduler = json.loads(str(scheduler_raw)) if scheduler_raw else {}
            if raw is None:
                return _json_response({"updated_at": None, "top10": [], "status": "waiting_for_first_scan", "scheduler": scheduler})
            state = json.loads(str(raw))
            return _json_response(
                {
                    "updated_at": state.get("updated_at"),
                    "next_expected_update": state.get("next_expected_update"),
                    "scan": state.get("scan", {}),
                    "top10": state.get("top10", []),
                    "scheduler": scheduler,
                }
            )

        if path == "/api/scheduler" and request.method == "GET":
            raw = await self.env.STATE.get(SCHEDULER_KEY)
            if raw is None:
                return _json_response({"status": "waiting_for_first_cron"})
            return Response(str(raw), headers={"content-type": "application/json; charset=utf-8", "cache-control": "no-store"})

        if path == "/api/scheduler/test" and request.method == "POST":
            if not _authorized(request, self.env):
                return _json_response({"error": "unauthorized"}, 401)
            status = await _dispatch_github(self.env, "manual scheduler diagnostic")
            return _json_response(status, 200 if status.get("dispatched") else 502)

        if path == "/api/state" and request.method == "GET":
            if not _authorized(request, self.env):
                return _json_response({"error": "unauthorized"}, 401)
            raw = await self.env.STATE.get(STATE_KEY)
            if raw is None:
                return _json_response({"version": 1, "candidates": {}, "top10": []})
            return Response(str(raw), headers={"content-type": "application/json; charset=utf-8", "cache-control": "no-store"})

        if path == "/api/queue" and request.method == "GET":
            if not _authorized(request, self.env):
                return _json_response({"error": "unauthorized"}, 401)
            raw = await self.env.STATE.get(QUEUE_KEY)
            queue = json.loads(str(raw)) if raw else []
            return _json_response({"queue": queue})

        if path == "/api/publish" and request.method == "POST":
            if not _authorized(request, self.env):
                return _json_response({"error": "unauthorized"}, 401)
            try:
                payload = (await request.text())
                state = json.loads(str(payload))
                if not isinstance(state.get("top10", []), list) or not isinstance(state.get("candidates", {}), dict):
                    raise ValueError("invalid state shape")
                await self.env.STATE.put(STATE_KEY, json.dumps(state, ensure_ascii=False))
                await self.env.STATE.put(QUEUE_KEY, "[]")
                return _json_response({"ok": True, "top_count": len(state.get("top10", []))})
            except Exception as exc:
                return _json_response({"error": str(exc)}, 400)

        if path == "/api/check" and request.method == "GET":
            params = parse_qs(url.query)
            host = params.get("dns", [""])[0].strip().lower().rstrip(".")
            if not HOST_RE.match(host):
                return _json_response({"error": "Введите hostname вида dns.example.com"}, 400)

            raw = await self.env.STATE.get(STATE_KEY)
            if raw:
                state = json.loads(str(raw))
                known = state.get("candidates", {}).get(host)
                if known and known.get("last"):
                    return _json_response({"source": "hourly-dot-scan", "result": known})

            queue_raw = await self.env.STATE.get(QUEUE_KEY)
            queue = json.loads(str(queue_raw)) if queue_raw else []
            if host not in queue:
                queue = (queue + [host])[-100:]
                await self.env.STATE.put(QUEUE_KEY, json.dumps(queue))

            try:
                result = await _manual_probe(host)
                return _json_response({"source": "instant-doh-probe", "queued_for_dot_scan": True, "result": result})
            except Exception as exc:
                return _json_response(
                    {
                        "source": "instant-doh-probe",
                        "queued_for_dot_scan": True,
                        "result": {"host": host, "doh_works": False, "smartdns_signal": False, "error": str(exc)},
                    }
                )

        return _json_response({"error": "not found"}, 404)
