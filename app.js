// The shared runtime owns escaping, kind labels, formatting, the clock/theme
// controls and the polling loop. The dashboard uses the compact byte policy.
const {
    escapeHtml,
    isNumber,
    kindLabel,
    KIND_ORDER,
    fmtBytesCompact,
    fmtUptime,
} = window.dashboardUI;

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
const briefListEl = document.getElementById('brief-list');
const briefSubEl = document.getElementById('brief-sub');
const deviceGridEl = document.getElementById('device-grid');
const networkSummaryEl = document.getElementById('network-summary');
const networkUpdatedEl = document.getElementById('network-updated');
const networkStaleEl = document.getElementById('network-stale');
const servicesSummaryEl = document.getElementById('services-summary');
const servicesListEl = document.getElementById('services-list');
const debugTimeInput = document.getElementById('debug-time');
const resetTimeBtn = document.getElementById('reset-time');
const debugMenuBtn = document.getElementById('debug-menu-btn');
const debugControls = document.getElementById('debug-controls');

let bookmarksRendered = false;
let bookmarkStore = [];
let networkDevices = [];
// The server describes pve -> docker01 -> Docker in the same payload; null
// until the first successful poll, so the tree can fall back to device kinds.
let networkTopology = null;
// Keyed by device identity so a poll can patch existing rows, and null until
// the first successful payload so the initial "Loading devices…" is replaced.
let networkDeviceKey = null;
let networkUpdatedAt = null;
let searchQuery = '';
// Latest /api/checks payload keyed by check id. Null until the first response
// so the launcher can tell "not loaded yet" apart from "check id is missing".
let checkState = null;
// Verdicts only change the launcher badges; the Services card still refreshes
// latency and uptime every poll. Rebuilding All tools each time would reset
// its scroll position for no visible change.
let checkSignature = null;
// Created on the first start, after refreshDevices is defined.
let devicePoller = null;
// The brief changes slowly, so it polls once a minute rather than with the rail.
let briefPoller = null;

const clock = window.dashboardUI.createClock({
    clock: clockEl,
    date: dateEl,
    debugMenu: debugMenuBtn,
    debugControls: debugControls,
    debugTime: debugTimeInput,
    resetTime: resetTimeBtn,
});

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
    clock.update();

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
        startBriefPolling();
    }
}

// Icons are monograms only: no emoji and no third-party icon requests.
function monogram(item) {
    const title = String((item && item.title) || '').trim();
    const initial = title ? title.charAt(0).toUpperCase() : '?';
    return '<span class="monogram" aria-hidden="true">' + escapeHtml(initial) + '</span>';
}

// ---------- service checks ----------
// One vocabulary for the launcher badges and the Services card. The dot's
// glyph and shape carry the verdict without hue; the label and the
// visually-hidden text carry it without sight. `mark` is that glyph.
const VERDICT_DEFS = {
    ok: { label: 'ok', mark: '' },
    service_down: { label: 'service down', mark: '!' },
    host_down: { label: 'host offline', mark: '!' },
    path_down: { label: 'path down', mark: '~' },
    unknown: { label: 'no data', mark: '?' }
};

function checksOf(payload) {
    return payload && Array.isArray(payload.checks) ? payload.checks : [];
}

function verdictKey(check) {
    const verdict = check && check.verdict;
    return Object.prototype.hasOwnProperty.call(VERDICT_DEFS, verdict)
        ? verdict
        : 'unknown';
}

function checkMap(payload) {
    const map = new Map();
    checksOf(payload).forEach((check) => {
        if (check && check.id) map.set(check.id, check);
    });
    return map;
}

// A reference that the store does not answer is still worth surfacing: the
// badge reads "no data" instead of quietly hiding a broken config.
function checkFor(item) {
    if (!checkState || !item || !item.check) return null;
    return checkState.get(item.check) || { id: item.check, verdict: 'unknown' };
}

function verdictLabel(check) {
    return VERDICT_DEFS[verdictKey(check)].label;
}

function verdictClass(check) {
    return 'is-' + verdictKey(check).replace(/_/g, '-');
}

// A failing dot carries "!" (service) or "~" (path); an unknown one "?"; a
// healthy one is plain. CSS gives host_down a square and path_down a ring.
function statusDotHTML(check) {
    return '<span class="status-dot ' + verdictClass(check) + '" aria-hidden="true">' +
        VERDICT_DEFS[verdictKey(check)].mark + '</span>';
}

function checkBadge(item) {
    const check = checkFor(item);
    if (!check) return '';
    const label = verdictLabel(check);
    const text = check.verdict === 'ok'
        ? ''
        : '<span class="check-text">' + escapeHtml(label) + '</span>';
    return '<span class="check-status ' + verdictClass(check) +
        '" data-check="' + escapeHtml(item.check) +
        '" title="Service: ' + escapeHtml(label) + '">' +
        statusDotHTML(check) + text +
        '<span class="visually-hidden">Service ' + escapeHtml(label) + '</span>' +
        '</span>';
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
                ${checkBadge(item)}
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
        checkBadge(item) +
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

// Initial Run & Interval
updateDashboard();
clock.start();

// ---------- server data ----------

async function loadBookmarks() {
    const result = await window.dashData.read('/api/bookmarks', 'bookmark store');
    const data = result.ok ? result.data : null;
    if (data && Array.isArray(data.work) && data.work.length) {
        bookmarkStore = data.work;
        renderAllTools(bookmarkStore);
        renderLaunchpad(bookmarkStore);
        renderMostUsed(data.work);
        return;
    }
    // No scored store: paint the Workbench empty states rather than leaving
    // the cards blank.
    renderAllTools(bookmarkStore);
    renderLaunchpad(bookmarkStore);
    renderMostUsed(bookmarkStore);
}

// ---------- What changed ----------

// Only the metrics deep link is trusted, and only for a device id the server
// encoded. Anything else renders as plain text.
function briefSafeHref(href) {
    return typeof href === 'string' && /^metrics\.html\?device=[A-Za-z0-9%._~:-]+$/.test(href)
        ? href : null;
}

// The API text names the device so it reads on its own; the row already has a
// device column, so drop the leading name rather than printing it twice.
function briefText(item) {
    const name = String(item.device_name || '');
    const text = String(item.text || '');
    if (name && text.indexOf(name + ':') === 0) return text.slice(name.length + 1).trim();
    if (name && text.indexOf(name + ' ') === 0) return text.slice(name.length + 1);
    return text;
}

function briefRow(item) {
    const level = ['critical', 'warning', 'info'].indexOf(item.level) === -1
        ? 'info' : item.level;
    const href = briefSafeHref(item.href);
    const tag = href ? 'a' : 'div';
    const attrs = href ? ' href="' + escapeHtml(href) + '"' : '';
    return '<li class="brief-row brief-level-' + level + '" data-level="' + level + '">' +
        '<' + tag + ' class="brief-row-link"' + attrs + '>' +
        '<span class="brief-mark" role="img" aria-label="' + level + '"></span>' +
        '<time class="brief-time" datetime="' +
        new Date(item.ts * 1000).toISOString() + '">' +
        escapeHtml(clockLabel(item.ts)) + '</time>' +
        '<span class="brief-device">' + escapeHtml(item.device_name || 'fleet') + '</span>' +
        '<span class="brief-text" title="' + escapeHtml(item.detail || item.text || '') + '">' +
        escapeHtml(briefText(item)) + '</span>' +
        '</' + tag + '></li>';
}

function renderBrief(payload) {
    const items = Array.isArray(payload && payload.items) ? payload.items : [];
    const quiet = !!(payload && payload.quiet);
    const rows = items.map(briefRow).join('');
    if (quiet) {
        briefListEl.innerHTML =
            '<li class="brief-empty brief-quiet">' +
            '<span class="brief-mark" aria-hidden="true"></span>' +
            'All clear — nothing needs attention</li>' + rows;
    } else {
        briefListEl.innerHTML = rows ||
            '<li class="brief-empty">No changes recorded</li>';
    }
    briefSubEl.textContent = quiet
        ? 'Last 24 hours · all clear'
        : 'Last 24 hours · ' + items.length + (items.length === 1 ? ' item' : ' items');
}

// A failed poll keeps the last good list instead of flashing an all-clear.
async function refreshBrief() {
    const result = await window.dashData.read('/api/brief?window=24h', 'brief');
    if (result.ok) renderBrief(result.data);
    return result.ok;
}

function startBriefPolling() {
    if (!briefPoller) {
        briefPoller = window.dashboardUI.createPoller({
            interval: 60000,
            maxInterval: 300000,
            run: refreshBrief,
        });
    }
    briefPoller.start();
}

function fmtAgo(ts) {
    const s = Math.max(0, Math.floor(Date.now() / 1000) - ts);
    if (s < 60) return s + 's ago';
    if (s < 3600) return Math.floor(s / 60) + 'm ago';
    return Math.floor(s / 3600) + 'h ago';
}

// ---------- Network card ----------

// Metric values are clamped to 0-100 for the bar; a missing sample stays null
// so it can render as an em dash instead of a false zero.
function clampPct(value) {
    if (!isNumber(value)) return null;
    return Math.max(0, Math.min(100, value));
}

function kindRank(kind) {
    const index = KIND_ORDER.indexOf(kind);
    return index === -1 ? KIND_ORDER.length : index;
}

// Kind and summary averages include online devices with a real numeric sample
// only; offline readings and nulls never drag the average toward zero.
// Containers are a layer of the Docker host, not devices, so their CPU stays
// out of the fleet average.
function averageSample(devices, field) {
    const samples = window.dashboardStack.countedDevices(devices)
        .filter((d) => d.online && isNumber(d[field]))
        .map((d) => clampPct(d[field]));
    if (!samples.length) return null;
    return samples.reduce((sum, value) => sum + value, 0) / samples.length;
}

function avgText(value) {
    return value === null ? '—' : Math.round(value) + '%';
}

// One count feeds both the Network summary and the toolbar health text.
// The shared helper drops Docker containers, which belong to the docker01
// card rather than counting as separate machines.
function deviceCounts(devices) {
    const counted = window.dashboardStack.countedDevices(devices);
    const total = counted.length;
    const online = counted.filter((d) => d.online).length;
    return { online, total };
}

function networkSummary(devices) {
    const { online, total } = deviceCounts(devices);
    if (!total) return 'no device data';
    // The avg CPU part is always present; a missing sample reads as an em dash.
    return online + '/' + total + ' online · ' +
        avgText(averageSample(devices, 'cpu_pct')) + ' avg CPU';
}

// 24-hour HH:MM, independent of the browser locale (a 12-hour locale must not
// turn 14:05 into 2:05 PM). Brief rows use this; updatedLabel keeps its own
// formatting, which its source contract pins to these same getHours calls.
function clockLabel(timestamp) {
    const date = new Date(timestamp * 1000);
    return String(date.getHours()).padStart(2, '0') + ':' +
        String(date.getMinutes()).padStart(2, '0');
}

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

function diskLabel(disk) {
    return disk.label || disk.mount || 'disk';
}

function diskRow(disk) {
    const pct = clampPct(disk.pct);
    const warn = pct !== null && pct >= METRIC_WARN_PCT ? ' is-warn' : '';
    const mount = escapeHtml(disk.mount || '');
    return '<div class="disk-row' + warn + '" data-mount="' + mount + '" title="' + mount + '">' +
        '<span class="disk-name">' + escapeHtml(diskLabel(disk)) + '</span>' +
        '<span class="disk-size">' + escapeHtml(fmtBytesCompact(disk.total)) + '</span>' +
        '<span class="disk-bar"><i class="disk-fill" style="width:' + (pct === null ? 0 : pct) + '%"></i></span>' +
        '<span class="disk-value">' + (pct === null ? '—' : Math.round(pct) + '%') + '</span>' +
        '</div>';
}

// One volume is already summarised by the DSK cell; only a multi-volume device
// needs the breakdown, so single-disk rows stay exactly as compact as before.
function deviceDisks(d) {
    const disks = Array.isArray(d.disks) ? d.disks : [];
    if (disks.length < 2) return '';
    return '<div class="device-disks">' + disks.map(diskRow).join('') + '</div>';
}

// Online rows read "kind · up 3d 4h · online"; offline rows keep their
// last-seen time and say "offline". The status is text as well as the dot, so
// assistive tech does not depend on colour.
//
// A guest's kind label says the least about it: on pve every sibling reads
// "LXC guests". The role says what the box actually does ("T3 Code",
// "Hermes"), so it replaces the label whenever an agent reports one.
function deviceRole(d) {
    return typeof d.role === 'string' ? d.role.trim() : '';
}

function deviceSub(d) {
    const role = deviceRole(d);
    if (!d.online) {
        return (d.last_seen ? fmtAgo(d.last_seen) : (role || kindLabel(d.kind))) + ' · offline';
    }
    const label = role || kindLabel(d.kind);
    // A guest's sub-line shares one ~130 px column with three metric cells:
    // "T3 Code · up 4d 7h · online" is clipped, and the tail it loses is the
    // state. So a guest keeps its role and state here, and its uptime, size
    // and vmid move into the row's tooltip.
    if (isNumber(d.vmid)) return label + ' · online';
    const parts = [label];
    if (d.uptime_s !== null && d.uptime_s !== undefined) {
        parts.push('up ' + fmtUptime(d.uptime_s));
    }
    return parts.join(' · ') + ' · online';
}

// 24 h availability is stamped by /api/stats from the history store. It sits
// beside the name, not in the sub-line: the sub-line is already tight in a
// 320 px rail and would ellipsise the number away. Absent until the first
// samples exist, so the rail never claims 100% it has not measured.
function deviceAvailability(d) {
    return typeof d.availability_24h === 'number'
        ? d.availability_24h.toFixed(1) + '%'
        : '';
}

function deviceKey(d) {
    return d.id || d.name;
}

function deviceRow(d) {
    const online = !!d.online;
    const state = online ? 'online' : 'offline';
    const availability = deviceAvailability(d);
    const warningCount = Number(d.warning_count) || 0;
    const warningLevel = d.warning_level === 'critical' ? 'critical' : 'warning';
    // The chip carries severity as text too ("!"), not colour alone.
    const warning = warningCount
        ? '<span class="device-warn is-' + warningLevel + '" title="' +
          warningCount + ' active warning(s)' +
          (warningLevel === 'critical' ? ', critical' : '') + '">' +
          warningCount + 'w' + (warningLevel === 'critical' ? '!' : '') +
          '</span>'
        : '';
    // A guest's pve id and size are worth one hover: "agent" is CT 210 on this
    // host, and `pct` and the Proxmox UI only accept the number. The rail's
    // sub-line has room for the role alone, so the rest lives here.
    const ct = isNumber(d.vmid) ? 'CT ' + d.vmid : '';
    const facts = [
        d.name,
        deviceRole(d),
        ct,
        isNumber(d.vcpu) ? d.vcpu + ' vCPU' : '',
        isNumber(d.mem_total) ? fmtBytesCompact(d.mem_total) + ' RAM' : '',
        d.uptime_s !== null && d.uptime_s !== undefined ? 'up ' + fmtUptime(d.uptime_s) : '',
    ].filter(Boolean);
    const title = ct ? ' title="' + escapeHtml(facts.join(' · ')) + '"' : '';
    return '<div class="device-row ' + state + '" data-device-id="' + escapeHtml(deviceKey(d)) + '"' + title + '>' +
        '<div class="device-id">' +
        '<span class="device-dot ' + state + '" aria-hidden="true"></span>' +
        '<span class="device-text">' +
        '<span class="device-name-row">' +
        '<span class="device-name">' + escapeHtml(d.name) + '</span>' +
        warning +
        '<span class="device-avail"' + (availability ? ' title="24h availability"' : ' hidden') + '>' +
        availability + '</span>' +
        '</span>' +
        '<span class="device-sub">' + escapeHtml(deviceSub(d)) + '</span>' +
        '<span class="device-error"' + (d.error ? '' : ' hidden') + '>' + escapeHtml(d.error || '') + '</span>' +
        '</span>' +
        '</div>' +
        DEVICE_METRICS.map((metric) => metricCell(metric, d[metric.field])).join('') +
        deviceDisks(d) +
        '</div>';
}

// Order only by kind: devices inside a kind keep the order the server sent.
// Array.prototype.sort is stable, so equal ranks never move.
function sortNetworkDevices(devices) {
    return devices.slice().sort((a, b) => kindRank(a.kind) - kindRank(b.kind));
}

// The rail's whole point is containment: PVE host, its LXC guests (with the
// Docker host and its containers), then everything that is not on pve.
function networkTree(devices, topology) {
    const stack = window.dashboardStack.group(devices, topology);
    let html = '<div class="network-tree">';

    if (stack.host) {
        html += window.dashboardStack.render.branch(
            'PVE host',
            'physical · ' + stack.host.name,
            deviceRow(stack.host),
            'is-host'
        );
    }

    let guests = '';
    if (stack.dockerHost) {
        guests += '<div class="network-node is-docker">' +
            deviceRow(stack.dockerHost) +
            window.dashboardStack.render.dockerEngine(stack.containers) +
            '</div>';
    }
    stack.guests.forEach((guest) => {
        guests += '<div class="network-node">' + deviceRow(guest) + '</div>';
    });
    if (guests) {
        html += window.dashboardStack.render.branch(
            'LXC guests',
            stack.host ? 'on ' + stack.host.name : 'on pve',
            guests,
            'is-guests'
        );
    }

    const others = sortNetworkDevices(stack.others);
    if (others.length) {
        html += window.dashboardStack.render.branch(
            'Elsewhere',
            'standalone',
            others.map(deviceRow).join(''),
            'is-elsewhere'
        );
    }

    return html + '</div>';
}

// Full render happens once and whenever the device set itself changes.
function renderNetwork(devices) {
    networkDeviceKey = networkKey(devices);
    deviceGridEl.innerHTML = devices.length
        ? networkTree(devices, networkTopology)
        : '<div class="device-empty">No devices reporting</div>';
    networkSummaryEl.textContent = networkSummary(devices);
}

// Mounts and labels are structure rather than values: if either changes, the
// rows must be rebuilt instead of patched, or the old names would go stale.
function diskKey(d) {
    const disks = Array.isArray(d.disks) ? d.disks : [];
    return disks.map((disk) => (disk.mount || '') + '~' + (disk.label || '')).join(',');
}

function networkKey(devices) {
    const topology = networkTopology || {};
    const containers = (topology.containers || [])
        .map((container) => container.name + ':' + container.state + ':' + container.role)
        .join(',');
    return devices.map((d) => deviceKey(d) + ':' + (d.kind || '') + ':' + diskKey(d) +
        ':' + (d.warning_count || 0) + ':' + (d.warning_level || '')).join('|') +
        '||' + (topology.host_id || '') + ':' + (topology.docker_host_id || '') +
        ':' + containers;
}

function patchMetric(cell, raw) {
    if (!cell) return;
    const value = clampPct(raw);
    cell.classList.toggle('is-warn', value !== null && value >= METRIC_WARN_PCT);
    cell.querySelector('.metric-value').textContent = value === null ? '—' : Math.round(value) + '%';
    cell.querySelector('.metric-fill').style.width = (value === null ? 0 : value) + '%';
}

function patchDisk(row, disk) {
    if (!row || !disk) return;
    const value = clampPct(disk.pct);
    row.classList.toggle('is-warn', value !== null && value >= METRIC_WARN_PCT);
    row.querySelector('.disk-value').textContent = value === null ? '—' : Math.round(value) + '%';
    row.querySelector('.disk-fill').style.width = (value === null ? 0 : value) + '%';
    row.querySelector('.disk-size').textContent = fmtBytesCompact(disk.total);
}

// The 10s poll changes values, not structure: existing rows keep their
// elements so bars animate and nothing flickers. Rows are matched by device
// id, not position, because the tree reorders them by containment.
function patchNetwork(devices) {
    networkSummaryEl.textContent = networkSummary(devices);

    const byId = new Map(devices.map((device) => [deviceKey(device), device]));
    deviceGridEl.querySelectorAll('.device-row').forEach((row) => {
        const d = byId.get(row.getAttribute('data-device-id'));
        if (!d) return;
        const online = !!d.online;
        row.classList.toggle('online', online);
        row.classList.toggle('offline', !online);
        row.querySelector('.device-dot').className = 'device-dot ' + (online ? 'online' : 'offline');
        row.querySelector('.device-name').textContent = d.name;
        row.querySelector('.device-sub').textContent = deviceSub(d);
        const availability = deviceAvailability(d);
        const availabilityEl = row.querySelector('.device-avail');
        availabilityEl.textContent = availability;
        availabilityEl.hidden = !availability;
        const errorEl = row.querySelector('.device-error');
        errorEl.textContent = d.error || '';
        errorEl.hidden = !d.error;
        DEVICE_METRICS.forEach((metric) => {
            patchMetric(row.querySelector('[data-metric="' + metric.key + '"]'), d[metric.field]);
        });
        const disks = Array.isArray(d.disks) ? d.disks : [];
        row.querySelectorAll('.disk-row').forEach((diskEl, i) => patchDisk(diskEl, disks[i]));
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

// ---------- Services card ----------
// Compact rows below Network: current verdict, latency, 24 h uptime and the
// certificate's remaining life. Missing numbers read as words, never zero.

function serviceMeta(check) {
    const parts = [
        isNumber(check.latency_ms) ? check.latency_ms + ' ms' : '— ms',
        isNumber(check.uptime_24h)
            ? check.uptime_24h.toFixed(1) + '% 24h'
            : 'no 24h data'
    ];
    if (isNumber(check.cert_days)) {
        parts.push(Math.round(check.cert_days) + 'd cert');
    }
    return parts.join(' · ');
}

function serviceRow(check) {
    const label = verdictLabel(check);
    const name = check.name || check.id;
    return '<div class="service-row ' + verdictClass(check) +
        '" data-check="' + escapeHtml(check.id) + '">' +
        statusDotHTML(check) +
        '<span class="service-main">' +
        '<span class="service-name" title="' + escapeHtml(name) + '">' +
        escapeHtml(name) + '</span>' +
        '<span class="service-meta">' + escapeHtml(serviceMeta(check)) + '</span>' +
        '</span>' +
        '<span class="service-verdict">' + escapeHtml(label) + '</span>' +
        '</div>';
}

function servicesSummary(checks) {
    if (!checks.length) return 'no checks';
    const up = checks.filter((check) => check && check.verdict === 'ok').length;
    return up + '/' + checks.length + ' up';
}

function renderServices(payload) {
    const checks = checksOf(payload);
    if (servicesSummaryEl) servicesSummaryEl.textContent = servicesSummary(checks);
    if (!servicesListEl) return;
    servicesListEl.innerHTML = checks.length
        ? checks.map(serviceRow).join('')
        : '<div class="services-empty">No service checks</div>';
}

function checkSignatureOf(payload) {
    return checksOf(payload).map((check) => (check && check.id) + ':' +
        (check && check.verdict)).join('|');
}

// The first paint has the bookmark store but not the checks. Once the poll
// lands, refresh the Services card every time; rebuild the launcher surfaces
// only when a verdict actually changed, so list scroll and selection survive.
function applyChecks(payload) {
    checkState = checkMap(payload);
    renderServices(payload);
    const signature = checkSignatureOf(payload);
    const changed = signature !== checkSignature;
    checkSignature = signature;
    if (changed && bookmarksRendered) {
        renderAllTools(bookmarkStore);
        renderLaunchpad(bookmarkStore);
    }
}

async function refreshDevices() {
    const [result, checks] = await Promise.all([
        window.dashData.read('/api/stats', 'stats'),
        window.dashData.read('/api/checks', 'service checks'),
    ]);
    // A failed checks poll keeps the last good card; only the stats failure
    // drives the poller's backoff and the stale label.
    if (checks.ok) applyChecks(checks.data);
    if (!result.ok) {
        // Keep the last good reading and label it stale: a failed poll must
        // never erase the panel or report a false all-clear.
        setNetworkStale(true);
        renderHealth(networkDevices);
        return false;
    }
    const data = result.data;
    networkDevices = sortNetworkDevices(data.devices || []);
    networkTopology = data.topology || null;
    networkUpdatedAt = data.generated || Math.floor(Date.now() / 1000);
    networkUpdatedEl.textContent = updatedLabel(networkUpdatedAt);
    setNetworkStale(false);
    applyNetworkDevices(networkDevices);
    renderHealth(networkDevices);
    return true;
}

// The shared poller refreshes every 10 s and backs off to a minute after
// repeated failures, pausing while the tab is hidden.
function startDevicePolling() {
    if (!devicePoller) {
        devicePoller = window.dashboardUI.createPoller({
            interval: 10000,
            maxInterval: 60000,
            run: refreshDevices,
        });
    }
    devicePoller.start();
}
