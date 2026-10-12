const grid = document.getElementById('dnsGrid');
const notice = document.getElementById('notice');
const updatedAt = document.getElementById('updatedAt');
const candidateCount = document.getElementById('candidateCount');
const workingCount = document.getElementById('workingCount');
const refreshBtn = document.getElementById('refreshBtn');
const dnsInput = document.getElementById('dnsInput');
const checkBtn = document.getElementById('checkBtn');
const checkResult = document.getElementById('checkResult');
const radarPanel = document.getElementById('radarPanel');
const radarLabel = document.getElementById('radarLabel');
const radarTitle = document.getElementById('radarTitle');
const radarMessage = document.getElementById('radarMessage');
const scanMode = document.getElementById('scanMode');
const scanLimit = document.getElementById('scanLimit');
const previousWorking = document.getElementById('previousWorking');
const workingNow = document.getElementById('workingNow');
const failedCount = document.getElementById('failedCount');
const newCount = document.getElementById('newCount');
const changesBox = document.getElementById('changesBox');

const esc = value => String(value ?? '').replace(/[&<>'"]/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[ch]));

function when(value) {
  if (!value) return 'ещё нет данных';
  return new Date(value).toLocaleString('ru-RU', {dateStyle:'medium', timeStyle:'short'});
}

function riskClass(level) {
  return ({low:'ok', medium:'warn', high:'bad', unknown:'muted'})[level] || 'muted';
}

function radarClass(level) {
  return ({normal:'radar-normal', watch:'radar-watch', degraded:'radar-degraded', critical:'radar-critical', learning:'radar-learning'})[level] || 'radar-learning';
}

function renderRadar(scan = {}) {
  const outage = scan.outage || {
    level:'learning', label:'LEARNING', title:'Набираем базовую линию',
    message:'После нескольких запусков Radar сможет замечать массовые отвалы.'
  };

  radarPanel.className = `radar-panel ${radarClass(outage.level)}`;
  radarLabel.textContent = outage.label || 'LEARNING';
  radarTitle.textContent = outage.title || 'Набираем базовую линию';
  radarMessage.textContent = outage.message || '—';
  scanMode.textContent = (scan.mode || 'normal').toUpperCase();
  scanLimit.textContent = `${scan.scan_limit ?? 90} кандидатов`;
  previousWorking.textContent = outage.previous_working ?? '—';
  workingNow.textContent = outage.working_now ?? scan.working_count ?? '—';
  failedCount.textContent = outage.failed_count ?? '—';
  newCount.textContent = outage.new_count ?? '—';

  const failed = outage.failed_hosts || [];
  const added = outage.new_hosts || [];
  if (!failed.length && !added.length) {
    changesBox.classList.add('hidden');
    changesBox.innerHTML = '';
    return;
  }

  const parts = [];
  if (failed.length) parts.push(`<div><span>Отвалились:</span><b>${failed.map(esc).join(', ')}</b></div>`);
  if (added.length) parts.push(`<div><span>Новые / восстановились:</span><b>${added.map(esc).join(', ')}</b></div>`);
  changesBox.innerHTML = parts.join('');
  changesBox.classList.remove('hidden');
}

function card(item, index) {
  const risk = item.risk || {level:'unknown', label:'Мало данных', forecast:'—'};
  return `
    <article class="dns-card">
      <div class="rank">#${index + 1}</div>
      <div class="card-head">
        <div>
          <h3>${esc(item.host)}</h3>
          <span class="score">score ${esc(item.score)}</span>
        </div>
        <span class="pill ok"><i></i>Gemini signal</span>
      </div>
      <div class="copy-box">
        <code>${esc(item.host)}</code>
        <button class="copy" data-host="${esc(item.host)}">Копировать</button>
      </div>
      <div class="metrics">
        <div><span>24 часа</span><strong>${esc(item.uptime_24h)}%</strong><small>${esc(item.checks_24h)} проверок</small></div>
        <div><span>7 дней</span><strong>${esc(item.uptime_7d)}%</strong><small>${esc(item.checks_7d)} проверок</small></div>
        <div><span>Серия</span><strong>${esc(item.streak_hours)} ч</strong><small>успешных подряд</small></div>
        <div><span>DoT latency</span><strong>${item.latency_ms ? esc(item.latency_ms) + ' ms' : '—'}</strong><small>порт 853</small></div>
      </div>
      <div class="risk ${riskClass(risk.level)}">
        <div><span>Риск отвала</span><strong>${esc(risk.label)}</strong></div>
        <p>${esc(risk.forecast)}</p>
      </div>
      <div class="proxy-row"><span>Проксируются цели:</span><b>${esc((item.proxy_domains || []).length)} / 4</b></div>
      <div class="foot">Проверено: ${when(item.last_checked)}</div>
    </article>`;
}

async function loadTop() {
  refreshBtn.disabled = true;
  try {
    const response = await fetch('/api/top', {cache:'no-store'});
    const data = await response.json();
    updatedAt.textContent = when(data.updated_at);
    candidateCount.textContent = data.scan?.candidate_count ?? '—';
    workingCount.textContent = data.scan?.working_count ?? '—';
    renderRadar(data.scan || {});

    const rows = data.top10 || [];
    if (!rows.length) {
      grid.innerHTML = '';
      notice.classList.remove('hidden');
      notice.textContent = data.status === 'waiting_for_first_scan'
        ? 'Сайт уже готов. TOP‑10 появится после первого запуска hourly crawler.'
        : 'Сейчас crawler не подтвердил ни одного DNS достаточно уверенно. Мы не подставляем нерабочие адреса ради цифры 10.';
      return;
    }
    notice.classList.toggle('hidden', rows.length === 10);
    if (rows.length < 10) notice.textContent = `Подтверждено только ${rows.length} DNS. Остальные позиции намеренно не заполняются сомнительными серверами.`;
    grid.innerHTML = rows.map(card).join('');
    document.querySelectorAll('.copy').forEach(btn => btn.addEventListener('click', async () => {
      await navigator.clipboard.writeText(btn.dataset.host);
      const old = btn.textContent;
      btn.textContent = 'Готово';
      setTimeout(() => btn.textContent = old, 1200);
    }));
  } catch (error) {
    notice.classList.remove('hidden');
    notice.textContent = 'Не удалось загрузить рейтинг: ' + error.message;
  } finally {
    refreshBtn.disabled = false;
  }
}

async function checkDns() {
  const host = dnsInput.value.trim();
  if (!host) return dnsInput.focus();
  checkBtn.disabled = true;
  checkBtn.textContent = 'Проверяем…';
  checkResult.classList.remove('hidden');
  checkResult.innerHTML = '<span class="spinner"></span> Выполняем проверку…';
  try {
    const response = await fetch('/api/check?dns=' + encodeURIComponent(host), {cache:'no-store'});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Ошибка проверки');
    const r = data.result || {};
    const known = data.source === 'hourly-dot-scan';
    const likely = known ? r.last?.gemini_likely : r.smartdns_signal;
    const ok = known ? r.last?.dns_ok : r.doh_works;
    checkResult.innerHTML = `
      <strong class="${likely ? 'text-ok' : 'text-warn'}">${likely ? 'Есть SmartDNS-сигнал для Gemini' : 'SmartDNS-сигнал не подтверждён'}</strong>
      <span>${known ? 'Полная DoT-проверка из hourly crawler' : 'Быстрая DoH-проверка; DNS добавлен в очередь полного DoT-скана'}</span>
      <span>DNS: ${ok ? 'отвечает' : 'не подтверждён'}</span>`;
  } catch (error) {
    checkResult.innerHTML = `<strong class="text-bad">Ошибка</strong><span>${esc(error.message)}</span>`;
  } finally {
    checkBtn.disabled = false;
    checkBtn.textContent = 'Проверить';
  }
}

refreshBtn.addEventListener('click', loadTop);
checkBtn.addEventListener('click', checkDns);
dnsInput.addEventListener('keydown', e => { if (e.key === 'Enter') checkDns(); });
loadTop();
setInterval(loadTop, 5 * 60 * 1000);
