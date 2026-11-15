/*
 * One same-origin data seam for both pages.
 *
 * `getJson` is the primitive: no-store, HTTP errors throw. `read` is the shape
 * both pollers use: a failed request never throws, it is logged once under the
 * surface's name, and the caller keeps painting its last good reading while it
 * marks its own stale state.
 *
 * Loaded before stack.js and the page script. Lives under window.dashData.
 */
(function () {
    'use strict';

    async function getJson(path) {
        const response = await fetch(path, { cache: 'no-store' });
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return response.json();
    }

    async function read(path, label) {
        try {
            return { ok: true, data: await getJson(path) };
        } catch (error) {
            console.warn(
                (label || path) + ' unreachable, keeping the last good reading',
                error
            );
            return { ok: false, error: error };
        }
    }

    window.dashData = {
        getJson: getJson,
        read: read,
    };
})();
