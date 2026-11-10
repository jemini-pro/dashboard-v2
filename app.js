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
const resultCountEl = document.getElementById('result-count');
const mostUsedEl = document.getElementById('most-used');
const launchpadGridEl = document.getElementById('launchpad-grid');
const deviceGridEl = document.getElementById('device-grid');
const networkUpdatedEl = document.getElementById('network-updated');
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
// query, the same slot shows the full count and group count.
function resultCountText(shown, total, groups, query) {
    return String(query || '').trim()
        ? shown + ' of ' + total + ' tools'
        : total + ' tools · ' + groups + ' groups';
}

function renderAllTools(categories) {
    const all = categories || [];
    const filtered = filterToolCategories(all, searchQuery);
    const shown = countToolItems(filtered);
    const total = countToolItems(all);

    if (resultCountEl) {
        resultCountEl.textContent = resultCountText(shown, total, all.length, searchQuery);
    }

    bookmarksGridEl.innerHTML = filtered.length
        ? filtered.map(cat => toolCategorySection(cat)).join('')
        : '<div class="tool-empty">' + escapeHtml(allToolsEmptyText(searchQuery)) + '</div>';
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

// All tools rows use the same beacon path as every other bookmark link. This
// is delegated from the list container so re-rendering after a search does not
// need to re-attach per-row listeners.
function recordToolHit(event) {
    if (event.type === 'auxclick' && event.button !== 1) return;
    const link = event.target.closest('.tool-link');
    if (!link) return;
    const url = link.getAttribute('href');
    if (!url) return;
    navigator.sendBeacon('/api/hit', JSON.stringify({ url }));
}

bookmarksGridEl.addEventListener('click', recordBookmarkHit);
bookmarksGridEl.addEventListener('auxclick', recordBookmarkHit);
bookmarksGridEl.addEventListener('click', recordToolHit);
bookmarksGridEl.addEventListener('auxclick', recordToolHit);
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

function metricRow(label, pct, extra) {
    const v = (pct === null || pct === undefined) ? null : Math.max(0, Math.min(100, pct));
    return '<div class="metric-row">' +
        '<span class="metric-label">' + label + '</span>' +
        '<span class="metric-value">' + (v === null ? '—' : v.toFixed(1) + '%') + '</span>' +
        '<div class="metric-bar"><div class="metric-fill" style="width:' + (v === null ? 0 : v) + '%"></div></div>' +
        '<span class="metric-extra">' + (extra || '') + '</span>' +
        '</div>';
}

function deviceCard(d) {
    const state = d.online ? 'online' : 'offline';
    const sub = (d.source === 'agent' && d.last_seen) ? fmtAgo(d.last_seen) : (d.kind || '');
    return '<div class="device-card ' + state + '">' +
        '<div class="device-head">' +
        '<span class="device-dot ' + state + '"></span>' +
        '<span class="device-name">' + escapeHtml(d.name) + '</span>' +
        '<span class="device-kind">' + escapeHtml(sub) + '</span>' +
        '</div>' +
        (d.error ? '<div class="device-error">' + escapeHtml(d.error) + '</div>' : '') +
        metricRow('CPU', d.cpu_pct) +
        metricRow('RAM', d.mem_pct, d.mem_total ? fmtBytes(d.mem_used) + ' / ' + fmtBytes(d.mem_total) : '') +
        ((d.disk_pct === null || d.disk_pct === undefined) ? '' : metricRow('Disk', d.disk_pct)) +
        '<div class="device-foot">up ' + fmtUptime(d.uptime_s) + '</div>' +
        '</div>';
}

// Network render hook. #45 replaces the device cards with kind averages and
// compact rows.
function renderNetwork(devices) {
    deviceGridEl.innerHTML = devices.length
        ? devices.map(deviceCard).join('')
        : '<div class="device-empty">No devices reporting</div>';
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
        const order = { host: 0, lxc: 1, 'lxc-guest': 2, pc: 3, mac: 4 };
        networkDevices = (data.devices || []).slice()
            .sort((a, b) => (order[a.kind] === undefined ? 9 : order[a.kind]) - (order[b.kind] === undefined ? 9 : order[b.kind]));
        networkUpdatedEl.textContent = 'updated ' + new Date(data.generated * 1000).toLocaleTimeString();
        renderNetwork(networkDevices);
        renderHealth(networkDevices);
    } catch (e) {
        // Keep the last good reading: a failed poll must not erase the panel or
        // report a false all-clear. #45 adds the stale indicator.
        console.warn('stats unreachable, keeping the last good reading', e);
        renderHealth(networkDevices);
    }
}

function startDevicePolling() {
    refreshDevices();
    setInterval(refreshDevices, 10000);
}
