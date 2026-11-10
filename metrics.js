const WORK_START_HOUR = 9;
const WORK_END_HOUR = 17;
const DEFAULT_RANGE = '24h';
const RANGES = ['1h', '6h', '24h', '7d', '30d'];
const REFRESH_MS = 60000;

// One definition drives the lines, the legend and the table. Fields index into
// the compact point arrays the API sends: [ts, cpu, mem, disk, online].
const SERIES = [
    { key: 'cpu', field: 1, label: 'CPU' },
    { key: 'ram', field: 2, label: 'RAM' },
    { key: 'disk', field: 3, label: 'Disk' }
];

const KIND_DEFS = [
    { key: 'host', label: 'Hosts' },
    { key: 'lxc', label: 'LXC' },
    { key: 'lxc-guest', label: 'LXC guests' },
    { key: 'pc', label: 'PC' },
    { key: 'mac', label: 'Mac' }
];
const KIND_LABELS = Object.fromEntries(KIND_DEFS.map((kind) => [kind.key, kind.label]));

const clockEl = document.getElementById('clock');
const healthDotEl = document.getElementById('health-dot');
const healthTextEl = document.getElementById('health-text');
const rangeTabsEl = document.getElementById('range-tabs');
const statGridEl = document.getElementById('stat-grid');
const staleEl = document.getElementById('metrics-stale');
const chartTitleEl = document.getElementById('chart-title');
const chartSubEl = document.getElementById('chart-sub');
const chartLegendEl = document.getElementById('chart-legend');
const chartEl = document.getElementById('chart');
const chartYAxisEl = document.getElementById('chart-yaxis');
const chartXAxisEl = document.getElementById('chart-xaxis');
const chartEmptyEl = document.getElementById('chart-empty');
const tableViewEl = document.getElementById('table-view');
const chartTableEl = document.getElementById('chart-table');
const deviceListEl = document.getElementById('device-list');
const devicesSubEl = document.getElementById('devices-sub');
const eventsEl = document.getElementById('events');
const eventsSubEl = document.getElementById('events-sub');
const debugTimeInput = document.getElementById('debug-time');
const resetTimeBtn = document.getElementById('reset-time');
const debugMenuBtn = document.getElementById('debug-menu-btn');
const debugControls = document.getElementById('debug-controls');

let debugTimeOffset = null;
let currentTheme = null;
let activeRange = DEFAULT_RANGE;
let selectedId = null;
let summary = null;
let series = null;
let refreshTimer = null;
let loadToken = 0;
let chartGeometry = null;

// ---------- clock, theme and debug controls (same rules as the dashboard) ----------

debugMenuBtn.addEventListener('click', (event) => {
    event.stopPropagation();
    debugControls.classList.toggle('visible');
    debugMenuBtn.classList.toggle('active');
});

document.addEventListener('click', (event) => {
    if (!debugControls.contains(event.target) && !debugMenuBtn.contains(event.target)) {
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

function updateClock() {
    const now = getCurrentTime();
    const hours = now.getHours();
    const minutes = now.getMinutes();
    const timeString = String(hours).padStart(2, '0') + ':' + String(minutes).padStart(2, '0');
    if (clockEl.textContent !== timeString) clockEl.textContent = timeString;
    if (clockEl.dateTime !== timeString) clockEl.dateTime = timeString;

    const theme = (hours >= WORK_START_HOUR && hours < WORK_END_HOUR) ? 'work' : 'personal';
    if (theme !== currentTheme) {
        currentTheme = theme;
        if (theme === 'work') {
            document.documentElement.removeAttribute('data-theme');
        } else {
            document.documentElement.setAttribute('data-theme', 'personal');
        }
    }
}

debugTimeInput.addEventListener('change', (event) => {
    if (!event.target.value) return;
    const [h, m] = event.target.value.split(':').map(Number);
    const now = new Date();
    const debugDate = new Date();
    debugDate.setHours(h, m, 0, 0);
    debugTimeOffset = debugDate.getTime() - now.getTime();
    updateClock();
});

resetTimeBtn.addEventListener('click', () => {
    debugTimeOffset = null;
    debugTimeInput.value = '';
    updateClock();
});

// ---------- helpers ----------

function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>"']/g, (c) => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[c]));
}

function isNumber(value) {
    return typeof value === 'number' && Number.isFinite(value);
}

function clamp(value, low, high) {
    return Math.max(low, Math.min(high, value));
}

function kindLabel(kind) {
    if (KIND_LABELS[kind]) return KIND_LABELS[kind];
    const text = String(kind || '').replace(/[-_]+/g, ' ').trim();
    if (!text) return 'Other';
    return text.charAt(0).toUpperCase() + text.slice(1);
}

function pct(value) {
    return isNumber(value) ? Math.round(clamp(value, 0, 100)) + '%' : '—';
}

function hhmm(ts) {
    const date = new Date(ts * 1000);
    return String(date.getHours()).padStart(2, '0') + ':' + String(date.getMinutes()).padStart(2, '0');
}

function pointTime(ts, range) {
    const date = new Date(ts * 1000);
    if (range === '7d' || range === '30d') {
        return date.toLocaleDateString('en-GB', { day: 'numeric', month: 'short' });
    }
    return hhmm(ts);
}

function fullTime(ts) {
    const date = new Date(ts * 1000);
    const day = date.toLocaleDateString('en-GB', { day: 'numeric', month: 'short' });
    return day + ' · ' + hhmm(ts);
}

function fmtDuration(seconds) {
    if (!isNumber(seconds)) return '';
    const s = Math.max(0, Math.round(seconds));
    const d = Math.floor(s / 86400);
    const h = Math.floor((s % 86400) / 3600);
    const m = Math.floor((s % 3600) / 60);
    if (d) return d + 'd ' + h + 'h';
    if (h) return h + 'h ' + m + 'm';
    return Math.max(1, m) + 'm';
}

function fmtDays(value) {
    if (!isNumber(value)) return null;
    if (value < 1) return '<1d';
    if (value < 10) return value.toFixed(1) + 'd';
    return Math.round(value) + 'd';
}

function stepLabel(seconds) {
    if (!isNumber(seconds)) return '';
    if (seconds < 3600) return Math.round(seconds / 60) + 'm';
    return Math.round(seconds / 3600) + 'h';
}

async function getJson(path) {
    const response = await fetch(path, { cache: 'no-store' });
    if (!response.ok) throw new Error('HTTP ' + response.status);
    return response.json();
}

function setStale(stale) {
    staleEl.hidden = !stale;
}

// ---------- state and url ----------

function readState() {
    const params = new URLSearchParams(window.location.search);
    const range = params.get('window');
    activeRange = RANGES.includes(range) ? range : DEFAULT_RANGE;
    selectedId = params.get('device') || null;
}

function syncUrl() {
    const params = new URLSearchParams();
    params.set('window', activeRange);
    if (selectedId) params.set('device', selectedId);
    const next = window.location.pathname + '?' + params.toString();
    window.history.replaceState(null, '', next);
}

function syncRangeButtons() {
    rangeTabsEl.querySelectorAll('[data-range]').forEach((button) => {
        button.setAttribute('aria-selected', String(button.dataset.range === activeRange));
    });
}

// ---------- summary tiles ----------

function statTile(label, value, note, flag) {
    return '<article class="stat-tile card">' +
        '<span class="stat-label">' + escapeHtml(label) + '</span>' +
        '<span class="stat-value">' + escapeHtml(value) + '</span>' +
        (note ? '<span class="stat-note">' + escapeHtml(note) + '</span>' : '') +
        (flag ? '<span class="stat-flag ' + flag.kind + '">' + escapeHtml(flag.text) + '</span>' : '') +
        '</article>';
}

function renderTiles() {
    const devices = (summary && summary.devices) || [];
    if (!devices.length) {
        statGridEl.innerHTML =
            statTile('Devices online', '—', 'history starts on the server') +
            statTile('Availability', '—', 'no samples yet') +
            statTile('Peak CPU', '—', 'no samples yet') +
            statTile('Disk pressure', '—', 'no samples yet');
        return;
    }

    const online = devices.filter((device) => device.online).length;
    const availabilities = devices.map((device) => device.availability_pct).filter(isNumber);
    const fleetAvailability = availabilities.length
        ? availabilities.reduce((sum, value) => sum + value, 0) / availabilities.length
        : null;

    const busiest = devices.slice()
        .filter((device) => isNumber(device.cpu_max))
        .sort((a, b) => b.cpu_max - a.cpu_max)[0];
    const fullest = devices.slice()
        .filter((device) => isNumber(device.disk_pct))
        .sort((a, b) => b.disk_pct - a.disk_pct)[0];

    const diskFlag = fullest && fullest.disk_pct >= 85
        ? { kind: 'is-critical', text: 'Critical' }
        : (fullest && fullest.disk_pct >= 75 ? { kind: 'is-warning', text: 'High' } : null);
    const diskDays = fullest ? fmtDays(fullest.days_to_90) : null;

    statGridEl.innerHTML =
        statTile('Devices online', online + '/' + devices.length,
            online === devices.length ? 'all reporting' : 'some offline') +
        statTile('Availability', isNumber(fleetAvailability) ? fleetAvailability.toFixed(1) + '%' : '—',
            activeRange + ' average') +
        statTile('Peak CPU', busiest ? pct(busiest.cpu_max) : '—',
            busiest ? busiest.name : 'no samples yet') +
        statTile('Disk pressure', fullest ? pct(fullest.disk_pct) : '—',
            fullest
                ? fullest.name + (diskDays ? ' · ~' + diskDays + ' to 90%' : ' · steady')
                : 'no samples yet',
            diskFlag);
}

// ---------- device rail ----------

function sparkSvg(values) {
    const points = (values || []).map((value, index) => ({ value, index }))
        .filter((point) => isNumber(point.value));
    if (points.length < 2) return '';
    const width = 96;
    const height = 22;
    const pad = 2;
    const min = Math.min(...points.map((point) => point.value));
    const max = Math.max(...points.map((point) => point.value));
    const span = max - min || 1;
    const last = (values || []).length - 1;
    const x = (index) => pad + (last ? (index / last) : 1) * (width - pad * 2);
    const y = (value) => pad + (1 - (value - min) / span) * (height - pad * 2);
    const path = points
        .map((point, i) => (i ? 'L' : 'M') + x(point.index).toFixed(1) + ' ' + y(point.value).toFixed(1))
        .join(' ');
    return '<svg class="spark" viewBox="0 0 ' + width + ' ' + height + '" aria-hidden="true">' +
        '<path class="spark-line" d="' + path + '"/></svg>';
}

function deviceItem(device) {
    const state = device.online ? 'online' : 'offline';
    const selected = device.id === selectedId;
    const availability = isNumber(device.availability_pct)
        ? device.availability_pct.toFixed(1) + '%'
        : '—';
    // State is text as well as a dot: colour and opacity never carry it alone.
    const stateText = kindLabel(device.kind) + ' · ' + (device.online ? 'online' : 'offline');
    return '<button type="button" class="device-item' + (selected ? ' is-selected' : '') + '" ' +
        'data-device="' + escapeHtml(device.id) + '" aria-pressed="' + String(selected) + '">' +
        '<span class="device-item-head">' +
        '<span class="device-dot ' + state + '" aria-hidden="true"></span>' +
        '<span class="device-item-name">' + escapeHtml(device.name) + '</span>' +
        '<span class="device-item-avail" title="Availability over ' + escapeHtml(activeRange) + '">' +
        availability + '</span>' +
        '</span>' +
        '<span class="device-item-kind">' + escapeHtml(stateText) + '</span>' +
        '<span class="device-item-spark">' + sparkSvg(device.spark) + '</span>' +
        '<span class="device-item-meta">CPU ' + pct(device.cpu_pct) +
        ' · RAM ' + pct(device.mem_pct) + ' · DSK ' + pct(device.disk_pct) + '</span>' +
        '</button>';
}

function renderDevices() {
    const devices = (summary && summary.devices) || [];
    if (!devices.length) {
        deviceListEl.innerHTML = '<div class="card-empty">No devices reporting</div>';
        devicesSubEl.textContent = '';
        return;
    }
    deviceListEl.innerHTML = devices.map(deviceItem).join('');
    const online = devices.filter((device) => device.online).length;
    devicesSubEl.textContent = online + '/' + devices.length + ' online';
}

function renderHealth() {
    const devices = (summary && summary.devices) || [];
    if (!devices.length) {
        healthDotEl.className = 'health-dot unknown';
        healthTextEl.textContent = 'no devices';
        return;
    }
    const online = devices.filter((device) => device.online).length;
    const status = online === devices.length ? 'online' : 'warn';
    healthDotEl.className = 'health-dot ' + status;
    healthTextEl.textContent = online + '/' + devices.length + ' online';
}

// ---------- chart ----------

function lastValidPoint(points, field) {
    for (let i = points.length - 1; i >= 0; i -= 1) {
        if (points[i][4] === 1 && isNumber(points[i][field])) return points[i];
    }
    return null;
}

function renderLegend() {
    const points = (series && series.points) || [];
    chartLegendEl.innerHTML = SERIES.map((item) => {
        const last = lastValidPoint(points, item.field);
        return '<li class="legend-item">' +
            '<span class="legend-swatch is-' + item.key + '" aria-hidden="true"></span>' +
            '<span class="legend-label">' + item.label + '</span>' +
            '<span class="legend-value">' + (last ? pct(last[item.field]) : '—') + '</span>' +
            '</li>';
    }).join('');
}

function seriesPath(points, field, xOf, yOf) {
    let path = '';
    let pen = false;
    points.forEach((point) => {
        const value = point[field];
        if (point[4] !== 1 || !isNumber(value)) {
            pen = false;
            return;
        }
        path += (pen ? 'L' : 'M') + xOf(point[0]).toFixed(1) + ' ' + yOf(value).toFixed(1) + ' ';
        pen = true;
    });
    return path.trim();
}

function renderChartHead() {
    const device = series && series.device;
    if (!device) {
        chartTitleEl.textContent = 'History';
        chartSubEl.textContent = 'no device selected';
        return;
    }
    chartTitleEl.textContent = 'History · ' + device.name;
    chartSubEl.textContent = (device.online ? 'online' : 'offline') + ' · ' + activeRange + ' · ' +
        stepLabel(series.step) + ' buckets · ' + series.summary.samples + ' samples';
}

function renderYAxis(yOf) {
    chartYAxisEl.innerHTML = [100, 75, 50, 25, 0].map((value) =>
        '<span style="top:' + yOf(value).toFixed(1) + 'px">' + value + '%</span>'
    ).join('');
}

function renderXAxis(points) {
    const count = points.length;
    if (!count) {
        chartXAxisEl.innerHTML = '';
        return;
    }
    const indexes = count > 3
        ? [0, Math.floor((count - 1) / 3), Math.floor(2 * (count - 1) / 3), count - 1]
        : [0, count - 1];
    const unique = [...new Set(indexes)];
    chartXAxisEl.innerHTML = unique.map((index) =>
        '<span>' + escapeHtml(pointTime(points[index][0], activeRange)) + '</span>'
    ).join('');
}

function renderChart() {
    const points = (series && series.points) || [];
    const hasPoints = points.length > 0;

    chartEmptyEl.hidden = hasPoints;
    chartEl.hidden = !hasPoints;
    chartYAxisEl.hidden = !hasPoints;
    chartXAxisEl.hidden = !hasPoints;
    chartLegendEl.hidden = !hasPoints;

    if (!hasPoints) {
        chartEl.innerHTML = '';
        chartYAxisEl.innerHTML = '';
        chartXAxisEl.innerHTML = '';
        chartGeometry = null;
        if (tableViewEl.open) renderTable();
        return;
    }

    const width = Math.max(320, Math.round(chartEl.clientWidth || 720));
    const height = 240;
    const pad = { top: 10, right: 12, bottom: 10, left: 0 };
    const plotW = width - pad.left - pad.right;
    const plotH = height - pad.top - pad.bottom;
    const first = points[0][0];
    const last = points[points.length - 1][0];
    const span = Math.max(1, last - first);
    const xOf = (ts) => pad.left + ((ts - first) / span) * plotW;
    const yOf = (value) => pad.top + plotH * (1 - clamp(value, 0, 100) / 100);

    renderYAxis(yOf);
    renderXAxis(points);

    const grid = [100, 75, 50, 25, 0].map((value) =>
        '<line class="chart-grid' + (value === 0 ? ' is-baseline' : '') + '" ' +
        'x1="' + pad.left + '" y1="' + yOf(value).toFixed(1) + '" ' +
        'x2="' + (width - pad.right) + '" y2="' + yOf(value).toFixed(1) + '"/>'
    ).join('');

    const lines = SERIES.map((item) =>
        '<path class="chart-line is-' + item.key + '" d="' +
        seriesPath(points, item.field, xOf, yOf) + '"/>'
    ).join('');

    const dots = SERIES.map((item) => {
        const point = lastValidPoint(points, item.field);
        if (!point) return '';
        return '<circle class="chart-dot is-' + item.key + '" cx="' + xOf(point[0]).toFixed(1) +
            '" cy="' + yOf(point[item.field]).toFixed(1) + '" r="4"/>';
    }).join('');

    chartEl.innerHTML =
        '<svg class="chart-svg" viewBox="0 0 ' + width + ' ' + height + '" ' +
        'preserveAspectRatio="none" aria-hidden="true">' + grid + lines + dots + '</svg>' +
        '<span class="chart-crosshair" hidden></span>' +
        '<span class="chart-hover-dots" hidden>' + SERIES.map((item) =>
            '<span class="chart-hover-dot is-' + item.key + '" data-series-key="' + item.key +
            '" hidden></span>'
        ).join('') + '</span>' +
        '<span class="chart-tooltip" hidden></span>';

    chartGeometry = { points, width, height, pad, plotW, plotH, first, span, xOf, yOf };
}

function nearestPointIndex(points, target) {
    let best = 0;
    for (let i = 1; i < points.length; i += 1) {
        if (Math.abs(points[i][0] - target) < Math.abs(points[best][0] - target)) best = i;
    }
    return best;
}

function handlePointer(event) {
    if (!chartGeometry) return;
    const rect = chartEl.getBoundingClientRect();
    const px = clamp(event.clientX - rect.left, 0, chartGeometry.width);
    const ratio = clamp((px - chartGeometry.pad.left) / chartGeometry.plotW, 0, 1);
    const target = chartGeometry.first + ratio * chartGeometry.span;
    const point = chartGeometry.points[nearestPointIndex(chartGeometry.points, target)];
    const x = chartGeometry.xOf(point[0]);

    const crosshair = chartEl.querySelector('.chart-crosshair');
    const dotsWrap = chartEl.querySelector('.chart-hover-dots');
    const tooltip = chartEl.querySelector('.chart-tooltip');
    if (!crosshair || !dotsWrap || !tooltip) return;

    crosshair.hidden = false;
    crosshair.style.left = x.toFixed(1) + 'px';
    dotsWrap.hidden = false;

    const online = point[4] === 1;
    SERIES.forEach((item) => {
        const dot = dotsWrap.querySelector('[data-series-key="' + item.key + '"]');
        if (!dot) return;
        const value = point[item.field];
        const show = online && isNumber(value);
        dot.hidden = !show;
        if (show) {
            dot.style.left = x.toFixed(1) + 'px';
            dot.style.top = chartGeometry.yOf(value).toFixed(1) + 'px';
        }
    });

    const rows = SERIES.map((item) => {
        const value = point[item.field];
        return '<li><span class="legend-swatch is-' + item.key + '" aria-hidden="true"></span>' +
            '<span class="tooltip-label">' + item.label + '</span>' +
            '<span class="tooltip-value">' +
            (online && isNumber(value) ? Math.round(value) + '%' : '—') + '</span></li>';
    }).join('');

    tooltip.innerHTML = '<span class="tooltip-time">' + escapeHtml(fullTime(point[0])) + '</span>' +
        '<ul class="tooltip-rows">' + rows + '</ul>' +
        (online ? '' : '<span class="tooltip-state">offline</span>');
    tooltip.hidden = false;
    const half = 82;
    tooltip.style.left = clamp(x, half, chartGeometry.width - half).toFixed(1) + 'px';
}

function hidePointer() {
    if (!chartEl) return;
    const crosshair = chartEl.querySelector('.chart-crosshair');
    const dotsWrap = chartEl.querySelector('.chart-hover-dots');
    const tooltip = chartEl.querySelector('.chart-tooltip');
    if (crosshair) crosshair.hidden = true;
    if (dotsWrap) dotsWrap.hidden = true;
    if (tooltip) tooltip.hidden = true;
}

// ---------- table and events ----------

function tableCells(point) {
    const online = point[4] === 1;
    return '<td>' + escapeHtml(fullTime(point[0])) + '</td>' +
        SERIES.map((item) => {
            const value = point[item.field];
            return '<td>' + (online && isNumber(value) ? Math.round(value) + '%' : '—') + '</td>';
        }).join('') +
        '<td>' + (online ? 'online' : 'offline') + '</td>';
}

function renderTable() {
    const body = chartTableEl.querySelector('tbody');
    const points = (series && series.points) || [];
    body.innerHTML = points.length
        ? points.slice().reverse().map((point) => '<tr>' + tableCells(point) + '</tr>').join('')
        : '<tr><td colspan="5">No samples in this window.</td></tr>';
}

function eventItem(event) {
    if (event.type === 'offline') {
        return '<li class="event is-offline">' +
            '<span class="event-mark" aria-hidden="true">▼</span>' +
            '<span class="event-body"><span class="event-label">Went offline</span>' +
            '<span class="event-time">' + escapeHtml(hhmm(event.ts)) + '</span></span></li>';
    }
    if (event.type === 'online') {
        const down = fmtDuration(event.down_s);
        return '<li class="event is-online">' +
            '<span class="event-mark" aria-hidden="true">▲</span>' +
            '<span class="event-body"><span class="event-label">Back online</span>' +
            '<span class="event-time">' + escapeHtml(hhmm(event.ts)) +
            (down ? ' · ' + down + ' down' : '') + '</span></span></li>';
    }
    return '<li class="event is-restart">' +
        '<span class="event-mark" aria-hidden="true">↻</span>' +
        '<span class="event-body"><span class="event-label">Restarted</span>' +
        '<span class="event-time">' + escapeHtml(hhmm(event.ts)) + '</span></span></li>';
}

function renderEvents() {
    const events = (series && series.events) || [];
    eventsSubEl.textContent = series && series.device ? series.device.name : '';
    eventsEl.innerHTML = events.length
        ? events.slice(0, 8).map(eventItem).join('')
        : '<li class="event-empty">No events in this window.</li>';
}

// ---------- loading ----------

async function load(full) {
    const token = ++loadToken;
    try {
        if (full) {
            const data = await getJson('/api/metrics/summary?window=' + encodeURIComponent(activeRange));
            if (token !== loadToken) return;
            summary = data;
            const ids = summary.devices.map((device) => device.id);
            if (!selectedId || !ids.includes(selectedId)) selectedId = ids[0] || null;
            renderTiles();
            renderDevices();
            renderHealth();
            syncUrl();
        }
        if (selectedId) {
            const data = await getJson('/api/metrics?device=' + encodeURIComponent(selectedId) +
                '&window=' + encodeURIComponent(activeRange));
            if (token !== loadToken) return;
            series = data;
        } else {
            series = null;
        }
        renderChartHead();
        renderLegend();
        renderChart();
        renderEvents();
        if (tableViewEl.open) renderTable();
        setStale(false);
    } catch (error) {
        if (token !== loadToken) return;
        console.warn('metrics unreachable, keeping the last good reading', error);
        setStale(true);
    }
}

function scheduleRefresh(delay) {
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(async () => {
        if (document.hidden) {
            refreshTimer = null;
            return;
        }
        await load(true);
        scheduleRefresh(REFRESH_MS);
    }, delay);
}

// ---------- wiring ----------

rangeTabsEl.addEventListener('click', (event) => {
    const button = event.target.closest('[data-range]');
    if (!button || button.dataset.range === activeRange) return;
    activeRange = button.dataset.range;
    syncRangeButtons();
    syncUrl();
    series = null;
    renderChart();
    load(true);
});

deviceListEl.addEventListener('click', (event) => {
    const button = event.target.closest('[data-device]');
    if (!button) return;
    const id = button.getAttribute('data-device');
    if (!id || id === selectedId) return;
    selectedId = id;
    renderDevices();  // move the selection without refetching every summary
    syncUrl();
    load(false);
});

chartEl.addEventListener('pointermove', handlePointer);
chartEl.addEventListener('pointerdown', handlePointer);
chartEl.addEventListener('pointerleave', hidePointer);

tableViewEl.addEventListener('toggle', () => {
    if (tableViewEl.open) renderTable();
});

// A resize changes the plot's pixel width; redraw rather than stretch.
if (typeof ResizeObserver !== 'undefined') {
    new ResizeObserver(() => {
        if (series && series.points.length) renderChart();
    }).observe(chartEl);
}

document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
        clearTimeout(refreshTimer);
        refreshTimer = null;
    } else {
        scheduleRefresh(0);
    }
});

readState();
syncRangeButtons();
updateClock();
setInterval(updateClock, 1000);
load(true);
scheduleRefresh(REFRESH_MS);
