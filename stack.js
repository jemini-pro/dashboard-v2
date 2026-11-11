/*
 * Containment model shared by the dashboard rail and the metrics rail.
 *
 * The dashboard runs inside Docker, inside the docker01 LXC, inside the pve
 * Proxmox host. The API describes that stack in `topology`; this file turns a
 * flat device list plus that description into the four groups both pages draw:
 * host, Docker host, sibling guests, and everything off the PVE tree.
 */
(function () {
    'use strict';

    function escapeHtml(value) {
        return String(value === null || value === undefined ? '' : value)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    function keyOf(device) {
        return device.id || device.name;
    }

    function group(devices, topology) {
        const list = Array.isArray(devices) ? devices : [];
        const described = topology && typeof topology === 'object';
        const byId = new Map(list.map((device) => [keyOf(device), device]));

        // Trust the server's ids when a topology is present. A null id means
        // the stack really has no such layer; only fall back to kind when an
        // older payload has no topology at all.
        const host = described
            ? byId.get(topology.host_id) || null
            : list.find((device) => device.kind === 'host') || null;
        const dockerHost = described
            ? byId.get(topology.docker_host_id) || null
            : list.find((device) => device.kind === 'lxc') || null;

        const guests = list.filter((device) =>
            device !== host && device !== dockerHost &&
            (device.kind === 'lxc' || device.kind === 'lxc-guest'));
        const others = list.filter((device) =>
            device !== host && device !== dockerHost && guests.indexOf(device) === -1);
        const containers = described && Array.isArray(topology.containers)
            ? topology.containers
            : [];

        return { host, dockerHost, guests, others, containers };
    }

    function branch(label, note, body, tone) {
        if (!body) return '';
        return '<section class="network-branch' + (tone ? ' ' + tone : '') + '">' +
            '<h3 class="network-branch-head"><span>' + escapeHtml(label) + '</span>' +
            (note ? '<span>' + escapeHtml(note) + '</span>' : '') +
            '</h3>' + body + '</section>';
    }

    // State is carried by the text as well as the dot; a colour alone would
    // leave the container's health unreadable to assistive tech.
    function containerRow(container) {
        const item = container && typeof container === 'object' ? container : {};
        const name = String(item.name || 'container');
        const role = String(item.role || '');
        const state = String(item.state || 'running');
        const detail = state + (role ? ' · ' + role : '');
        return '<div class="docker-container" title="' +
            escapeHtml(name + ' · ' + detail) + '">' +
            '<span class="device-dot ' +
            (state === 'running' ? 'online' : 'offline') +
            '" aria-hidden="true"></span>' +
            '<span class="docker-container-name">' + escapeHtml(name) + '</span>' +
            '<span class="docker-container-role">' + escapeHtml(detail) + '</span>' +
            '</div>';
    }

    function dockerEngine(containers) {
        if (!Array.isArray(containers) || !containers.length) return '';
        const running = containers.filter((container) =>
            container && container.state === 'running').length;
        return '<div class="docker-engine">' +
            '<h4 class="docker-engine-head">' +
            '<svg class="docker-glyph" viewBox="0 0 16 16" aria-hidden="true" focusable="false">' +
            '<rect x="1.5" y="6" width="3" height="3" rx=".7"></rect>' +
            '<rect x="5.5" y="6" width="3" height="3" rx=".7"></rect>' +
            '<rect x="9.5" y="6" width="3" height="3" rx=".7"></rect>' +
            '<rect x="5.5" y="2.5" width="3" height="3" rx=".7"></rect>' +
            '<path d="M1 11.5c2.4 2.2 9.4 2.6 12.7-1.1M12.4 10.4h2.1"></path>' +
            '</svg><span>Docker engine</span>' +
            '<span class="docker-engine-count">' + running + ' running</span>' +
            '</h4>' +
            '<div class="docker-containers">' +
            containers.map(containerRow).join('') +
            '</div></div>';
    }

    window.dashboardStack = {
        group: group,
        branch: branch,
        containerRow: containerRow,
        dockerEngine: dockerEngine,
    };
})();
