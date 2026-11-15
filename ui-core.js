/*
 * One runtime for both pages: escaping, kind labels, byte and duration
 * formatting, the shared clock/theme/debug controls and the polling loop.
 *
 * Loaded before stack.js and the page script. Everything lives under
 * window.dashboardUI so the pages keep using plain global modules instead of a
 * bundler. Each helper has one definition here; the pages only choose the
 * display policy where they genuinely differ (see fmtBytesCompact/fmtBytes and
 * fmtUptime/fmtDuration).
 */
(function () {
    'use strict';

    const WORK_START_HOUR = 9;
    const WORK_END_HOUR = 17;

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

    function escapeHtml(value) {
        return String(value ?? '').replace(/[&<>"']/g, (c) => ({
            '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
        }[c]));
    }

    function isNumber(value) {
        return typeof value === 'number' && Number.isFinite(value);
    }

    // Known kinds get their proper plural label; anything else is made readable
    // ("nas-box" -> "Nas box") instead of leaking a raw slug into the UI.
    function kindLabel(kind) {
        if (KIND_LABELS[kind]) return KIND_LABELS[kind];
        const text = String(kind || '').replace(/[-_]+/g, ' ').trim();
        if (!text) return 'Other';
        return text.charAt(0).toUpperCase() + text.slice(1);
    }

    // Disk sizes read as "931 GB" / "1.8 TB". Binary units match what the OS
    // shows (a 1 TB drive is 931 GB here). The two pages share the scale but
    // keep their existing display policy: the dashboard omits the empty marker
    // and keeps one decimal below 100, the history table shows an em dash and
    // two decimals below 10.
    function scaleBytes(value, empty, compact) {
        if (!isNumber(value) || value <= 0) return empty;
        const units = ['B', 'KB', 'MB', 'GB', 'TB'];
        let size = value;
        let unit = 0;
        while (size >= 1024 && unit < units.length - 1) {
            size /= 1024;
            unit += 1;
        }
        const digits = unit === 0 || size >= 100 ? 0 : (compact || size >= 10 ? 1 : 2);
        return size.toFixed(digits) + ' ' + units[unit];
    }

    function fmtBytesCompact(value) {
        return scaleBytes(value, '', true);
    }

    function fmtBytes(value) {
        return scaleBytes(value, '—', false);
    }

    // Uptime (dashboard) and gap duration (history events) differ only at the
    // edges: the dashboard reads an unknown as an em dash and a sub-minute as
    // "0m", the event list omits an unknown and floors a sub-minute at "1m".
    function durationText(seconds, empty, floorMinute) {
        if (!isNumber(seconds)) return empty;
        const total = Math.max(0, Math.round(seconds));
        const d = Math.floor(total / 86400);
        const h = Math.floor((total % 86400) / 3600);
        const m = Math.floor((total % 3600) / 60);
        if (d) return d + 'd ' + h + 'h';
        if (h) return h + 'h ' + m + 'm';
        return (floorMinute ? Math.max(1, m) : m) + 'm';
    }

    function fmtUptime(seconds) {
        return durationText(seconds, '—', false);
    }

    function fmtDuration(seconds) {
        return durationText(seconds, '', true);
    }

    // ---------- clock, theme and debug controls ----------
    // Work hours (09:00-17:00) keep the light theme; everything else is
    // personal. The debug input moves the clock without touching the machine
    // clock, so a reviewer can check both themes at any hour.
    function createClock(options) {
        const settings = options || {};
        const clockEl = settings.clock;
        const dateEl = settings.date || null;
        const debugTimeInput = settings.debugTime || null;
        const resetTimeButton = settings.resetTime || null;
        const debugMenuButton = settings.debugMenu || null;
        const debugControls = settings.debugControls || null;
        let debugTimeOffset = null;
        let currentTheme = null;

        function getCurrentTime() {
            if (debugTimeOffset !== null) {
                return new Date(Date.now() + debugTimeOffset);
            }
            return new Date();
        }

        function update() {
            const now = getCurrentTime();
            const hours = now.getHours();
            const minutes = now.getMinutes();

            // Update the toolbar clock and, when this page has one, the date.
            const timeString = String(hours).padStart(2, '0') + ':' +
                String(minutes).padStart(2, '0');
            if (clockEl.textContent !== timeString) clockEl.textContent = timeString;
            if (clockEl.dateTime !== timeString) clockEl.dateTime = timeString;

            if (dateEl) {
                const dateString = now.toLocaleDateString('en-GB', {
                    weekday: 'short', day: 'numeric', month: 'short'
                });
                if (dateEl.textContent !== dateString) dateEl.textContent = dateString;
            }

            // Determine theme: work 09:00-17:00, personal otherwise.
            const theme = (hours >= WORK_START_HOUR && hours < WORK_END_HOUR)
                ? 'work'
                : 'personal';
            if (theme !== currentTheme) {
                currentTheme = theme;
                if (theme === 'work') {
                    document.documentElement.removeAttribute('data-theme');
                } else {
                    document.documentElement.setAttribute('data-theme', 'personal');
                }
            }
        }

        if (debugMenuButton && debugControls) {
            debugMenuButton.addEventListener('click', (event) => {
                event.stopPropagation();
                debugControls.classList.toggle('visible');
                debugMenuButton.classList.toggle('active');
            });

            document.addEventListener('click', (event) => {
                if (!debugControls.contains(event.target) &&
                        !debugMenuButton.contains(event.target)) {
                    debugControls.classList.remove('visible');
                    debugMenuButton.classList.remove('active');
                }
            });
        }

        if (debugTimeInput) {
            debugTimeInput.addEventListener('change', (event) => {
                if (!event.target.value) return;
                const [h, m] = event.target.value.split(':').map(Number);
                const now = new Date();
                const debugDate = new Date();
                debugDate.setHours(h, m, 0, 0);

                debugTimeOffset = debugDate.getTime() - now.getTime();
                update();
            });
        }

        if (resetTimeButton) {
            resetTimeButton.addEventListener('click', () => {
                debugTimeOffset = null;
                debugTimeInput.value = '';
                update();
            });
        }

        // Both pages keep the same one-second tick; it lives here so neither
        // page wires its own interval.
        let ticker = null;
        function start() {
            update();
            if (ticker === null) ticker = setInterval(update, 1000);
        }

        return { update: update, start: start };
    }

    // ---------- polling ----------
    // One loop for both pages. `run` returns false (or throws) for a failed
    // poll; the loop then backs off up to `maxInterval`. The metrics page sets
    // maxInterval equal to interval to keep its fixed minute. Both pages pause
    // while the tab is hidden and resume immediately when it returns.
    function createPoller(options) {
        const settings = options || {};
        const interval = settings.interval;
        const maxInterval = settings.maxInterval || interval;
        const initialDelay = settings.initialDelay || 0;
        let timer = null;
        let failures = 0;
        let started = false;

        function nextDelay() {
            if (!failures) return interval;
            return Math.min(maxInterval, interval * 2 ** Math.min(failures, 3));
        }

        function schedule(delay) {
            clearTimeout(timer);
            timer = setTimeout(async () => {
                if (document.hidden) {
                    timer = null;
                    return;
                }
                let ok = false;
                try {
                    ok = await settings.run();
                } catch (error) {
                    console.warn('poll failed', error);
                }
                failures = ok === false ? failures + 1 : 0;
                schedule(nextDelay());
            }, delay);
        }

        function start() {
            if (started) return;
            started = true;
            schedule(initialDelay);
        }

        function stop() {
            clearTimeout(timer);
            timer = null;
        }

        document.addEventListener('visibilitychange', () => {
            if (document.hidden) {
                stop();
            } else if (started && !timer) {
                schedule(0);
            }
        });

        return { start: start, stop: stop };
    }

    window.dashboardUI = {
        escapeHtml: escapeHtml,
        isNumber: isNumber,
        kindLabel: kindLabel,
        KIND_DEFS: KIND_DEFS,
        KIND_ORDER: KIND_ORDER,
        fmtBytes: fmtBytes,
        fmtBytesCompact: fmtBytesCompact,
        fmtDuration: fmtDuration,
        fmtUptime: fmtUptime,
        createClock: createClock,
        createPoller: createPoller,
    };
})();
