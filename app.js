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
