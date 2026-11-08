// Also run by pytest. Exercise the browser entry points with controllable
// network, decoding and visibility events; no camera or browser cache needed.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const staticDir = path.resolve(__dirname, '../../indi_allsky/flask/static/js');

function browser(view, {sidebar = view !== 'companion', livePath = 'latest'} = {}) {
    let now = 0, nextId = 0;
    const timers = new Map(), elements = new Map(), requests = [], images = [], displays = [], draws = [];
    const handlers = new Map();
    const listen = (type, callback) => {
        if (!handlers.has(type)) handlers.set(type, []);
        handlers.get(type).push(callback);
    };

    class Element {
        constructor(id) {
            this.id = id;
            this.textContent = '';
            this.hidden = false;
            this.options = [];
            this.value = '';
            this.style = {setProperty() {}};
            this.classList = {toggle() {}};
            this.dataset = {};
        }
        addEventListener(type, callback) { this[type] = callback; }
        getContext() { return {drawImage: image => draws.push(image.src)}; }
        querySelector(key) {
            if (key === 'canvas' && this.id === 'live-view') return view === 'canvas' ? el('canvas') : null;
            return el(this.id + key);
        }
        replaceWith(other) {
            elements.set(this.id, other);
            displays.push(other.src);
        }
        replaceChildren(...children) { this.options = children; }
        append(...children) { this.options.push(...children); }
        removeAttribute(key) { delete this[key]; }
    }
    function el(id) {
        if (!elements.has(id)) elements.set(id, new Element(id));
        return elements.get(id);
    }
    el('companion').dataset = {cameraId: '3', refreshMs: '30000', staleMs: '90000', endpoint: '/indi-allsky/js/companion?camera_id=3'};
    el('live-view').dataset = {interval: '31000', endpoint: '/indi-allsky/js/' + livePath + '?camera_id=3&night=auto'};
    if (sidebar) el('sidebar-live-status').dataset = {endpoint: '/indi-allsky/js/latest?camera_id=3&night=auto'};
    function byId(id) {
        if (id === 'live-view' && view === 'sidebar') return null;
        if (['message', 'live-age', 'live-time', 'live-server', 'live-status'].includes(id)) return null;
        if (!sidebar && ['sidebar-live-status', 'sidebar-live-detail', 'live-retry', 'live-login', 'live-services', 'live-settings'].includes(id)) return null;
        return el(id);
    }
    const document = {baseURI: 'https://camera.test/indi-allsky/', hidden: false, addEventListener: listen,
        getElementById: byId,
        createElement: () => new Element(), createTextNode: text => text, querySelector: el};
    function schedule(callback, delay, repeat = false) {
        const id = ++nextId;
        timers.set(id, {callback, delay, at: now + delay, repeat});
        return id;
    }
    const setTimeout = (callback, delay) => schedule(callback, delay);
    const setInterval = (callback, delay) => schedule(callback, delay, true);
    const clearTimeout = id => timers.delete(id);
    const window = {setInterval, innerWidth: 640, innerHeight: 480, addEventListener: listen};
    const context = vm.createContext({URL, Date, Intl, AbortController, document, window, setTimeout, clearTimeout,
        performance: {now: () => now}, localStorage: {getItem: () => null, setItem() {}},
        Option: class {constructor(label, value) {this.label = label; this.value = value;}},
        Image: class extends Element {
            constructor() {super(); this.naturalWidth = 640; this.naturalHeight = 480; images.push(this);}
        },
        fetch: (url, options) => new Promise((resolve, reject) => {
            requests.push({url: new URL(url), options, fail: () => reject(new Error('network')),
                reply: (body, type = 'application/json', status = 200, redirected = false) => resolve({
                    json: async () => body, headers: {get: () => type}, ok: status >= 200 && status < 300, status, redirected,
                    url: redirected ? 'https://camera.test/indi-allsky/login' : url.toString(),
                })});
            // Deliberately do not settle fetch on signal.abort(). This lets a test
            // deliver an already-dispatched obsolete response after a wakeup.
        }),
    });
    const files = ['live_frames.js', view === 'companion' ? 'companion.js' : 'latest_view.js'];
    for (const file of files) vm.runInContext(fs.readFileSync(path.join(staticDir, file), 'utf8'), context, {filename: file});
    const flush = () => new Promise(resolve => setImmediate(resolve));
    async function advance(ms) {
        const finish = now + ms;
        while (true) {
            const next = [...timers].sort((a, b) => a[1].at - b[1].at)[0];
            if (!next || next[1].at > finish) break;
            const [id, timer] = next;
            now = timer.at;
            if (timer.repeat) timer.at += timer.delay;
            else timers.delete(id);
            timer.callback(); await flush();
        }
        now = finish;
    }
    const emit = (type, data = {}) => { for (const cb of handlers.get(type) || []) cb(data); };
    return {el, advance, requests, images, displays, draws, flush, emit, document, timers};
}

const latest = filename => ({latest_image: {url: filename, message: ''}});
const frame = (id = 0) => ({url: 'images/' + id + '.jpg', captured_at: '2026-11-07T21:00:0' + id + '+01:00', time_source: 'capture'});
const status = image => ({image, now: '2026-11-07T21:00:10+01:00', time_zone: 'Europe/Berlin', offset_minutes: 60});
const full = image => ({...status(image), astronomy: {moon_phase: 'New Moon', moon_illumination: 0}, metrics: [], defaults: [], series: []});

for (const view of ['image', 'canvas']) {
    test(view + ': login HTML cannot kill the loop, recovered data draws immediately', async () => {
        const b = browser(view);
        b.requests[0].reply('<html>login</html>', 'text/html', 200, true);
        await b.flush();
        assert.match(b.el('sidebar-live-status').textContent, /sign-in required/);
        assert.equal(b.el('live-login').hidden, false);
        await b.advance(5000);
        b.requests[1].reply(latest('images/ok.jpg')); await b.flush();
        assert.equal(b.images.length, 1);
        b.images[0].onload();
        assert.deepEqual(view === 'canvas' ? b.draws : b.displays, ['images/ok.jpg']);
        assert.match(b.el('sidebar-live-status').textContent, /camera server reachable/);
        assert.equal(b.el('live-login').hidden, true);
        assert.equal(b.requests[1].options.cache, 'no-store');
    });
}

const detailed = (url, capturedAt, reason = null, now = '2026-11-08T03:05:00+01:00') => ({
    latest_image: {url, message: '', captured_at: capturedAt, frame_key: capturedAt, time_source: 'capture'},
    live_status: {now, stale_after_s: 90, reason, can_manage: false},
});

for (const view of ['image', 'canvas']) {
    test(view + ': age and camera time describe only the displayed frame, including while offline', async () => {
        const b = browser(view);
        const indicator = b.el('sidebar-live-status');
        assert.equal(b.requests.length, 1);
        assert.equal(b.requests[0].url.searchParams.get('live_status'), '1');
        const first = detailed('first', '2026-11-08T03:02:00+01:00');
        b.requests[0].reply(first); await b.flush();
        assert.match(indicator.textContent, /^Waiting for image/);
        b.images[0].onload();
        assert.match(indicator.textContent, /^Last image 3 min ago/);
        assert.match(indicator.title, /camera server reachable/);
        assert.match(indicator.title, /2026-11-08 03:02:00 UTC\+01:00/);
        await b.advance(5000);
        const next = detailed('next', first.live_status.now, null, '2026-11-08T03:05:05+01:00');
        b.requests[1].reply(next); await b.flush();
        assert.match(indicator.title, /03:02:00 UTC\+01:00/);
        assert.doesNotMatch(indicator.title, /03:05:00 UTC\+01:00/);
        b.images[1].onload();
        assert.match(indicator.title, /03:05:00 UTC\+01:00/);
        assert.equal(indicator.textContent, 'Last image 5 s ago · camera server reachable');
        await b.advance(5000); b.requests[2].fail(); await b.flush();
        await b.advance(1000);
        assert.equal(indicator.textContent, 'Last image 11 s ago · server offline');
        assert.match(indicator.title, /camera server unreachable/);
        assert.equal(b.el('live-retry').hidden, false);
        b.el('live-retry').click();
        b.requests[3].reply(detailed('next', next.latest_image.captured_at, 'processing', '2026-11-08T03:05:11+01:00'));
        await b.flush();
        assert.equal(b.images.length, 2);
        assert.equal(indicator.textContent, 'Last image 11 s ago · camera server reachable');
        assert.equal(b.el('live-retry').hidden, true);
    });

    test(view + ': frames still display without a status element in the page', async () => {
        const b = browser(view, {sidebar:false});
        b.requests[0].reply(detailed('shown.jpg', '2026-11-08T03:04:59+01:00'));
        await b.flush();
        b.images[0].onload();
        assert.deepEqual(view === 'canvas' ? b.draws : b.displays, ['shown.jpg']);
        await b.advance(5000);
        assert.equal(b.requests.length, 2);
    });
}

test('specific remedies require a current authorization, and deliberate pauses are never retried manually', async () => {
    const b = browser('sidebar');
    const pause = detailed(null, null, 'paused');
    const update = async data => {
        b.emit('online'); b.requests.at(-1).reply(data); await b.flush();
    };
    b.requests[0].reply(pause); await b.flush();
    assert.match(b.el('sidebar-live-status').title, /Capture is paused/);
    assert.equal(b.el('sidebar-live-detail').textContent, 'Capture is paused.');
    assert.equal(b.el('live-settings').hidden, true);
    assert.equal(b.el('live-retry').hidden, true);
    assert.match(b.el('sidebar-live-status').textContent, /capture paused/);
    assert.doesNotMatch(b.el('sidebar-live-status').textContent, /settings/i);
    pause.live_status.can_sign_in_to_manage = true;
    await update(pause);
    assert.equal(b.el('live-login').hidden, false);
    assert.equal(b.el('live-settings').hidden, true);
    pause.live_status.can_sign_in_to_manage = false;
    pause.live_status.can_manage = true;
    await update(pause);
    assert.equal(b.el('live-settings').hidden, false);
    pause.live_status.reason = 'archive_readonly';
    await update(pause);
    assert.equal(b.el('live-settings').hidden, true);
    assert.equal(b.el('live-services').hidden, false);
    assert.match(b.el('sidebar-live-status').title, /write access/);
    assert.match(b.el('sidebar-live-detail').textContent, /write access/);
    b.emit('online'); b.requests.at(-1).reply({}, 'application/json', 403);
    await b.flush();
    assert.equal(b.el('live-services').hidden, true);
    assert.equal(b.el('live-login').hidden, true);
    assert.equal(b.el('live-retry').hidden, true);
    b.emit('online'); b.requests.at(-1).reply({}, 'application/json', 401);
    await b.flush();
    assert.equal(b.el('live-login').hidden, false);
});

test('ambiguous camera timestamps do not inherit the timestamp or age of the previous frame', async () => {
    const b = browser('image');
    b.requests[0].reply(detailed('first', '2026-11-08T03:04:59+01:00')); await b.flush();
    b.images[0].onload();
    const entry = detailed('ambiguous', null);
    entry.latest_image.frame_key = 'image:2';
    await b.advance(5000); b.requests[1].reply(entry); await b.flush(); b.images[1].onload();
    assert.doesNotMatch(b.el('sidebar-live-status').title, /03:04:59/);
    assert.match(b.el('sidebar-live-status').textContent, /^Image time unknown/);
});

test('the sidebar tooltip identifies file times and distinguishes the repeated local hour', async () => {
    const b = browser('image');
    const indicator = b.el('sidebar-live-status');
    for (const [index, offset] of ['+02:00', '+01:00'].entries()) {
        if (index) await b.advance(5000);
        const entry = detailed('file-' + index, '2026-10-25T02:15:00' + offset,
            null, '2026-10-25T02:15:02' + offset);
        entry.latest_image.time_source = 'file';
        b.requests[index].reply(entry);
        await b.flush();
        b.images[index].onload();
        assert.equal(indicator.textContent, 'Last image 2 s ago · camera server reachable');
        assert.ok(indicator.title.includes('File time 2026-10-25 02:15:00 UTC' + offset));
        assert.equal(b.el('sidebar-live-detail').textContent, '');
    }
});

test('a clock correction never turns a genuinely future displayed timestamp into a freshly captured image', async () => {
    const b = browser('image', {sidebar: true});
    const captured = '2026-11-08T03:05:01+01:00';
    b.requests[0].reply(detailed('shown.jpg', captured));
    await b.flush(); b.images[0].onload();
    const indicator = b.el('sidebar-live-status');
    assert.match(indicator.textContent, /^Last image 0 s ago/);
    await b.advance(5000);
    b.requests[1].reply(detailed(null, null, 'no_recent_image', '2026-11-08T02:55:05+01:00'));
    await b.flush();
    assert.match(indicator.title, /03:05:01 UTC\+01:00/);
    assert.match(indicator.title, /Check the camera time and time zone/);
    assert.equal(b.el('live-retry').hidden, false);
    assert.equal(b.el('sidebar-live-status').textContent, 'Image time ahead of camera · check clock');
    b.el('live-retry').click();
    b.requests[2].reply(detailed('new-frame.jpg', '2026-11-08T02:55:05+01:00', null, '2026-11-08T02:55:08+01:00'));
    await b.flush(); b.images[1].onload();
    assert.equal(indicator.textContent, 'Last image 3 s ago · camera server reachable');
    assert.equal(b.el('live-retry').hidden, true);
});

test('on other pages the sidebar polls JSON at a low rate without downloading images', async () => {
    const b = browser('sidebar', {sidebar: true});
    b.requests[0].reply(detailed('not-fetched', '2026-11-08T03:04:57+01:00'));
    await b.flush();
    assert.equal(b.el('sidebar-live-status').textContent, 'Last image 3 s ago · camera server reachable');
    await b.advance(12000);
    assert.equal(b.requests.length, 1);
    assert.equal(b.el('sidebar-live-status').textContent, 'Last image 15 s ago · camera server reachable');
    await b.advance(3000);
    assert.equal(b.requests.length, 2);
    assert.equal(b.images.length, 0);
    b.requests[1].fail();
    await b.flush();
    assert.equal(b.el('live-retry').hidden, false);
    b.el('live-retry').click();
    assert.equal(b.requests.length, 3);
    b.requests[2].reply(detailed('new.jpg', '2026-11-08T03:04:59+01:00'));
    await b.flush();
    assert.equal(b.el('live-retry').hidden, true);
    assert.deepEqual(b.images, []);
});

test('panorama polls and sidebar capture polls remain independent', async () => {
    const b = browser('canvas', {sidebar: true, livePath:'latest_panorama'});
    assert.equal(b.requests.length, 2);
    b.requests[0].reply(detailed('panorama.jpg', '2026-11-08T03:01:00+01:00'));
    b.requests[1].reply(detailed('normal.jpg', '2026-11-08T03:04:51+01:00', 'capture_recovering'));
    await b.flush();
    b.images[0].onload();
    assert.equal(b.el('sidebar-live-status').textContent, 'Last image 9 s ago · recovering capture');
    assert.doesNotMatch(b.el('sidebar-live-status').title, /03:01:00/);
    assert.deepEqual(b.draws, ['panorama.jpg']);
    assert.equal(b.images.length, 1);
});

test('a slow completed frame displays before the newest queued frame; a failed image retains it', async () => {
    const b = browser('image');
    b.requests[0].reply(latest('first')); await b.flush();
    await b.advance(5000); b.requests[1].reply(latest('obsolete')); await b.flush();
    await b.advance(5000); b.requests[2].reply(latest('newest')); await b.flush();
    assert.equal(b.images.length, 1);
    b.images[0].onload();
    assert.deepEqual(b.displays, ['first']);
    assert.equal(b.images[1].src, 'newest');
    b.images[1].onerror();
    assert.deepEqual(b.displays, ['first']);
    assert.match(b.el('sidebar-live-status').title, /could not be loaded/i);
    await b.advance(5000); b.requests[3].reply(latest('newest')); await b.flush();
    b.images[2].onload(); assert.deepEqual(b.displays, ['first', 'newest']);
});

test('returning from sleep cancels old HTTP and JPEG callbacks; a bfcache return refreshes', async () => {
    const b = browser('canvas');
    b.requests[0].reply(latest('old')); await b.flush();
    const lateImage = b.images[0].onload;
    await b.advance(5000);
    b.document.hidden = true; b.emit('visibilitychange');
    assert.equal(b.requests[1].options.signal.aborted, true);
    assert.equal(b.images[0].onload, null);
    await b.advance(15000); assert.equal(b.requests.length, 2);
    b.document.hidden = false; b.emit('visibilitychange');
    b.requests[2].reply(latest('new')); await b.flush();
    b.requests[1].reply(latest('stale')); lateImage(); await b.flush();
    assert.equal(b.images.length, 2);
    b.images[1].onload(); assert.deepEqual(b.draws, ['new']);
    b.emit('pageshow', {persisted: true});
    assert.equal(b.requests.length, 4);
});

test('a hanging JPEG times out and the same key becomes retriable', async () => {
    const b = browser('image');
    b.requests[0].reply(latest('unchanged')); await b.flush();
    for (let i = 1; i < 6; i++) {
        await b.advance(5000); b.requests[i].reply(latest('unchanged')); await b.flush();
        assert.equal(b.images.length, 1);
    }
    await b.advance(5000);
    assert.match(b.el('sidebar-live-status').title, /timed out/);
    b.requests[6].reply(latest('unchanged')); await b.flush();
    assert.equal(b.images.length, 2);
    b.images[1].onload(); assert.deepEqual(b.displays, ['unchanged']);
});

test('companion image checks proceed through repeated or slow detail failures', async () => {
    const b = browser('companion');
    const only = () => b.requests.filter(r => r.url.searchParams.has('image_only'));
    const details = () => b.requests.filter(r => !r.url.searchParams.has('image_only'));
    assert.equal(only().length, 1); assert.equal(details().length, 1);
    only()[0].reply(status(frame())); await b.flush();
    b.images[0].onload();
    for (let i = 1; i < 4; i++) {
        await b.advance(2000); only()[i].reply(status(frame(i))); await b.flush();
        b.images[i].onload();
    }
    assert.equal(details().length, 1);
    details()[0].reply(null, 'application/json', 503); await b.flush();
    assert.match(b.el('connection-message').textContent, /history/);
    await b.advance(2000); only()[4].reply(status(frame(4))); await b.flush();
    b.images[4].onload();
    assert.equal(b.el('image-time').dateTime, frame(4).captured_at);
    assert.equal(b.displays.length, 5);
});

test('companion differentiates missing, transferring, paused, offline and authentication states', async () => {
    const b = browser('companion');
    let read = b.requests[0];
    const message = () => b.el('connection-message').textContent;
    read.reply(status(null)); b.requests[1].reply(full(null)); await b.flush();
    assert.match(message(), /Pi online · no new image/);
    await b.advance(2000); read = b.requests.at(-1);
    read.reply(status(frame())); await b.flush(); await b.advance(1000);
    assert.match(message(), /New capture available · loading image/);
    b.images[0].onload(); assert.equal(message(), '');
    await b.advance(1000); b.requests.at(-1).fail(); await b.flush();
    assert.match(message(), /Lost connection to the Pi/);
    b.emit('online');
    b.requests.at(-2).reply({...status(frame()), capture_paused: true}); b.requests.at(-1).reply(full(null)); await b.flush();
    assert.match(message(), /capture paused/);
    assert.equal(b.displays.length, 1);
    await b.advance(2000); b.requests.at(-1).reply('login', 'text/html', 200, true); await b.flush();
    assert.match(message(), /Sign in again/);
    assert.equal(b.displays.length, 1);
});

test('companion clears a stale chart reading and reports an unreadable local UPS', async () => {
    const b = browser('companion');
    const payload = full(null);
    const metric = {id: 'sensor_user_35', label: 'Lux', unit: 'lx'};
    Object.assign(payload, {metrics: [metric], defaults: [metric.id],
        series: [{...metric, points: [['2026-11-07T20:59:00+01:00', 2], ['2026-11-07T21:00:00+01:00', null]]}]});
    b.requests[0].reply({...status(null), ups_unreadable: true});
    b.requests[1].reply(payload);
    await b.flush();
    assert.equal(b.el('chart-slot-1.chart-value').textContent, '–');
    assert.match(b.el('connection-message').textContent, /Cannot read UPS status/);
    b.emit('online');
    payload.series[0].points.push(['2026-11-07T21:01:00+01:00', 0]);
    b.requests.at(-2).reply({...status(null), ups_unreadable: false});
    b.requests.at(-1).reply(payload);
    await b.flush();
    assert.equal(b.el('chart-slot-1.chart-value').textContent, '0 lx');
    assert.doesNotMatch(b.el('connection-message').textContent, /Cannot read UPS status/);
});

test('companion formats English readings and keeps the date and offset of each camera event', async () => {
    const b = browser('companion');
    const image = {...frame(), captured_at: '2026-10-25T00:00:00+02:00'};
    const payload = {...full(image), now: '2026-10-25T00:00:10+02:00', offset_minutes: 120};
    Object.assign(payload.astronomy, {moon_rise: '2026-10-25T00:15:00+02:00', moon_illumination: 0.5});
    const metric = {id: 'sensor_user_35', label: 'Sky brightness', unit: 'lx'};
    payload.metrics = [metric];
    payload.defaults = [metric.id];
    payload.series = [{...metric, points: [['2026-10-25T02:30:00+02:00', 1.25]]}];
    b.requests[0].reply(payload);
    b.requests[1].reply(payload);
    await b.flush();
    b.images[0].onload();
    assert.equal(b.el('clock').textContent, '00:00:10');
    assert.equal(b.el('image-time').options[1].textContent, '2026-10-25');
    assert.equal(b.el('image-age').textContent, '10 s ago');
    const rise = b.el('moon_rise').options[0];
    assert.equal(rise.dateTime, payload.astronomy.moon_rise);
    assert.deepEqual(rise.options.map(child => child.textContent), ['00:15', 'Oct 25']);
    assert.equal(b.el('moon-phase').textContent, 'New Moon · 0.5%');
    const reading = b.el('chart-slot-1.chart-value');
    assert.equal(reading.textContent, '1.25 lx');
    assert.equal(reading.title, 'Last reading at 02:30 UTC+02:00');
    // The same local time occurs again after the camera switches to standard time.
    payload.series[0].points.push(['2026-10-25T02:30:00+01:00', 0.025]);
    b.emit('online');
    b.requests.at(-2).reply(payload);
    b.requests.at(-1).reply(payload);
    await b.flush();
    assert.equal(reading.textContent, '0.025 lx');
    assert.equal(reading.title, 'Last reading at 02:30 UTC+01:00');
});
