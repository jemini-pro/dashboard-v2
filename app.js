const WORK_START_HOUR = 9;
const WORK_END_HOUR = 17;
// One fresh click scores 1.0 (a single exp(0) term), so any clicked link already
// clears this 0.5 bar. The spec's "~3 clicks" wording describes typical use, not
// a gate: the threshold and 14-day half-life are the actual rule.
const MOST_USED_MIN_SCORE = 0.5;
const MOST_USED_LIMIT = 6;

// Launchpad: items opt in with `pinned: true`; with none, these titles stand in.
// Both paths stay in store order and cap at eight, so the tile count follows data.
const PINNED_LIMIT = 8;
const PINNED_FALLBACK_TITLES = [
    'Gmail', 'Google Calendar', 'Google Sheets', 'GitHub',
    'Slack', 'T3 Code', 'Proxmox', 'AWS'
];

// Network metrics turn amber at this percentage. Status is carried by the
// number as well as the colour, so the value is never colour-only.
const METRIC_WARN_PCT = 80;
// One definition drives both the labels and the ordering, so adding a kind
// means editing one list rather than two constants that can drift apart.
const KIND_DEFS = [
    { key: 'host', label: 'Hosts' },
    { key: 'lxc', label: 'LXC' },
    { key: 'lxc-guest', label: 'LXC guests' },
    { key: 'pc', label: 'PC' },
    { key: 'mac', label: 'Mac' }
];
const KIND_LABELS = Object.fromEntries(KIND_DEFS.map((kind) => [kind.key, kind.label]));
const KIND_ORDER = KIND_DEFS.map((kind) => kind.key);
// One metric definition drives both the row markup and the in-place poll patch.
const DEVICE_METRICS = [
    { label: 'CPU', key: 'cpu', field: 'cpu_pct' },
    { label: 'RAM', key: 'ram', field: 'mem_pct' },
    { label: 'DSK', key: 'disk', field: 'disk_pct' }
];

const clockEl = document.getElementById('clock');
const dateEl = document.getElementById('date');
const searchInput = document.getElementById('search');
const healthDotEl = document.getElementById('health-dot');
const healthTextEl = document.getElementById('health-text');
const bookmarksGridEl = document.getElementById('bookmarks-grid');
const resultCountEl = document.getElementById('result-count');
const mostUsedEl = document.getElementById('most-used');
const launchpadGridEl = document.getElementById('launchpad-grid');
const deviceGridEl = document.getElementById('device-grid');
const kindChipsEl = document.getElementById('kind-chips');
const networkSummaryEl = document.getElementById('network-summary');
const networkUpdatedEl = document.getElementById('network-updated');
const networkStaleEl = document.getElementById('network-stale');
const debugTimeInput = document.getElementById('debug-time');
const resetTimeBtn = document.getElementById('reset-time');
const debugMenuBtn = document.getElementById('debug-menu-btn');
const debugControls = document.getElementById('debug-controls');

let debugTimeOffset = null;
let currentTheme = null;
let bookmarksRendered = false;
let bookmarkStore = [];
let networkDevices = [];
// Keyed by device identity so a poll can patch existing rows, and null until
// the first successful payload so the initial "Loading devices…" is replaced.
let networkDeviceKey = null;
let networkUpdatedAt = null;
let searchQuery = '';
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

    // Update the toolbar clock and date.
    const timeString = `${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}`;
    if (clockEl.textContent !== timeString) clockEl.textContent = timeString;
    if (clockEl.dateTime !== timeString) clockEl.dateTime = timeString;

    const dateString = now.toLocaleDateString('en-GB', {
        weekday: 'short', day: 'numeric', month: 'short'
    });
    if (dateEl.textContent !== dateString) dateEl.textContent = dateString;

    // Determine theme: work 09:00-17:00, personal otherwise.
    const isWorkHours = hours >= WORK_START_HOUR && hours < WORK_END_HOUR;
    const newTheme = isWorkHours ? 'work' : 'personal';

    if (newTheme !== currentTheme) {
        currentTheme = newTheme;
        if (isWorkHours) {
            document.documentElement.removeAttribute('data-theme');
        } else {
            document.documentElement.setAttribute('data-theme', 'personal');
        }
    }

    if (!bookmarksRendered) {
        bookmarksRendered = true;
        const inline = injectedBookmarks();
        if (inline) {
            bookmarkStore = inline.work;
            renderAllTools(bookmarkStore);
            renderLaunchpad(bookmarkStore);
            renderMostUsed(bookmarkStore);
        } else {
            // Static-only hosting: no injected payload, so fetch the store.
            loadBookmarks();
        }
        startDevicePolling();
    }
}

function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>"']/g, (c) => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[c]));
}

// Icons are monograms only: no emoji and no third-party icon requests.
function monogram(item) {
    const title = String((item && item.title) || '').trim();
    const initial = title ? title.charAt(0).toUpperCase() : '?';
    return '<span class="monogram" aria-hidden="true">' + escapeHtml(initial) + '</span>';
}

// ---------- All tools ----------
// Every category and item from the store, in store order. The toolbar search
// filters title, description and category name; categories only ever disappear
// when they have no matches, so the surviving order is the store order.

function filterToolCategories(categories, query) {
    const needle = String(query || '').trim().toLowerCase();
    if (!needle) return categories || [];
    return (categories || []).map(cat => ({
        category: cat.category,
        items: (cat.items || []).filter(item => {
            const haystack = item.title + ' ' + (item.desc || '') + ' ' + cat.category;
            return haystack.toLowerCase().includes(needle);
        })
    })).filter(cat => cat.items.length);
}

function countToolItems(categories) {
    return (categories || []).reduce((n, cat) => n + (cat.items || []).length, 0);
}

// Numbers are scoped to each category and to the rows shown, so a filtered
// category reads 01, 02… without inheriting gaps from hidden items.
function toolRow(item, index) {
    return `
        <li>
            <a class="tool-link" href="${escapeHtml(item.url)}" target="_blank" rel="noreferrer">
                <span class="tool-num">${String(index + 1).padStart(2, '0')}</span>
                <span class="tool-link-main">
                    <span class="tool-link-title">${escapeHtml(item.title)}</span>
                    <span class="tool-link-desc">${escapeHtml(item.desc || '')}</span>
                </span>
                <span class="tool-link-arrow" aria-hidden="true">↗</span>
            </a>
        </li>
    `;
}

function toolCategorySection(cat) {
    return `
        <section class="tool-category">
            <h3 class="tool-category-title">${escapeHtml(cat.category)}</h3>
            <ul class="tool-list">
                ${cat.items.map((item, i) => toolRow(item, i)).join('')}
            </ul>
        </section>
    `;
}

function allToolsEmptyText(query) {
    const needle = String(query || '').trim();
    return needle ? 'No tools match “' + needle + '”.' : 'No bookmarks loaded.';
}

// While filtering, report how many of the store's tools survived. With no
// query the slot stays empty; the spec only asks for the count while filtering.
function resultCountText(shown, total, query) {
    return String(query || '').trim()
        ? shown + ' of ' + total + ' tools'
        : '';
}

function renderAllTools(categories) {
    const all = categories || [];
    const filtered = filterToolCategories(all, searchQuery);
    const shown = countToolItems(filtered);
    const total = countToolItems(all);

    if (resultCountEl) {
        resultCountEl.textContent = resultCountText(shown, total, searchQuery);
    }

    bookmarksGridEl.innerHTML = filtered.length
        ? filtered.map(cat => toolCategorySection(cat)).join('')
        : '<div class="tool-empty">' + escapeHtml(allToolsEmptyText(searchQuery)) + '</div>';
}

// Launchpad selection. A store item opts in with `pinned: true`; only when
// nothing opts in do the fallback titles stand in, walked in the listed order
// and skipping titles the store does not carry. The pinned path keeps store
// order. Either way the result is capped at eight.
function selectLaunchpad(categories) {
    const items = [];
    (categories || []).forEach(cat => {
        (cat.items || []).forEach(item => items.push(item));
    });
    const pinned = items.filter(item => item && item.pinned === true);
    if (!pinned.length) {
        return PINNED_FALLBACK_TITLES
            .map((title) => items.find((item) => item && item.title === title))
            .filter(Boolean)
            .slice(0, PINNED_LIMIT);
    }
    return pinned.slice(0, PINNED_LIMIT);
}

// Launchpad render hook. Monogram icon plus a two-line label; the CSS clamps
// the label. The title attribute keeps the description discoverable.
function renderLaunchpad(categories) {
    const items = selectLaunchpad(categories);
    launchpadGridEl.innerHTML = items.length
        ? items.map(launchpadTile).join('')
        : '<div class="card-empty">No pinned tools</div>';
}

function launchpadTile(item) {
    const title = escapeHtml(item.title);
    const desc = escapeHtml(item.desc || '');
    return '<a class="launchpad-tile" href="' + escapeHtml(item.url) + '" ' +
        'target="_blank" rel="noreferrer" title="' + title +
        (desc ? ' — ' + desc : '') + '">' +
        '<span class="launchpad-icon">' + monogram(item) + '</span>' +
        '<span class="launchpad-tile-label">' + title + '</span>' +
        '</a>';
}

// Jump back in draws from the decayed `score` the server adds to each item;
// the scoreless embedded copy used for the first paint never qualifies. This
// picks copies for the card and leaves the category order untouched.
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
        .slice(0, MOST_USED_LIMIT);
}

// The bar compares each row with the highest score currently on show, so the
// top row always fills it. Callers never render the raw score as a number.
function usageBarPercent(score, maxScore) {
    if (!maxScore || maxScore <= 0) return 0;
    return Math.max(0, Math.min(100, Math.round((score / maxScore) * 100)));
}

function usageRow(entry, maxScore) {
    const item = entry.item;
    const pct = usageBarPercent(entry.score, maxScore);
    return `
        <li>
            <a href="${escapeHtml(item.url)}" class="usage-row" target="_blank" rel="noreferrer">
                <span class="usage-icon">${monogram(item)}</span>
                <span class="usage-main">
                    <span class="usage-title">${escapeHtml(item.title)}</span>
                    <span class="usage-desc">${escapeHtml(item.desc || '')}</span>
                </span>
                <span class="usage-bar" aria-hidden="true"><span style="width:${pct}%"></span></span>
            </a>
        </li>
    `;
}

// The card stays visible when nothing qualifies, with a quiet empty row.
function renderMostUsed(categories) {
    const entries = selectMostUsed(categories);
    mostUsedEl.hidden = false;
    if (!entries.length) {
        mostUsedEl.innerHTML = '<li class="usage-empty">No usage yet</li>';
        return;
    }
    const maxScore = entries[0].score;
    mostUsedEl.innerHTML = entries.map(entry => usageRow(entry, maxScore)).join('');
}

// Record link activation server-side for every card: All tools rows, Jump back
// in rows and Launchpad tiles. One document-level pair of listeners means a
// click is recorded exactly once whichever container rendered the link, and
// re-rendering never needs per-row listeners. sendBeacon survives the
// navigation that a plain fetch would cancel; auxclick covers middle-click,
// while plain and ctrl/cmd clicks arrive as click.
function recordHit(event) {
    if (event.type === 'auxclick' && event.button !== 1) return;
    const link = event.target.closest('.usage-row, .tool-link, .launchpad-tile');
    if (!link) return;
    const url = link.getAttribute('href');
    if (!url) return;
    navigator.sendBeacon('/api/hit', JSON.stringify({ url }));
}

document.addEventListener('click', recordHit);
document.addEventListener('auxclick', recordHit);

// ---------- search ----------

function isTypingTarget(target) {
    if (!target) return false;
    const tag = target.tagName;
    return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || !!target.isContentEditable;
}

// The search field filters All tools in place; every other card reflects the
// store as loaded.
function applySearchFilter(query) {
    searchQuery = query;
    renderAllTools(bookmarkStore);
}

// "/" focuses search from anywhere outside a field. The caret keys are
// deliberately left alone so they keep moving the caret while the user types.
document.addEventListener('keydown', (e) => {
    if (e.key !== '/') return;
    if (isTypingTarget(e.target) || e.metaKey || e.ctrlKey || e.altKey) return;
    e.preventDefault();
    searchInput.focus();
});

searchInput.addEventListener('input', (e) => {
    applySearchFilter(e.target.value);
});

// Esc clears and leaves the field.
searchInput.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    e.preventDefault();
    searchInput.value = '';
    applySearchFilter('');
    searchInput.blur();
});

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

async function loadBookmarks() {
    try {
        const res = await fetch('/api/bookmarks');
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const data = await res.json();
        if (data && Array.isArray(data.work) && data.work.length) {
            bookmarkStore = data.work;
            renderAllTools(bookmarkStore);
            renderLaunchpad(bookmarkStore);
            renderMostUsed(data.work);
            return;
        }
        throw new Error('empty bookmark store');
    } catch (e) {
        console.warn('bookmark store unreachable', e);
        // No scored store: paint the Workbench empty states rather than
        // leaving the cards blank.
        renderAllTools(bookmarkStore);
        renderLaunchpad(bookmarkStore);
        renderMostUsed(bookmarkStore);
    }
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

// ---------- Network card ----------

function isNumericSample(value) {
    return typeof value === 'number' && Number.isFinite(value);
}

// Metric values are clamped to 0-100 for the bar; a missing sample stays null
// so it can render as an em dash instead of a false zero.
function clampPct(value) {
    if (!isNumericSample(value)) return null;
    return Math.max(0, Math.min(100, value));
}

// Known kinds get their proper plural label; anything else is made readable
// ("nas-box" -> "Nas box") instead of leaking a raw slug into the UI.
function kindLabel(kind) {
    if (KIND_LABELS[kind]) return KIND_LABELS[kind];
    const text = String(kind || '').replace(/[-_]+/g, ' ').trim();
    if (!text) return 'Other';
    return text.charAt(0).toUpperCase() + text.slice(1);
}

function kindRank(kind) {
    const index = KIND_ORDER.indexOf(kind);
    return index === -1 ? KIND_ORDER.length : index;
}

function groupDevicesByKind(devices) {
    const groups = {};
    devices.forEach((d) => {
        const key = d.kind || 'other';
        if (!groups[key]) groups[key] = [];
        groups[key].push(d);
    });
    return groups;
}

// Kind and summary averages include online devices with a real numeric sample
// only; offline readings and nulls never drag the average toward zero.
function averageSample(devices, field) {
    const samples = devices
        .filter((d) => d.online && isNumericSample(d[field]))
        .map((d) => d[field]);
    if (!samples.length) return null;
    return samples.reduce((sum, value) => sum + value, 0) / samples.length;
}

function avgText(value) {
    return value === null ? '—' : Math.round(value) + '%';
}

// One count feeds both the Network summary and the toolbar health text.
function deviceCounts(devices) {
    const total = devices.length;
    const online = devices.filter((d) => d.online).length;
    return { online, total };
}

function networkSummary(devices) {
    const { online, total } = deviceCounts(devices);
    if (!total) return 'no device data';
    // The avg CPU part is always present; a missing sample reads as an em dash.
    return online + '/' + total + ' online · ' +
        avgText(averageSample(devices, 'cpu_pct')) + ' avg CPU';
}

// One helper owns the "cpu · ram" chip meta for both the full render and the
// in-place poll patch.
function chipMeta(group) {
    return avgText(averageSample(group, 'cpu_pct')) + ' cpu · ' +
        avgText(averageSample(group, 'mem_pct')) + ' ram';
}

function kindChips(devices) {
    const groups = groupDevicesByKind(devices);
    return Object.keys(groups)
        .sort((a, b) => kindRank(a) - kindRank(b))
        .map((key) => {
            const group = groups[key];
            return '<div class="kind-chip" data-kind="' + escapeHtml(key) + '">' +
                '<span class="kind-name">' + escapeHtml(kindLabel(key)) + '</span>' +
                '<span class="kind-count">' + group.length + '</span>' +
                '<span class="kind-meta">' + chipMeta(group) + '</span>' +
                '</div>';
        })
        .join('');
}

// 24-hour HH:MM, independent of the browser locale (a 12-hour locale must not
// turn 14:05 into 2:05 PM).
function updatedLabel(timestamp) {
    if (!timestamp) return '';
    const date = new Date(timestamp * 1000);
    return 'updated ' + String(date.getHours()).padStart(2, '0') + ':' +
        String(date.getMinutes()).padStart(2, '0');
}

function metricCell(metric, raw) {
    const value = clampPct(raw);
    const warn = value !== null && value >= METRIC_WARN_PCT ? ' is-warn' : '';
    return '<span class="metric' + warn + '" data-metric="' + metric.key + '">' +
        '<span class="metric-label">' + metric.label + '</span>' +
        '<span class="metric-value">' + (value === null ? '—' : Math.round(value) + '%') + '</span>' +
        '<span class="metric-bar"><i class="metric-fill" style="width:' + (value === null ? 0 : value) + '%"></i></span>' +
        '</span>';
}

// Online rows read "kind · up 3d 4h · online"; offline rows keep their
// last-seen time and say "offline". The status is text as well as the dot, so
// assistive tech does not depend on colour.
function deviceSub(d) {
    if (!d.online) {
        return (d.last_seen ? fmtAgo(d.last_seen) : kindLabel(d.kind)) + ' · offline';
    }
    const kind = kindLabel(d.kind);
    const detail = d.uptime_s === null || d.uptime_s === undefined
        ? kind
        : kind + ' · up ' + fmtUptime(d.uptime_s);
    return detail + ' · online';
}

function deviceKey(d) {
    return d.id || d.name;
}

function deviceRow(d) {
    const online = !!d.online;
    const state = online ? 'online' : 'offline';
    return '<div class="device-row ' + state + '" data-device-id="' + escapeHtml(deviceKey(d)) + '">' +
        '<div class="device-id">' +
        '<span class="device-dot ' + state + '" aria-hidden="true"></span>' +
        '<span class="device-text">' +
        '<span class="device-name">' + escapeHtml(d.name) + '</span>' +
        '<span class="device-sub">' + escapeHtml(deviceSub(d)) + '</span>' +
        '<span class="device-error"' + (d.error ? '' : ' hidden') + '>' + escapeHtml(d.error || '') + '</span>' +
        '</span>' +
        '</div>' +
        DEVICE_METRICS.map((metric) => metricCell(metric, d[metric.field])).join('') +
        '</div>';
}

// Order only by kind: devices inside a kind keep the order the server sent.
// Array.prototype.sort is stable, so equal ranks never move.
function sortNetworkDevices(devices) {
    return devices.slice().sort((a, b) => kindRank(a.kind) - kindRank(b.kind));
}

// Full render happens once and whenever the device set itself changes.
function renderNetwork(devices) {
    networkDeviceKey = networkKey(devices);
    kindChipsEl.innerHTML = kindChips(devices);
    deviceGridEl.innerHTML = devices.length
        ? devices.map(deviceRow).join('')
        : '<div class="device-empty">No devices reporting</div>';
    networkSummaryEl.textContent = networkSummary(devices);
}

function networkKey(devices) {
    return devices.map((d) => deviceKey(d) + ':' + (d.kind || '')).join('|');
}

function patchMetric(cell, raw) {
    if (!cell) return;
    const value = clampPct(raw);
    cell.classList.toggle('is-warn', value !== null && value >= METRIC_WARN_PCT);
    cell.querySelector('.metric-value').textContent = value === null ? '—' : Math.round(value) + '%';
    cell.querySelector('.metric-fill').style.width = (value === null ? 0 : value) + '%';
}

// The 10s poll changes values, not structure: existing device rows and kind
// chips keep their elements so bars animate and nothing flickers.
function patchNetwork(devices) {
    networkSummaryEl.textContent = networkSummary(devices);

    const rows = deviceGridEl.querySelectorAll('.device-row');
    rows.forEach((row, index) => {
        const d = devices[index];
        if (!d) return;
        const online = !!d.online;
        row.classList.toggle('online', online);
        row.classList.toggle('offline', !online);
        row.querySelector('.device-dot').className = 'device-dot ' + (online ? 'online' : 'offline');
        row.querySelector('.device-name').textContent = d.name;
        row.querySelector('.device-sub').textContent = deviceSub(d);
        const errorEl = row.querySelector('.device-error');
        errorEl.textContent = d.error || '';
        errorEl.hidden = !d.error;
        DEVICE_METRICS.forEach((metric) => {
            patchMetric(row.querySelector('[data-metric="' + metric.key + '"]'), d[metric.field]);
        });
    });

    const groups = groupDevicesByKind(devices);
    kindChipsEl.querySelectorAll('.kind-chip').forEach((chip) => {
        const group = groups[chip.getAttribute('data-kind')] || [];
        chip.querySelector('.kind-count').textContent = String(group.length);
        chip.querySelector('.kind-meta').textContent = chipMeta(group);
    });
}

function applyNetworkDevices(devices) {
    if (networkKey(devices) !== networkDeviceKey) {
        renderNetwork(devices);
        return;
    }
    patchNetwork(devices);
}

function setNetworkStale(stale) {
    networkStaleEl.hidden = !stale;
    deviceGridEl.classList.toggle('is-stale', stale);
}

// Health is the toolbar's dot plus "n/m online". Amber whenever anything is
// offline; green only when every device is online.
function renderHealth(devices) {
    const { online, total } = deviceCounts(devices);
    const status = total === 0 ? 'unknown' : (online === total ? 'online' : 'warn');
    healthDotEl.className = 'health-dot ' + status;
    healthTextEl.textContent = total ? online + '/' + total + ' online' : 'no devices';
}

async function refreshDevices() {
    try {
        const res = await fetch('/api/stats');
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const data = await res.json();
        networkDevices = sortNetworkDevices(data.devices || []);
        networkUpdatedAt = data.generated || Math.floor(Date.now() / 1000);
        networkUpdatedEl.textContent = updatedLabel(networkUpdatedAt);
        setNetworkStale(false);
        applyNetworkDevices(networkDevices);
        renderHealth(networkDevices);
        statsFailures = 0;
        return true;
    } catch (e) {
        // Keep the last good reading and label it stale: a failed poll must
        // never erase the panel or report a false all-clear.
        statsFailures += 1;
        console.warn('stats unreachable, keeping the last good reading', e);
        setNetworkStale(true);
        renderHealth(networkDevices);
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
