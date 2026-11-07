// Lightweight API: keep the full DNS history in KV, never parse it inside a Worker.
const STATE_KEY = "radar_state_v1";
const PUBLIC_KEY = "radar_public_v1";
const QUEUE_KEY = "manual_queue_v1";
const SCHEDULER_KEY = "scheduler_status_v1";
const GITHUB_DISPATCH_URL = "https://api.github.com/repos/Alexustimenko/gemini-dns-radar/actions/workflows/refresh.yml/dispatches";
const PRIMARY_CRON = "7 * * * *";
const WATCHDOG_CRON = "*/10 * * * *";
const WATCHDOG_STALE_MINUTES = 65;
const CHECK_DOMAINS = ["gemini.google.com", "aistudio.google.com", "notebooklm.google.com"];
const BASELINE_DOH = "https://cloudflare-dns.com/dns-query";

const JSON_HEADERS = {
  "content-type": "application/json; charset=utf-8",
  "cache-control": "no-store",
  "x-content-type-options": "nosniff",
};

function jsonResponse(data, status = 200) {
  return new Response(JSON.stringify(data), { status, headers: JSON_HEADERS });
}

function authorized(request, env) {
  return Boolean(env.PUBLISH_TOKEN) &&
    request.headers.get("authorization") === "Bearer " + env.PUBLISH_TOKEN;
}

function stateAgeMinutes(snapshot) {
  const timestamp = Date.parse(snapshot?.updated_at ?? "");
  if (!Number.isFinite(timestamp)) return null;
  return Math.max(0, (Date.now() - timestamp) / 60000);
}

async function dispatchGitHub(env, reason, cron = "manual-test") {
  const status = {
    checked_at: new Date().toISOString(),
    cron,
    dispatched: false,
    reason,
  };
  if (!env.GITHUB_DISPATCH_TOKEN) {
    status.error = "GITHUB_DISPATCH_TOKEN is not configured";
    return status;
  }

  try {
    const response = await fetch(GITHUB_DISPATCH_URL, {
      method: "POST",
      headers: {
        "accept": "application/vnd.github+json",
        "authorization": "Bearer " + env.GITHUB_DISPATCH_TOKEN,
        "x-github-api-version": "2022-11-28",
        "user-agent": "GeminiDNSRadar-Cloudflare-Scheduler/2.0",
        "content-type": "application/json",
      },
      body: JSON.stringify({ ref: "main", inputs: { source: "cloudflare" } }),
    });
    status.github_status = response.status;
    if (response.ok) {
      status.dispatched = true;
      status.reason += "; GitHub workflow dispatched";
    } else {
      status.error = "GitHub dispatch HTTP " + response.status +
        ": " + (await response.text()).slice(0, 300);
    }
  } catch (error) {
    status.error = "GitHub dispatch network error: " + String(error);
  }
  return status;
}

function validHostname(host) {
  return host.length <= 253 &&
    /^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$/.test(host);
}

function dnsPacket(domain) {
  const labels = domain.split(".");
  const length = 12 + labels.reduce((total, label) => total + 1 + label.length, 0) + 1 + 4;
  const buffer = new Uint8Array(length);
  const view = new DataView(buffer.buffer);
  view.setUint16(0, Math.floor(Math.random() * 65536));
  view.setUint16(2, 0x0100);
  view.setUint16(4, 1);
  let pos = 12;
  for (const label of labels) {
    buffer[pos++] = label.length;
    for (let i = 0; i < label.length; i++) buffer[pos++] = label.charCodeAt(i);
  }
  buffer[pos++] = 0;
  view.setUint16(pos, 1);
  view.setUint16(pos + 2, 1);
  return buffer;
}

function skipDnsName(bytes, pos) {
  while (pos < bytes.length) {
    const n = bytes[pos];
    if ((n & 0xc0) === 0xc0) return pos + 2;
    if (n === 0) return pos + 1;
    pos += n + 1;
  }
  throw new Error("invalid DNS response name");
}

function aRecords(bytes) {
  if (bytes.length < 12) return [];
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const questions = view.getUint16(4);
  const answers = view.getUint16(6);
  let pos = 12;
  for (let i = 0; i < questions; i++) {
    pos = skipDnsName(bytes, pos);
    if (pos + 4 > bytes.length) return [];
    pos += 4;
  }
  const addresses = new Set();
  for (let i = 0; i < answers; i++) {
    pos = skipDnsName(bytes, pos);
    if (pos + 10 > bytes.length) break;
    const type = view.getUint16(pos);
    const cls = view.getUint16(pos + 2);
    const len = view.getUint16(pos + 8);
    pos += 10;
    if (pos + len > bytes.length) break;
    if (type === 1 && cls === 1 && len === 4) {
      addresses.add(Array.from(bytes.subarray(pos, pos + 4)).join("."));
    }
    pos += len;
  }
  return Array.from(addresses).sort();
}

async function dohQuery(endpoint, domain) {
  const packet = dnsPacket(domain);
  let binary = "";
  for (const byte of packet) binary += String.fromCharCode(byte);
  const encoded = btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  const response = await fetch(endpoint + "?dns=" + encoded, {
    headers: { accept: "application/dns-message" },
    redirect: "error",
  });
  if (!response.ok) throw new Error("DoH HTTP " + response.status);
  return aRecords(new Uint8Array(await response.arrayBuffer()));
}

async function checkDns(host) {
  const started = Date.now();
  const details = {};
  let answered = 0;
  let changed = 0;
  for (const domain of CHECK_DOMAINS) {
    try {
      const candidate = await dohQuery("https://" + host + "/dns-query", domain);
      const baseline = await dohQuery(BASELINE_DOH, domain);
      if (candidate.length) answered++;
      if (candidate.length && baseline.length &&
          JSON.stringify(candidate) !== JSON.stringify(baseline)) changed++;
      details[domain] = { candidate, baseline };
    } catch (error) {
      details[domain] = { error: String(error) };
    }
  }
  return {
    host,
    doh_endpoint: "https://" + host + "/dns-query",
    doh_works: answered >= 2,
    smartdns_signal: changed >= 2,
    changed_domains: changed,
    answered_domains: answered,
    latency_ms: Date.now() - started,
    details,
    note: "Это быстрая DoH-проверка; полный рейтинг строится GitHub crawler через DoT.",
  };
}

async function handleCheck(request, env, url) {
  const host = (url.searchParams.get("dns") ?? "").trim().toLowerCase().replace(/\.$/, "");
  if (!validHostname(host)) return jsonResponse({ error: "Введите hostname вида dns.example.com" }, 400);

  // The public top10 is small. Never parse the full history for one host.
  const summary = await env.STATE.get(PUBLIC_KEY, "json");
  const known = (summary?.top10 ?? []).find((item) => item.host === host);
  if (known) {
    return jsonResponse({
      source: "hourly-dot-scan",
      result: {
        host,
        sources: known.sources,
        last: {
          checked_at: known.last_checked,
          dns_ok: known.dns_ok,
          gemini_likely: known.gemini_likely,
          latency_ms: known.latency_ms,
          proxy_domains: known.proxy_domains,
        },
      },
    });
  }

  const queueRaw = await env.STATE.get(QUEUE_KEY);
  let queue = [];
  try { queue = JSON.parse(queueRaw ?? "[]"); } catch (_) {}
  if (!Array.isArray(queue)) queue = [];
  if (!queue.includes(host)) {
    queue = [...queue, host].slice(-100);
    try { await env.STATE.put(QUEUE_KEY, JSON.stringify(queue)); }
    catch (error) { console.warn("Queue update skipped:", String(error)); }
  }

  try {
    const result = await checkDns(host);
    return jsonResponse({ source: "instant-doh-probe", queued_for_dot_scan: true, result });
  } catch (error) {
    return jsonResponse({
      source: "instant-doh-probe",
      queued_for_dot_scan: true,
      result: { host, doh_works: false, smartdns_signal: false, error: String(error) },
    });
  }
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const path = url.pathname;
    const method = request.method;

    if (path === "/api/top" && method === "GET") {
      // Only return the small public snapshot; this must remain cheap even when
      // the full state contains months of DNS history.
      const raw = await env.STATE.get(PUBLIC_KEY);
      return raw ? new Response(raw, { headers: JSON_HEADERS }) :
        jsonResponse({ updated_at: null, top10: [], status: "waiting_for_first_scan" });
    }

    if (path === "/api/scheduler" && method === "GET") {
      const raw = await env.STATE.get(SCHEDULER_KEY);
      return raw ? new Response(raw, { headers: JSON_HEADERS }) :
        jsonResponse({ status: "waiting_for_first_cron" });
    }

    if (path === "/api/health" && method === "GET") {
      const snapshot = await env.STATE.get(PUBLIC_KEY, "json");
      const age = stateAgeMinutes(snapshot);
      return jsonResponse({
        ok: age !== null && age < 90,
        updated_at: snapshot?.updated_at ?? null,
        state_age_minutes: age === null ? null : Math.round(age * 10) / 10,
      }, age !== null && age < 90 ? 200 : 503);
    }

    if (path === "/api/state" && method === "GET") {
      if (!authorized(request, env)) return jsonResponse({ error: "unauthorized" }, 401);
      // Streaming avoids allocating the complete JSON in the 128 MB isolate.
      const stream = await env.STATE.get(STATE_KEY, { type: "stream" });
      return new Response(stream ?? '{"version":1,"candidates":{},"top10":[]}', { headers: JSON_HEADERS });
    }

    if (path === "/api/queue" && method === "GET") {
      if (!authorized(request, env)) return jsonResponse({ error: "unauthorized" }, 401);
      const raw = await env.STATE.get(QUEUE_KEY);
      return raw ? new Response(JSON.stringify({ queue: JSON.parse(raw) }), { headers: JSON_HEADERS }) :
        jsonResponse({ queue: [] });
    }

    if (path === "/api/publish" && method === "POST") {
      if (!authorized(request, env)) return jsonResponse({ error: "unauthorized" }, 401);
      if (!request.body) return jsonResponse({ error: "empty payload" }, 400);
      // KV accepts a ReadableStream: no whole-body JSON parse or serialize.
      await env.STATE.put(STATE_KEY, request.body);
      return jsonResponse({ ok: true, stored: "full_state" });
    }

    if (path === "/api/summary" && method === "POST") {
      if (!authorized(request, env)) return jsonResponse({ error: "unauthorized" }, 401);
      const summary = await request.json();
      if (!summary || typeof summary.updated_at !== "string" ||
          !Array.isArray(summary.top10) || !summary.scan) {
        return jsonResponse({ error: "invalid summary" }, 400);
      }
      await env.STATE.put(PUBLIC_KEY, JSON.stringify({
        updated_at: summary.updated_at,
        next_expected_update: summary.next_expected_update ?? null,
        scan: summary.scan,
        top10: summary.top10,
      }));
      return jsonResponse({ ok: true, top_count: summary.top10.length });
    }

    if (path === "/api/scheduler/test" && method === "POST") {
      if (!authorized(request, env)) return jsonResponse({ error: "unauthorized" }, 401);
      const status = await dispatchGitHub(env, "manual scheduler diagnostic");
      await env.STATE.put(SCHEDULER_KEY, JSON.stringify(status));
      return jsonResponse(status, status.dispatched ? 200 : 502);
    }

    if (path === "/api/check" && method === "GET") return handleCheck(request, env, url);

    return jsonResponse({ error: "not found" }, 404);
  },

  async scheduled(controller, env, ctx) {
    const cron = String(controller.cron);
    const now = new Date().toISOString();
    const snapshot = await env.STATE.get(PUBLIC_KEY, "json");
    const age = stateAgeMinutes(snapshot);
    const primary = cron === PRIMARY_CRON;
    const watchdog = cron === WATCHDOG_CRON;
    const shouldDispatch = primary || (watchdog && (age === null || age >= WATCHDOG_STALE_MINUTES));
    let reason = "unrecognized cron";
    if (primary) reason = "primary hourly trigger";
    else if (watchdog) reason = age === null ? "watchdog: no successful state yet" :
      (shouldDispatch ? "watchdog: state is stale (" + age.toFixed(1) + " min)" :
        "watchdog: state still fresh (" + age.toFixed(1) + " min)");
    const status = shouldDispatch ?
      await dispatchGitHub(env, reason, cron) :
      { checked_at: now, cron, dispatched: false, reason };
    status.state_age_minutes = age === null ? null : Math.round(age * 10) / 10;
    // Scheduler status is a separate, small KV key.
    await env.STATE.put(SCHEDULER_KEY, JSON.stringify(status));
    if (status.error) console.error(status.error);
  },
};
