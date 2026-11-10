const WORK_START_HOUR = 9;
const WORK_END_HOUR = 17;
const MOST_USED_MIN_SCORE = 0.5;
const MOST_USED_LIMIT = 6;

const bookmarks = {
    work: [
        {
            category: 'Google Workspace',
            items: [
                { title: 'Gmail', desc: 'Email', icon: '📧', url: 'https://mail.google.com' },
                { title: 'Google Calendar', desc: 'Meetings & scheduling', icon: '📅', url: 'https://calendar.google.com' },
                { title: 'Google Sheets', desc: 'Spreadsheets & data', icon: '📊', url: 'https://docs.google.com/spreadsheets' },
                { title: 'Google Docs', desc: 'Documents', icon: '📝', url: 'https://docs.google.com/document' },
                { title: 'Google Forms', desc: 'Forms', icon: '📋', url: 'https://docs.google.com/forms' },
                { title: 'Shared Drive', desc: 'Team storage', icon: '📁', url: 'https://drive.google.com/drive/u/0/folders/0AAZUVdPYQtvyUk9PVA' },
                { title: 'Data Studio', desc: 'Dashboards & reporting', icon: '📈', url: 'https://lookerstudio.google.com' }
            ]
        },
        {
            category: 'Automation',
            items: [
                { title: 'OpenAI API Dashboard', desc: 'AI & model management', icon: '🤖', url: 'https://platform.openai.com' },
                { title: 'Ollama', desc: 'Usage & settings', icon: '🦙', url: 'https://ollama.com/settings' },
                { title: 'Make.com', desc: 'Workflow automation', icon: '⚙️', url: 'https://www.make.com' },
                { title: 'GitHub', desc: 'Code repository', icon: '🐙', url: 'https://github.com/jemini-pro' },
                { title: 'Jules', desc: 'Internal link', icon: '🤖', url: 'https://jules.google.com' }
            ]
        },
        {
            category: 'Analytics & Collaboration',
            items: [
                { title: 'Lucidchart', desc: 'Diagrams & flowcharts', icon: '📐', url: 'https://lucid.app' },
                { title: 'AWS', desc: 'Cloud platform & login', icon: '☁️', url: 'https://d-9c67692c29.awsapps.com/start/' },
                { title: 'Slack', desc: 'Team communication', icon: '💬', url: 'https://slack.com' },
                { title: 'NIRA', desc: 'File storage', icon: '☁️', url: 'https://nira.com' }
            ]
        },
        {
            category: 'Social Media',
            items: [
                { title: 'Instagram', desc: 'Content & engagement', icon: '📸', url: 'https://www.instagram.com/surveyair/' },
                { title: 'LinkedIn', desc: 'Networking', icon: '💼', url: 'https://www.linkedin.com/company/survey-air' },
                { title: 'YouTube', desc: 'Video content', icon: '🎥', url: 'https://www.youtube.com/@surveyair' }
            ]
        },
        {
            category: 'Work Tools',
            items: [
                { title: 'Portal', desc: 'Survey Air portal', icon: '🔑', url: 'https://clients.surveyair.co.uk/login/' },
                { title: 'Website', desc: 'Survey Air website', icon: '🌐', url: 'https://surveyair.co.uk/' }
            ]
        },
        {
            category: 'Homelab',
            items: [
                { title: 'Proxmox', desc: 'Host UI', icon: '🖥️', url: 'https://pve.tail73491d.ts.net/' },
                { title: 'T3 Code', desc: 'Agent console', icon: '🤖', url: 'https://pve.tail73491d.ts.net:8443/' },
                { title: 'Tailscale', desc: 'Machines & ACLs', icon: '🔗', url: 'https://login.tailscale.com/admin/machines' }
            ]
        }
    ],
    personal: [
        {
            category: 'Entertainment',
            items: [
                { title: 'YouTube', desc: 'Video streaming', url: 'https://youtube.com', icon: '📺' },
                { title: 'Netflix', desc: 'Movies & TV', url: 'https://netflix.com', icon: '🍿' }
            ]
        },
        {
            category: 'Social',
            items: [
                { title: 'Reddit', desc: 'Community', url: 'https://reddit.com', icon: '👽' },
                { title: 'Twitter', desc: 'News & updates', url: 'https://twitter.com', icon: '🐦' },
                { title: 'GitHub', desc: 'Personal projects', url: 'https://github.com/jem-cell', icon: '🐙' }
            ]
        },
        {
            category: 'Tools',
            items: [
                { title: 'Spotify', desc: 'Music', url: 'https://spotify.com', icon: '🎵' },
                { title: 'Amazon', desc: 'Shopping', url: 'https://amazon.com', icon: '🛒' }
            ]
        }
    ]
};

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
        renderBookmarks(bookmarks.work);   // instant paint from the embedded copy…
        bookmarksRendered = true;
        loadBookmarks();                   // …then replace it from the server store
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

function renderBookmarks(categories) {
    // If the data is flat (old personal array structure fallback), wrap it
    if (categories.length > 0 && !categories[0].category) {
        categories = [{ category: 'Bookmarks', items: categories }];
    }

    bookmarksGridEl.innerHTML = categories.map(cat => `
        <div class="category-section">
            <h2 class="category-title">${cat.category}</h2>
            <div class="items-grid">
                ${cat.items.map(item => bookmarkCard(item)).join('')}
            </div>
        </div>
    `).join('');
}

// The Most used strip draws from the decayed `score` the server adds to each
// item; the scoreless embedded copy used for the first paint never qualifies.
// This picks copies for the strip and leaves the category order untouched.
function selectMostUsed(categories, minScore = MOST_USED_MIN_SCORE, limit = MOST_USED_LIMIT) {
    const scored = [];
    (categories || []).forEach(cat => {
        (cat.items || []).forEach(item => {
            const score = Number(item.score) || 0;
            if (score > minScore) scored.push({ item, score });
        });
    });
    return scored
        .sort((a, b) => b.score - a.score)
        .slice(0, limit)
        .map(entry => entry.item);
}

function renderMostUsed(categories) {
    const items = selectMostUsed(categories);
    if (!items.length) {
        mostUsedEl.hidden = true;
        mostUsedEl.innerHTML = '';
        return;
    }
    mostUsedEl.innerHTML = `
        <h2 class="category-title">Most used</h2>
        <div class="items-grid">
            ${items.map(item => bookmarkCard(item)).join('')}
        </div>
    `;
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

async function loadBookmarks() {
    try {
        const res = await fetch('/api/bookmarks', { cache: 'no-store' });
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const data = await res.json();
        if (data && Array.isArray(data.work) && data.work.length) {
            renderBookmarks(data.work);
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
        '<span class="device-name">' + d.name + '</span>' +
        '<span class="device-kind">' + sub + '</span>' +
        '</div>' +
        (d.error ? '<div class="device-error">' + d.error + '</div>' : '') +
        metricRow('CPU', d.cpu_pct) +
        metricRow('RAM', d.mem_pct, d.mem_total ? fmtBytes(d.mem_used) + ' / ' + fmtBytes(d.mem_total) : '') +
        ((d.disk_pct === null || d.disk_pct === undefined) ? '' : metricRow('Disk', d.disk_pct)) +
        '<div class="device-foot">up ' + fmtUptime(d.uptime_s) + '</div>' +
        '</div>';
}

async function refreshDevices() {
    try {
        const res = await fetch('/api/stats', { cache: 'no-store' });
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const data = await res.json();
        const order = { host: 0, lxc: 1, 'lxc-guest': 2, pc: 3, mac: 4 };
        const devices = (data.devices || []).slice()
            .sort((a, b) => (order[a.kind] === undefined ? 9 : order[a.kind]) - (order[b.kind] === undefined ? 9 : order[b.kind]));
        deviceGridEl.innerHTML = devices.length
            ? devices.map(deviceCard).join('')
            : '<div class="device-empty">No devices reporting</div>';
        networkUpdatedEl.textContent = 'updated ' + new Date(data.generated * 1000).toLocaleTimeString();
    } catch (e) {
        // No API (e.g. the repo served as plain static files): hide the panel
        // rather than show a permanent error card.
        const panel = document.querySelector('.network-panel');
        if (panel) panel.style.display = 'none';
    }
}

function startDevicePolling() {
    refreshDevices();
    setInterval(refreshDevices, 10000);
}
