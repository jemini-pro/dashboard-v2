const WORK_START_HOUR = 9;
const WORK_END_HOUR = 17;

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
const debugTimeInput = document.getElementById('debug-time');
const resetTimeBtn = document.getElementById('reset-time');
const debugMenuBtn = document.getElementById('debug-menu-btn');
const debugControls = document.getElementById('debug-controls');

const calendarSelect = document.getElementById('calendar-select');
const calendarViewSelect = document.getElementById('calendar-view-select');
const calendarIframe = document.getElementById('calendar-iframe');
const manageCalendarsBtn = document.getElementById('manage-calendars-btn');
const calendarManagerModal = document.getElementById('calendar-manager-modal');
const closeManagerBtn = document.getElementById('close-manager-btn');
const addCalendarForm = document.getElementById('add-calendar-form');
const calNameInput = document.getElementById('cal-name-input');
const calIdInput = document.getElementById('cal-id-input');
const savedCalendarsList = document.getElementById('saved-calendars-list');

const DEFAULT_CALENDARS = [
    { id: '201e89a61ac47a7109df0877b72f13305e2f4402fc3d9637664e4e7172bb91b1@group.calendar.google.com', name: 'Team Calendar' }
];

const STORAGE_KEY_CALENDARS_LIST = 'dashboard_calendars_list';
const STORAGE_KEY_CALENDAR_ID = 'dashboard_calendar_id';
const STORAGE_KEY_CALENDAR_MODE = 'dashboard_calendar_mode';

let debugTimeOffset = null;
let currentTheme = null;
let bookmarksRendered = false;

// Load stored user calendars or fallback to defaults
function getStoredCalendars() {
    const json = localStorage.getItem(STORAGE_KEY_CALENDAR_LIST_KEY_FALLBACK());
    if (json) {
        try {
            const parsed = JSON.parse(json);
            if (Array.isArray(parsed) && parsed.length > 0) return parsed;
        } catch (e) {
            console.error('Failed to parse saved calendars:', e);
        }
    }
    return DEFAULT_CALENDARS;
}

function STORAGE_KEY_CALENDAR_LIST_KEY_FALLBACK() {
    return STORAGE_KEY_CALENDARS_LIST;
}

function saveCalendars(calendars) {
    localStorage.setItem(STORAGE_KEY_CALENDARS_LIST, JSON.stringify(calendars));
}

function renderCalendarSelectOptions() {
    const calendars = getStoredCalendars();
    const currentSelectedId = localStorage.getItem(STORAGE_KEY_CALENDAR_ID) || calendars[0].id;

    calendarSelect.innerHTML = calendars.map(cal =>
        `<option value="${cal.id}" ${cal.id === currentSelectedId ? 'selected' : ''}>${cal.name}</option>`
    ).join('');

    // If current selected calendar ID is not in the list, default to first option
    if (!calendars.some(c => c.id === currentSelectedId) && calendars.length > 0) {
        localStorage.setItem(STORAGE_KEY_CALENDAR_ID, calendars[0].id);
        calendarSelect.value = calendars[0].id;
    }
}

function renderSavedCalendarsList() {
    const calendars = getStoredCalendars();
    const currentSelectedId = localStorage.getItem(STORAGE_KEY_CALENDAR_ID);

    savedCalendarsList.innerHTML = calendars.map((cal, index) => `
        <li class="calendar-item">
            <div class="calendar-item-info">
                <span class="calendar-item-name">${cal.name}</span>
                <span class="calendar-item-id">${cal.id}</span>
            </div>
            <div class="calendar-item-actions">
                ${calendars.length > 1 ? `
                    <button type="button" class="delete-cal-btn" data-index="${index}" title="Remove Calendar">&times;</button>
                ` : ''}
            </div>
        </li>
    `).join('');

    // Delete buttons event listeners
    savedCalendarsList.querySelectorAll('.delete-cal-btn').forEach(btn => {
        btn.addEventListener('click', (e) => {
            const idx = parseInt(e.currentTarget.getAttribute('data-index'), 10);
            deleteCalendar(idx);
        });
    });
}

function deleteCalendar(index) {
    let calendars = getStoredCalendars();
    if (calendars.length <= 1) return; // Prevent deleting the last calendar

    const deleted = calendars.splice(index, 1)[0];
    saveCalendars(calendars);

    // If deleted calendar was currently active, reset to first available
    const activeId = localStorage.getItem(STORAGE_KEY_CALENDAR_ID);
    if (activeId === deleted.id && calendars.length > 0) {
        localStorage.setItem(STORAGE_KEY_CALENDAR_ID, calendars[0].id);
    }

    renderCalendarSelectOptions();
    renderSavedCalendarsList();
    updateCalendarIframe();
}

function initCalendarControls() {
    const savedMode = localStorage.getItem(STORAGE_KEY_CALENDAR_MODE) || 'AGENDA';
    calendarViewSelect.value = savedMode;

    renderCalendarSelectOptions();
    updateCalendarIframe();
}

function updateCalendarIframe() {
    const calId = calendarSelect.value || getStoredCalendars()[0].id;
    const mode = calendarViewSelect.value || 'AGENDA';
    const ctz = encodeURIComponent(Intl.DateTimeFormat().resolvedOptions().timeZone || 'Europe/London');
    const encodedCalId = encodeURIComponent(calId);

    const newSrc = `https://calendar.google.com/calendar/embed?src=${encodedCalId}&ctz=${ctz}&mode=${mode}`;

    if (calendarIframe.src !== newSrc) {
        calendarIframe.src = newSrc;
    }

    localStorage.setItem(STORAGE_KEY_CALENDAR_ID, calId);
    localStorage.setItem(STORAGE_KEY_CALENDAR_MODE, mode);
}

calendarSelect.addEventListener('change', () => {
    updateCalendarIframe();
});

calendarViewSelect.addEventListener('change', () => {
    updateCalendarIframe();
});

manageCalendarsBtn.addEventListener('click', () => {
    renderSavedCalendarsList();
    calendarManagerModal.classList.remove('hidden');
});

closeManagerBtn.addEventListener('click', () => {
    calendarManagerModal.classList.add('hidden');
});

calendarManagerModal.addEventListener('click', (e) => {
    if (e.target === calendarManagerModal) {
        calendarManagerModal.classList.add('hidden');
    }
});

addCalendarForm.addEventListener('submit', (e) => {
    e.preventDefault();
    const name = calNameInput.value.trim();
    const id = calIdInput.value.trim();

    if (!name || !id) return;

    const calendars = getStoredCalendars();
    // Avoid duplicate IDs
    if (!calendars.some(c => c.id === id)) {
        calendars.push({ id, name });
        saveCalendars(calendars);
    }

    localStorage.setItem(STORAGE_KEY_CALENDAR_ID, id);

    calNameInput.value = '';
    calIdInput.value = '';

    renderCalendarSelectOptions();
    renderSavedCalendarsList();
    updateCalendarIframe();

    calendarManagerModal.classList.add('hidden');
});

initCalendarControls();

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
        renderBookmarks(bookmarks.work);
        bookmarksRendered = true;
    }
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
                ${cat.items.map(item => `
                    <a href="${item.url}" class="bookmark-card" target="_blank">
                        <div class="bookmark-icon-text">${item.icon}</div>
                        <div class="bookmark-details">
                            <div class="bookmark-title">${item.title}</div>
                            <div class="bookmark-desc">${item.desc || ''}</div>
                        </div>
                    </a>
                `).join('')}
            </div>
        </div>
    `).join('');
}

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
