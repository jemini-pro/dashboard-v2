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
const KIND_LABELS = { host: 'Hosts', lxc: 'LXC', 'lxc-guest': 'LXC guests', pc: 'PC', mac: 'Mac' };
const KIND_ORDER = ['host', 'lxc', 'lxc-guest', 'pc', 'mac'];

const bookmarks = {
    work: [
        {
            category: 'Google Workspace',
            items: [
                { title: 'Gmail', desc: 'Email', url: 'https://mail.google.com' },
                { title: 'Google Calendar', desc: 'Meetings & scheduling', url: 'https://calendar.google.com' },
                { title: 'Google Sheets', desc: 'Spreadsheets & data', url: 'https://docs.google.com/spreadsheets' },
                { title: 'Google Docs', desc: 'Documents', url: 'https://docs.google.com/document' },
                { title: 'Google Forms', desc: 'Forms', url: 'https://docs.google.com/forms' },
                { title: 'Shared Drive', desc: 'Team storage', url: 'https://drive.google.com/drive/u/0/folders/0AAZUVdPYQtvyUk9PVA' },
                { title: 'Data Studio', desc: 'Dashboards & reporting', url: 'https://lookerstudio.google.com' }
            ]
        },
        {
            category: 'Automation',
            items: [
                { title: 'OpenAI API Dashboard', desc: 'AI & model management', url: 'https://platform.openai.com' },
                { title: 'Ollama', desc: 'Usage & settings', url: 'https://ollama.com/settings' },
                { title: 'Make.com', desc: 'Workflow automation', url: 'https://www.make.com' },
                { title: 'GitHub', desc: 'Code repository', url: 'https://github.com/jemini-pro' },
                { title: 'Jules', desc: 'Internal link', url: 'https://jules.google.com' }
            ]
        },
        {
            category: 'Analytics & Collaboration',
            items: [
                { title: 'Lucidchart', desc: 'Diagrams & flowcharts', url: 'https://lucid.app' },
                { title: 'AWS', desc: 'Cloud platform & login', url: 'https://d-9c67692c29.awsapps.com/start/' },
                { title: 'Slack', desc: 'Team communication', url: 'https://slack.com' },
                { title: 'NIRA', desc: 'File storage', url: 'https://nira.com' }
            ]
        },
        {
            category: 'Social Media',
            items: [
                { title: 'Instagram', desc: 'Content & engagement', url: 'https://www.instagram.com/surveyair/' },
                { title: 'LinkedIn', desc: 'Networking', url: 'https://www.linkedin.com/company/survey-air' },
                { title: 'YouTube', desc: 'Video content', url: 'https://www.youtube.com/@surveyair' }
            ]
        },
        {
            category: 'Work Tools',
            items: [
                { title: 'Portal', desc: 'Survey Air portal', url: 'https://clients.surveyair.co.uk/login/' },
                { title: 'Website', desc: 'Survey Air website', url: 'https://surveyair.co.uk/' }
            ]
        },
        {
            category: 'Homelab',
            items: [
                { title: 'Proxmox', desc: 'Host UI', url: 'https://pve.tail73491d.ts.net/' },
                { title: 'T3 Code', desc: 'Agent console', url: 'https://pve.tail73491d.ts.net:8443/' },
                { title: 'Tailscale', desc: 'Machines & ACLs', url: 'https://login.tailscale.com/admin/machines' }
            ]
        }
    ],
    personal: [
        {
            category: 'Entertainment',
            items: [
                { title: 'YouTube', desc: 'Video streaming', url: 'https://youtube.com' },
                { title: 'Netflix', desc: 'Movies & TV', url: 'https://netflix.com' }
            ]
        },
        {
            category: 'Social',
            items: [
                { title: 'Reddit', desc: 'Community', url: 'https://reddit.com' },
                { title: 'Twitter', desc: 'News & updates', url: 'https://twitter.com' },
                { title: 'GitHub', desc: 'Personal projects', url: 'https://github.com/jem-cell' }
            ]
        },
        {
            category: 'Tools',
            items: [
                { title: 'Spotify', desc: 'Music', url: 'https://spotify.com' },
                { title: 'Amazon', desc: 'Shopping', url: 'https://amazon.com' }
            ]
        }
    ]
};

const clockEl = document.getElementById('clock');
const dateEl = document.getElementById('date');
const searchInput = document.getElementById('search');
const healthDotEl = document.getElementById('health-dot');
const healthTextEl = document.getElementById('health-text');
const bookmarksGridEl = document.getElementById('bookmarks-grid');
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
// Embedded copy paints first; /api/bookmarks replaces it when the server is up.
let bookmarkStore = bookmarks.work;
let networkDevices = [];
// Keyed by device identity so a poll can patch existing rows, and null until
// the first successful payload so the initial "Loading devices…" is replaced.
let networkDeviceKey = null;
let networkUpdatedAt = null;
let searchQuery = '';

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
        renderAllTools(bookmarkStore);        // instant paint from the embedded copy…
        renderLaunchpad(bookmarkStore);
        loadBookmarks();                      // …then replace it from the server store
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

function bookmarkCard(item) {
    return `
        <a href="${escapeHtml(item.url)}" class="bookmark-card" target="_blank" rel="noreferrer">
            <div class="bookmark-icon-text">${monogram(item)}</div>
            <div class="bookmark-details">
                <div class="bookmark-title">${escapeHtml(item.title)}</div>
                <div class="bookmark-desc">${escapeHtml(item.desc || '')}</div>
            </div>
        </a>
    `;
}

// Category grid skeleton for All tools. The Jump back in card has its own
// compact row renderer below, so the two layouts stay independent.
function categorySection(title, items) {
    return `
        <div class="category-section">
            <h2 class="category-title">${escapeHtml(title)}</h2>
            <div class="items-grid">
                ${items.map(item => bookmarkCard(item)).join('')}
            </div>
        </div>
    `;
}

// All tools render hook. #42 replaces this with numbered, searchable rows; the
// shell keeps the shared card renderer so the embedded copy still paints.
function renderAllTools(categories) {
    renderBookmarks(categories);
}

// Launchpad selection. A store item opts in with `pinned: true`; only when
// nothing opts in do the fallback titles stand in. Either way the store order
// is preserved and the result is capped, independent of the embedded copy size.
function selectLaunchpad(categories) {
    const items = [];
    (categories || []).forEach(cat => {
        (cat.items || []).forEach(item => items.push(item));
    });
    const pinned = items.filter(item => item && item.pinned === true);
    if (!pinned.length) {
        return items
            .filter(item => item && PINNED_FALLBACK_TITLES.includes(item.title))
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

function renderBookmarks(categories) {
    // If the data is flat (old personal array structure fallback), wrap it
    if (categories.length > 0 && !categories[0].category) {
        categories = [{ category: 'Bookmarks', items: categories }];
    }

    bookmarksGridEl.innerHTML = categories.map(
        cat => categorySection(cat.category, cat.items)
    ).join('');
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

// Record bookmark activation server-side, for both All tools cards and Jump
// back in rows. sendBeacon survives the navigation that a plain fetch would
// cancel; auxclick covers middle-click, while plain and ctrl/cmd clicks arrive
// as click.
function recordBookmarkHit(event) {
    if (event.type === 'auxclick' && event.button !== 1) return;
    const card = event.target.closest('.bookmark-card, .usage-row');
    if (!card) return;
    const url = card.getAttribute('href');
    if (!url) return;
    navigator.sendBeacon('/api/hit', JSON.stringify({ url }));
}

bookmarksGridEl.addEventListener('click', recordBookmarkHit);
bookmarksGridEl.addEventListener('auxclick', recordBookmarkHit);
mostUsedEl.addEventListener('click', recordBookmarkHit);
mostUsedEl.addEventListener('auxclick', recordBookmarkHit);

// Launchpad tiles share the same beacon contract as bookmark cards.
function recordLaunchpadHit(event) {
    if (event.type === 'auxclick' && event.button !== 1) return;
    const tile = event.target.closest('.launchpad-tile');
    if (!tile) return;
    const url = tile.getAttribute('href');
    if (!url) return;
    navigator.sendBeacon('/api/hit', JSON.stringify({ url }));
}

launchpadGridEl.addEventListener('click', recordLaunchpadHit);
launchpadGridEl.addEventListener('auxclick', recordLaunchpadHit);

// ---------- search ----------

function isTypingTarget(target) {
    if (!target) return false;
    const tag = target.tagName;
    return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || !!target.isContentEditable;
}

// #42 owns the All tools filtering. The shell exposes the seam the field writes
// to and leaves the full list visible until then.
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
        const res = await fetch('/api/bookmarks', { cache: 'no-store' });
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const data = await res.json();
        if (data && Array.isArray(data.work) && data.work.length) {
            bookmarkStore = data.work;
            renderAllTools(bookmarkStore);
            renderLaunchpad(bookmarkStore);
            renderMostUsed(data.work);
        }
    } catch (e) {
        console.warn('bookmark store unreachable, keeping the embedded copy', e);
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

function networkSummary(devices) {
    const total = devices.length;
    if (!total) return 'no device data';
    const online = devices.filter((d) => d.online).length;
    const cpu = averageSample(devices, 'cpu_pct');
    return online + '/' + total + ' online · ' +
        (cpu === null ? '—' : Math.round(cpu) + '% avg CPU');
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
                '<span class="kind-meta">' + avgText(averageSample(group, 'cpu_pct')) + ' cpu · ' +
                avgText(averageSample(group, 'mem_pct')) + ' ram</span>' +
                '</div>';
        })
        .join('');
}

function updatedLabel(timestamp) {
    if (!timestamp) return '';
    return 'updated ' + new Date(timestamp * 1000).toLocaleTimeString([], {
        hour: '2-digit', minute: '2-digit'
    });
}

function metricCell(label, key, raw) {
    const value = clampPct(raw);
    const warn = value !== null && value >= METRIC_WARN_PCT ? ' is-warn' : '';
    return '<span class="metric' + warn + '" data-metric="' + key + '">' +
        '<span class="metric-label">' + label + '</span>' +
        '<span class="metric-value">' + (value === null ? '—' : Math.round(value) + '%') + '</span>' +
        '<span class="metric-bar"><i class="metric-fill" style="width:' + (value === null ? 0 : value) + '%"></i></span>' +
        '</span>';
}

// Online rows read "kind · up 3d 4h"; offline rows keep their last-seen time
// so a missing heartbeat is legible without opening anything.
function deviceSub(d) {
    if (!d.online) {
        return d.last_seen ? fmtAgo(d.last_seen) : kindLabel(d.kind);
    }
    const kind = kindLabel(d.kind);
    return d.uptime_s === null || d.uptime_s === undefined
        ? kind
        : kind + ' · up ' + fmtUptime(d.uptime_s);
}

function deviceRow(d) {
    const online = !!d.online;
    const state = online ? 'online' : 'offline';
    return '<div class="device-row ' + state + '" data-device-id="' + escapeHtml(d.id || d.name) + '">' +
        '<div class="device-id">' +
        '<span class="device-dot ' + state + '" aria-hidden="true"></span>' +
        '<span class="device-text">' +
        '<span class="device-name">' + escapeHtml(d.name) + '</span>' +
        '<span class="device-sub">' + escapeHtml(deviceSub(d)) + '</span>' +
        '<span class="device-error"' + (d.error ? '' : ' hidden') + '>' + escapeHtml(d.error || '') + '</span>' +
        '</span>' +
        '</div>' +
        metricCell('CPU', 'cpu', d.cpu_pct) +
        metricCell('RAM', 'ram', d.mem_pct) +
        metricCell('DSK', 'disk', d.disk_pct) +
        '</div>';
}

function sortNetworkDevices(devices) {
    return devices.slice().sort((a, b) =>
        kindRank(a.kind) - kindRank(b.kind) ||
        String(a.name || '').localeCompare(String(b.name || ''))
    );
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
    return devices.map((d) => (d.id || d.name) + ':' + (d.kind || '')).join('|');
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
        patchMetric(row.querySelector('[data-metric="cpu"]'), d.cpu_pct);
        patchMetric(row.querySelector('[data-metric="ram"]'), d.mem_pct);
        patchMetric(row.querySelector('[data-metric="disk"]'), d.disk_pct);
    });

    const groups = groupDevicesByKind(devices);
    kindChipsEl.querySelectorAll('.kind-chip').forEach((chip) => {
        const group = groups[chip.getAttribute('data-kind')] || [];
        chip.querySelector('.kind-count').textContent = String(group.length);
        chip.querySelector('.kind-meta').textContent =
            avgText(averageSample(group, 'cpu_pct')) + ' cpu · ' +
            avgText(averageSample(group, 'mem_pct')) + ' ram';
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
    const total = devices.length;
    const online = devices.filter((d) => d.online).length;
    const status = total === 0 ? 'unknown' : (online === total ? 'online' : 'warn');
    healthDotEl.className = 'health-dot ' + status;
    healthTextEl.textContent = total ? online + '/' + total + ' online' : 'no devices';
}

async function refreshDevices() {
    try {
        const res = await fetch('/api/stats', { cache: 'no-store' });
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const data = await res.json();
        networkDevices = sortNetworkDevices(data.devices || []);
        networkUpdatedAt = data.generated || Math.floor(Date.now() / 1000);
        networkUpdatedEl.textContent = updatedLabel(networkUpdatedAt);
        setNetworkStale(false);
        applyNetworkDevices(networkDevices);
        renderHealth(networkDevices);
    } catch (e) {
        // Keep the last good reading and label it stale: a failed poll must
        // never erase the panel or report a false all-clear.
        console.warn('stats unreachable, keeping the last good reading', e);
        setNetworkStale(true);
        renderHealth(networkDevices);
    }
}

function startDevicePolling() {
    refreshDevices();
    setInterval(refreshDevices, 10000);
}
