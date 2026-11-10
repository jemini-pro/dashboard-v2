const WORK_START_HOUR = 9;
const WORK_END_HOUR = 17;
// One fresh click scores 1.0 (a single exp(0) term), so any clicked link already
// clears this 0.5 bar. The spec's "~3 clicks" wording describes typical use, not
// a gate: the threshold and 14-day half-life are the actual rule.
const MOST_USED_MIN_SCORE = 0.5;
const MOST_USED_LIMIT = 6;

const clockEl = document.getElementById('clock');
const greetingEl = document.getElementById('greeting');
const bookmarksGridEl = document.getElementById('bookmarks-grid');
const mostUsedEl = document.getElementById('most-used');
const debugTimeInput = document.getElementById('debug-time');
const resetTimeBtn = document.getElementById('reset-time');
const debugMenuBtn = document.getElementById('debug-menu-btn');
const debugControls = document.getElementById('debug-controls');

let debugTimeOffset = null;
let currentTheme = null;
let bookmarksRendered = false;
let lastDevices = null;
let statsFailures = 0;
let refreshTimer = null;
let pollingStarted = false;

// Debug Menu Toggle
debugMenuBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    debugControls.classList.toggle('visible');
    debugMenuBtn.classList.toggle('active');
});

document.addEventListener('click', (e) => {
    if (!debugControls.contains(e.target) && !debugMenuBtn.contains(e.target)) {
        debugControls.classList.remove('visible');
        debugMenuBtn.classList.remove('active');
    }
});

function getCurrentTime() {
    if (debugTimeOffset !== null) {
        return new Date(Date.now() + debugTimeOffset);
    }
    return new Date();
}

// The server injects the scored bookmark store into index.html, so the first
// paint needs no second request. Static-only hosting leaves the placeholder
// behind; then this returns null and the fetch fallback takes over.
function injectedBookmarks() {
    const el = document.getElementById('bookmarks-data');
    if (!el) return null;
    try {
        const data = JSON.parse(el.textContent);
        return (data && Array.isArray(data.work) && data.work.length) ? data : null;
    } catch (e) {
        return null;
    }
}

function updateDashboard() {
    const now = getCurrentTime();
    const hours = now.getHours();
    const minutes = now.getMinutes();

    // Update Clock
    const timeString = `${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}`;
    if (clockEl.textContent !== timeString) {
        clockEl.textContent = timeString;
    }

    // Determine Theme based on specified hours
    const isWorkHours = hours >= WORK_START_HOUR && hours < WORK_END_HOUR;
    const newTheme = isWorkHours ? 'work' : 'personal';

    // Update Theme only when changed
    if (newTheme !== currentTheme) {
        currentTheme = newTheme;
        if (isWorkHours) {
            document.documentElement.removeAttribute('data-theme');
            greetingEl.textContent = 'Time to Focus';
        } else {
            document.documentElement.setAttribute('data-theme', 'personal');
            greetingEl.textContent = 'Time to Relax';
        }
    }

    // Work bookmarks are the main bookmarks all the time
    if (!bookmarksRendered) {
        bookmarksRendered = true;
        const inline = injectedBookmarks();
        if (inline) {
            renderBookmarks(inline.work);
            renderMostUsed(inline.work);
        } else {
            loadBookmarks();
        }
        startDevicePolling();
    }
}

function bookmarkCard(item) {
    return `
        <a href="${item.url}" class="bookmark-card" target="_blank">
            <div class="bookmark-icon-text">${item.icon}</div>
            <div class="bookmark-details">
                <div class="bookmark-title">${item.title}</div>
                <div class="bookmark-desc">${item.desc || ''}</div>
            </div>
        </a>
    `;
}

// One section skeleton for both the bookmark categories and the Most used strip,
// so the two grids can never drift apart in markup.
function categorySection(title, items) {
    return `
        <div class="category-section">
            <h2 class="category-title">${title}</h2>
            <div class="items-grid">
                ${items.map(item => bookmarkCard(item)).join('')}
            </div>
        </div>
    `;
}

function renderBookmarks(categories) {
    // If the data is flat (old personal array structure fallback), wrap it
    if (categories.length > 0 && !categories[0].category) {
        categories = [{ category: 'Bookmarks', items: categories }];
    }

    bookmarksGridEl.innerHTML = categories.map(
        cat => categorySection(cat.category, cat.items)
    ).join('');
}

// The Most used strip draws from the decayed `score` the server adds to each
// item. This picks copies for the strip and leaves the category order untouched.
function selectMostUsed(categories) {
    const scored = [];
    (categories || []).forEach(cat => {
        (cat.items || []).forEach(item => {
            const score = Number(item.score) || 0;
            if (score > MOST_USED_MIN_SCORE) scored.push({ item, score });
        });
    });
    return scored
        .sort((a, b) => b.score - a.score)
        .slice(0, MOST_USED_LIMIT)
        .map(entry => entry.item);
}

function renderMostUsed(categories) {
    const items = selectMostUsed(categories);
    if (!items.length) {
        mostUsedEl.hidden = true;
        mostUsedEl.innerHTML = '';
        return;
    }
    mostUsedEl.innerHTML = categorySection('Most used', items);
    mostUsedEl.hidden = false;
}

// Record bookmark activation server-side. sendBeacon survives the navigation
// that a plain fetch would cancel; auxclick covers middle-click, while plain
// and ctrl/cmd clicks arrive as click.
function recordBookmarkHit(event) {
    if (event.type === 'auxclick' && event.button !== 1) return;
    const card = event.target.closest('.bookmark-card');
    if (!card) return;
    const url = card.getAttribute('href');
    if (!url) return;
    navigator.sendBeacon('/api/hit', JSON.stringify({ url }));
}

bookmarksGridEl.addEventListener('click', recordBookmarkHit);
bookmarksGridEl.addEventListener('auxclick', recordBookmarkHit);
mostUsedEl.addEventListener('click', recordBookmarkHit);
mostUsedEl.addEventListener('auxclick', recordBookmarkHit);

// Debug Controls
debugTimeInput.addEventListener('change', (e) => {
    if (!e.target.value) return;
    const [h, m] = e.target.value.split(':').map(Number);
    const now = new Date();
    const debugDate = new Date();
    debugDate.setHours(h, m, 0, 0);

    debugTimeOffset = debugDate.getTime() - now.getTime();
    updateDashboard();
});

resetTimeBtn.addEventListener('click', () => {
    debugTimeOffset = null;
    debugTimeInput.value = '';
    updateDashboard();
});

// Initial Run & Interval
updateDashboard();
setInterval(updateDashboard, 1000);

// ---------- server data ----------
const deviceGridEl = document.getElementById('device-grid');
const networkUpdatedEl = document.getElementById('network-updated');
const KIND_ORDER = { host: 0, lxc: 1, 'lxc-guest': 2, pc: 3, mac: 4 };

async function loadBookmarks() {
    try {
        const res = await fetch('/api/bookmarks');
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const data = await res.json();
        if (data && Array.isArray(data.work) && data.work.length) {
            renderBookmarks(data.work);
            renderMostUsed(data.work);
            return;
        }
        throw new Error('empty bookmark store');
    } catch (e) {
        console.warn('bookmark store unreachable', e);
        bookmarksGridEl.innerHTML = '<div class="device-empty">Bookmarks unavailable — check the server store.</div>';
        mostUsedEl.hidden = true;
        mostUsedEl.innerHTML = '';
    }
}

function fmtBytes(n) {
    if (!n) return '';
    const gb = n / (1024 ** 3);
    return gb.toFixed(gb >= 10 ? 1 : 2) + ' GB';
}

function fmtUptime(s) {
    if (s === null || s === undefined) return '—';
    const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
    if (d) return d + 'd ' + h + 'h';
    if (h) return h + 'h ' + m + 'm';
    return m + 'm';
}

function fmtAgo(ts) {
    const s = Math.max(0, Math.floor(Date.now() / 1000) - ts);
    if (s < 60) return s + 's ago';
    if (s < 3600) return Math.floor(s / 60) + 'm ago';
    return Math.floor(s / 3600) + 'h ago';
}

function metricRowEl(label, metric) {
    const row = document.createElement('div');
    row.className = 'metric-row';
    row.dataset.metric = metric;
    row.innerHTML =
        '<span class="metric-label"></span>' +
        '<span class="metric-value">—</span>' +
        '<div class="metric-bar"><div class="metric-fill"></div></div>' +
        '<span class="metric-extra"></span>';
    row.querySelector('.metric-label').textContent = label;
    return row;
}

function setMetric(card, name, pct, extra) {
    const row = card.querySelector('[data-metric="' + name + '"]');
    if (!row) return;
    if (pct === null || pct === undefined) {
        if (name === 'disk') {
            row.hidden = true;
        } else {
            row.hidden = false;
            row.querySelector('.metric-value').textContent = '—';
            row.querySelector('.metric-fill').style.width = '0%';
            row.querySelector('.metric-extra').textContent = extra || '';
        }
        return;
    }
    const v = Math.max(0, Math.min(100, pct));
    row.hidden = false;
    row.querySelector('.metric-value').textContent = v.toFixed(1) + '%';
    row.querySelector('.metric-fill').style.width = v + '%';
    row.querySelector('.metric-extra').textContent = extra || '';
}

function createDeviceCard(d) {
    const card = document.createElement('div');
    card.className = 'device-card';
    card.dataset.deviceId = d.id;
    card.innerHTML =
        '<div class="device-head">' +
        '<span class="device-dot"></span>' +
        '<span class="device-name"></span>' +
        '<span class="device-kind"></span>' +
        '</div>' +
        '<div class="device-error" hidden></div>';
    card.appendChild(metricRowEl('CPU', 'cpu'));
    card.appendChild(metricRowEl('RAM', 'ram'));
    card.appendChild(metricRowEl('Disk', 'disk'));
    const foot = document.createElement('div');
    foot.className = 'device-foot';
    card.appendChild(foot);
    updateDeviceCard(card, d);
    return card;
}

function updateDeviceCard(card, d) {
    card.classList.toggle('online', !!d.online);
    card.classList.toggle('offline', !d.online);
    const dot = card.querySelector('.device-dot');
    dot.classList.toggle('online', !!d.online);
    dot.classList.toggle('offline', !d.online);
    card.querySelector('.device-name').textContent = d.name || '';
    card.querySelector('.device-kind').textContent =
        (d.source === 'agent' && d.last_seen) ? fmtAgo(d.last_seen) : (d.kind || '');
    const error = card.querySelector('.device-error');
    if (d.error) {
        error.hidden = false;
        error.textContent = d.error;
    } else {
        error.hidden = true;
        error.textContent = '';
    }
    setMetric(card, 'cpu', d.cpu_pct);
    setMetric(card, 'ram', d.mem_pct, d.mem_total ? fmtBytes(d.mem_used) + ' / ' + fmtBytes(d.mem_total) : '');
    setMetric(card, 'disk', d.disk_pct);
    card.querySelector('.device-foot').textContent = 'up ' + fmtUptime(d.uptime_s);
}

function renderDevices(devices) {
    const wanted = new Set(devices.map(d => d.id));
    for (const card of [...deviceGridEl.querySelectorAll('.device-card')]) {
        if (!wanted.has(card.dataset.deviceId)) card.remove();
    }
    const empty = deviceGridEl.querySelector('.device-empty');
    if (!devices.length) {
        if (empty) empty.textContent = 'No devices reporting';
        else deviceGridEl.innerHTML = '<div class="device-empty">No devices reporting</div>';
        return;
    }
    if (empty) empty.remove();
    for (const d of devices) {
        let card = deviceGridEl.querySelector(
            '.device-card[data-device-id="' + CSS.escape(d.id) + '"]'
        );
        if (card) updateDeviceCard(card, d);
        else card = createDeviceCard(d);
        deviceGridEl.appendChild(card); // preserve the server's order
    }
}

async function refreshDevices() {
    try {
        const res = await fetch('/api/stats');
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const data = await res.json();
        const devices = (data.devices || []).slice().sort(
            (a, b) => (KIND_ORDER[a.kind] === undefined ? 9 : KIND_ORDER[a.kind])
                - (KIND_ORDER[b.kind] === undefined ? 9 : KIND_ORDER[b.kind])
        );
        renderDevices(devices);
        lastDevices = devices;
        statsFailures = 0;
        const generated = data.generated || Math.floor(Date.now() / 1000);
        networkUpdatedEl.textContent = 'updated ' + new Date(generated * 1000).toLocaleTimeString();
        networkUpdatedEl.classList.remove('stale');
        return true;
    } catch (e) {
        statsFailures += 1;
        if (!lastDevices) {
            deviceGridEl.innerHTML = '<div class="device-empty">Telemetry unavailable — retrying…</div>';
        }
        networkUpdatedEl.textContent = 'stale — retrying';
        networkUpdatedEl.classList.add('stale');
        return false;
    }
}

function scheduleRefresh(delay) {
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(async () => {
        if (document.hidden) {
            refreshTimer = null;
            return;
        }
        const ok = await refreshDevices();
        const next = ok
            ? 10000
            : Math.min(60000, 10000 * 2 ** Math.min(statsFailures, 3));
        scheduleRefresh(next);
    }, delay);
}

function startDevicePolling() {
    if (pollingStarted) return;
    pollingStarted = true;
    scheduleRefresh(0);
}

document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
        clearTimeout(refreshTimer);
        refreshTimer = null;
    } else if (pollingStarted && !refreshTimer) {
        scheduleRefresh(0);
    }
});
