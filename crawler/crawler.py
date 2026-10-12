from __future__ import annotations

import concurrent.futures
import datetime as dt
import ipaddress
import json
import os
import random
import re
import socket
import ssl
import struct
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Iterable

PUBLISH_URL = os.environ.get("PUBLISH_URL", "").rstrip("/")
PUBLISH_TOKEN = os.environ.get("PUBLISH_TOKEN", "")
SCAN_LIMIT = int(os.environ.get("SCAN_LIMIT", "90"))
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "14"))
TIMEOUT = float(os.environ.get("DNS_TIMEOUT", "4.0"))
HISTORY_LIMIT = 168

TARGET_DOMAINS = [
    "gemini.google.com",
    "aistudio.google.com",
    "notebooklm.google.com",
    "generativelanguage.googleapis.com",
]

SPECIAL_SOURCES = {
    "aigide-gemini": "https://aigide.me/gemini/dns/",
    "telegram-krohobor": "https://t.me/s/krohobortv/1507",
}

GENERIC_DOT_SOURCES = {
    "disposable-public-dns": "https://raw.githubusercontent.com/disposable/public-dns/main/txt/resolvers-dot.txt",
    "takuya-public-dns": "https://raw.githubusercontent.com/takuya/public_dns_list/master/dns-list-dot.txt",
}

GOOGLE_RANGES_URL = "https://www.gstatic.com/ipranges/goog.json"
HOST_RE = re.compile(r"(?<![A-Za-z0-9_-])(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}(?![A-Za-z0-9_-])")
IGNORE_SUFFIXES = (
    "google.com",
    "googleapis.com",
    "gstatic.com",
    "github.com",
    "githubusercontent.com",
    "telegram.org",
    "t.me",
    "aigide.me",
    "4pda.to",
    "reddit.com",
    "cloudflare.com",
)


@dataclass(frozen=True)
class Candidate:
    host: str
    sources: tuple[str, ...]
    special: bool = False


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def fetch_text(url: str, timeout: float = 12.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "GeminiDNSRadar/1.0 (+public monitoring project)"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def fetch_json(url: str, timeout: float = 12.0):
    return json.loads(fetch_text(url, timeout))


def api_json(path: str, method: str = "GET", payload=None):
    if not PUBLISH_URL or not PUBLISH_TOKEN:
        raise RuntimeError("PUBLISH_URL and PUBLISH_TOKEN must be configured")
    data = None
    headers = {"Authorization": f"Bearer {PUBLISH_TOKEN}"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"{PUBLISH_URL}{path}", data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=25) as response:
        return json.loads(response.read().decode("utf-8"))


def normalize_host(value: str) -> str | None:
    value = value.strip().lower().rstrip(".")
    if value.startswith("tls://"):
        value = value[6:]
    if "#" in value:
        host_part, tls_name = value.split("#", 1)
        if tls_name:
            value = tls_name
        else:
            value = host_part
    if ":" in value and value.count(":") == 1:
        host, maybe_port = value.rsplit(":", 1)
        if maybe_port.isdigit():
            value = host
    try:
        ipaddress.ip_address(value)
        return None
    except ValueError:
        pass
    if not HOST_RE.fullmatch(value):
        return None
    if value.endswith(IGNORE_SUFFIXES):
        return None
    return value


def hosts_from_dot_text(text: str) -> set[str]:
    result = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        host = normalize_host(line.split()[0])
        if host:
            result.add(host)
    return result


def hosts_from_page(text: str) -> set[str]:
    result = set()
    for raw in HOST_RE.findall(text):
        host = normalize_host(raw)
        if host:
            result.add(host)
    return result


def collect_candidates(previous: dict, queued: Iterable[str]) -> dict[str, Candidate]:
    mapping: dict[str, dict] = {}

    def add(host: str, source: str, special: bool):
        item = mapping.setdefault(host, {"sources": set(), "special": False})
        item["sources"].add(source)
        item["special"] = item["special"] or special

    for name, url in SPECIAL_SOURCES.items():
        try:
            for host in hosts_from_page(fetch_text(url)):
                add(host, name, True)
        except Exception as exc:
            print(f"source warning {name}: {exc}")

    for name, url in GENERIC_DOT_SOURCES.items():
        try:
            for host in hosts_from_dot_text(fetch_text(url)):
                add(host, name, False)
        except Exception as exc:
            print(f"source warning {name}: {exc}")

    for host in queued:
        host = normalize_host(host)
        if host:
            add(host, "user-queue", True)

    for host, old in previous.items():
        if normalize_host(host):
            for source in old.get("sources", ["history"]):
                add(host, source, bool(old.get("special_source")))

    return {
        host: Candidate(host, tuple(sorted(meta["sources"])), bool(meta["special"]))
        for host, meta in mapping.items()
    }


def build_query(domain: str, txid: int) -> bytes:
    header = struct.pack("!HHHHHH", txid, 0x0100, 1, 0, 0, 0)
    qname = b"".join(bytes([len(part)]) + part.encode("ascii") for part in domain.split(".")) + b"\x00"
    return header + qname + struct.pack("!HH", 1, 1)


def recv_exact(sock: ssl.SSLSocket, size: int) -> bytes:
    chunks = []
    left = size
    while left:
        chunk = sock.recv(left)
        if not chunk:
            raise ConnectionError("unexpected EOF")
        chunks.append(chunk)
        left -= len(chunk)
    return b"".join(chunks)


def skip_name(data: bytes, offset: int) -> int:
    while offset < len(data):
        length = data[offset]
        if length & 0xC0 == 0xC0:
            return offset + 2
        if length == 0:
            return offset + 1
        offset += length + 1
    return offset


def parse_a_records(data: bytes) -> list[str]:
    if len(data) < 12:
        return []
    _, _, qdcount, ancount, _, _ = struct.unpack("!HHHHHH", data[:12])
    offset = 12
    for _ in range(qdcount):
        offset = skip_name(data, offset)
        offset += 4
    result = []
    for _ in range(ancount):
        offset = skip_name(data, offset)
        if offset + 10 > len(data):
            break
        rtype, rclass, _, rdlength = struct.unpack("!HHIH", data[offset : offset + 10])
        offset += 10
        rdata = data[offset : offset + rdlength]
        offset += rdlength
        if rtype == 1 and rclass == 1 and len(rdata) == 4:
            result.append(str(ipaddress.IPv4Address(rdata)))
    return sorted(set(result))


def dot_resolve_many(host: str, domains: list[str]) -> tuple[dict[str, list[str]], int]:
    started = time.perf_counter()
    context = ssl.create_default_context()
    answers = {}
    with socket.create_connection((host, 853), timeout=TIMEOUT) as raw:
        raw.settimeout(TIMEOUT)
        with context.wrap_socket(raw, server_hostname=host) as tls:
            tls.settimeout(TIMEOUT)
            for idx, domain in enumerate(domains):
                packet = build_query(domain, (int(time.time() * 1000) + idx) & 0xFFFF)
                tls.sendall(struct.pack("!H", len(packet)) + packet)
                msg_len = struct.unpack("!H", recv_exact(tls, 2))[0]
                response = recv_exact(tls, msg_len)
                answers[domain] = parse_a_records(response)
    latency = round((time.perf_counter() - started) * 1000)
    return answers, latency


def google_networks() -> list[ipaddress._BaseNetwork]:
    data = fetch_json(GOOGLE_RANGES_URL)
    networks = []
    for item in data.get("prefixes", []):
        prefix = item.get("ipv4Prefix") or item.get("ipv6Prefix")
        if prefix:
            networks.append(ipaddress.ip_network(prefix))
    return networks


def is_google_ip(value: str, networks) -> bool:
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    return any(ip in network for network in networks if ip.version == network.version)


def probe(candidate: Candidate, networks) -> dict:
    checked_at = utc_now()
    try:
        answers, latency = dot_resolve_many(candidate.host, TARGET_DOMAINS)
        answered_domains = sum(1 for domain in TARGET_DOMAINS if answers.get(domain))
        proxy_domains = []
        non_google_ips = set()
        for domain in TARGET_DOMAINS:
            ips = answers.get(domain, [])
            outside = [ip for ip in ips if not is_google_ip(ip, networks)]
            if outside:
                proxy_domains.append(domain)
                non_google_ips.update(outside)

        dns_ok = answered_domains >= 3
        gemini_likely = dns_ok and len(proxy_domains) >= 2
        return {
            "checked_at": checked_at,
            "dns_ok": dns_ok,
            "gemini_likely": gemini_likely,
            "answered_domains": answered_domains,
            "proxy_domains": proxy_domains,
            "proxy_ip_count": len(non_google_ips),
            "proxy_ips": sorted(non_google_ips)[:8],
            "latency_ms": latency,
            "answers": answers,
            "error": None,
        }
    except Exception as exc:
        return {
            "checked_at": checked_at,
            "dns_ok": False,
            "gemini_likely": False,
            "answered_domains": 0,
            "proxy_domains": [],
            "proxy_ip_count": 0,
            "proxy_ips": [],
            "latency_ms": None,
            "answers": {},
            "error": f"{type(exc).__name__}: {exc}"[:300],
        }


def success_rate(history: list[dict], hours: int) -> tuple[float, int]:
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
    rows = []
    for item in history:
        try:
            when = dt.datetime.fromisoformat(item["checked_at"].replace("Z", "+00:00"))
        except Exception:
            continue
        if when >= cutoff:
            rows.append(item)
    if not rows:
        return 0.0, 0
    good = sum(1 for item in rows if item.get("gemini_likely"))
    return round(good / len(rows) * 100, 1), len(rows)


def consecutive_successes(history: list[dict]) -> int:
    count = 0
    for item in reversed(history):
        if item.get("gemini_likely"):
            count += 1
        else:
            break
    return count


def compute_score(record: dict) -> float:
    last = record.get("last") or {}
    if not last.get("gemini_likely"):
        return 0.0

    history = record.get("history", [])
    uptime24, count24 = success_rate(history, 24)
    uptime168, count168 = success_rate(history, 168)
    streak = consecutive_successes(history)
    latency = last.get("latency_ms") or 9999

    score = 45.0
    if record.get("special_source"):
        score += 12.0
    score += min(18.0, uptime24 * 0.18) if count24 else 0
    score += min(10.0, uptime168 * 0.10) if count168 else 0
    score += min(8.0, streak * 1.5)
    score += max(0.0, 5.0 - latency / 600.0)
    if len(last.get("proxy_domains", [])) >= 3:
        score += 2.0
    return round(min(100.0, score), 1)


def risk_label(record: dict) -> dict:
    history = record.get("history", [])
    uptime24, count24 = success_rate(history, 24)
    streak = consecutive_successes(history)
    if count24 < 4:
        return {"level": "unknown", "label": "Мало данных", "forecast": "Нужно накопить несколько почасовых проверок"}
    if uptime24 >= 95 and streak >= 6:
        return {"level": "low", "label": "Низкий", "forecast": "Признаков скорого отвала нет"}
    if uptime24 >= 75 and streak >= 2:
        return {"level": "medium", "label": "Средний", "forecast": "Возможны перебои в ближайшие 6–24 часа"}
    return {"level": "high", "label": "Высокий", "forecast": "Сервис нестабилен; возможен отвал в ближайшие часы"}


def public_view(record: dict) -> dict:
    history = record.get("history", [])
    uptime24, checks24 = success_rate(history, 24)
    uptime7d, checks7d = success_rate(history, 168)
    last = record.get("last") or {}
    return {
        "host": record["host"],
        "score": record.get("score", 0),
        "sources": record.get("sources", []),
        "special_source": record.get("special_source", False),
        "last_checked": last.get("checked_at"),
        "dns_ok": last.get("dns_ok", False),
        "gemini_likely": last.get("gemini_likely", False),
        "proxy_domains": last.get("proxy_domains", []),
        "proxy_ips": last.get("proxy_ips", []),
        "latency_ms": last.get("latency_ms"),
        "uptime_24h": uptime24,
        "checks_24h": checks24,
        "uptime_7d": uptime7d,
        "checks_7d": checks7d,
        "streak_hours": consecutive_successes(history),
        "risk": risk_label(record),
    }


def select_scan(candidates: dict[str, Candidate], previous: dict) -> list[Candidate]:
    must_scan = set()
    for host, record in previous.items():
        if record.get("last", {}).get("gemini_likely") or record.get("special_source"):
            must_scan.add(host)
    for host, cand in candidates.items():
        if cand.special or "user-queue" in cand.sources:
            must_scan.add(host)

    selected = [candidates[h] for h in sorted(must_scan) if h in candidates]
    remaining = [c for h, c in sorted(candidates.items()) if h not in must_scan]
    if remaining:
        hour_seed = int(time.time() // 3600)
        random.Random(hour_seed).shuffle(remaining)
    return (selected + remaining)[:SCAN_LIMIT]


def main():
    previous_state = api_json("/api/state")
    queue_payload = api_json("/api/queue")
    previous = previous_state.get("candidates", {})
    queued = queue_payload.get("queue", [])

    candidates = collect_candidates(previous, queued)
    networks = google_networks()
    scan_list = select_scan(candidates, previous)
    print(f"candidates={len(candidates)} scanning={len(scan_list)}")

    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        future_map = {pool.submit(probe, candidate, networks): candidate for candidate in scan_list}
        for future in concurrent.futures.as_completed(future_map):
            candidate = future_map[future]
            try:
                results[candidate.host] = future.result()
            except Exception as exc:
                results[candidate.host] = {
                    "checked_at": utc_now(),
                    "dns_ok": False,
                    "gemini_likely": False,
                    "error": str(exc),
                }

    new_records = {}
    for host, candidate in candidates.items():
        old = previous.get(host, {})
        history = list(old.get("history", []))[-HISTORY_LIMIT:]
        last = old.get("last")
        if host in results:
            last = results[host]
            history.append(last)
            history = history[-HISTORY_LIMIT:]
        record = {
            "host": host,
            "sources": sorted(set(old.get("sources", [])) | set(candidate.sources)),
            "special_source": bool(old.get("special_source") or candidate.special),
            "history": history,
            "last": last,
        }
        record["score"] = compute_score(record)
        new_records[host] = record

    ranked = [r for r in new_records.values() if (r.get("last") or {}).get("gemini_likely")]
    ranked.sort(key=lambda item: (item.get("score", 0), item.get("special_source", False)), reverse=True)
    top10 = [public_view(item) for item in ranked[:10]]

    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    state = {
        "version": 1,
        "updated_at": now.isoformat().replace("+00:00", "Z"),
        "next_expected_update": (now + dt.timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        "scan": {
            "candidate_count": len(candidates),
            "scanned_this_run": len(scan_list),
            "working_count": len(ranked),
            "top_count": len(top10),
            "method": "DoT port 853 + non-Google proxy IP detection for Gemini targets",
        },
        "top10": top10,
        "candidates": new_records,
    }

    result = api_json("/api/publish", method="POST", payload=state)
    print(json.dumps(result, ensure_ascii=False))
    for idx, item in enumerate(top10, 1):
        print(f"#{idx} {item['host']} score={item['score']} uptime24={item['uptime_24h']}%")


if __name__ == "__main__":
    main()
