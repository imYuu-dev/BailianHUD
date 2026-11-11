import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createServer } from 'node:http';
import { createHash } from 'node:crypto';
import { HudController } from '../controller.mjs';
import { safeControlRequest } from '../index.js';

async function fixture(t) {
  const root = await mkdtemp(join(tmpdir(), 'bailian-plugin-'));
  const state = {shown: true, user_hidden: false, mode: 'plugin', owner_pid: process.pid};
  const calls = [];
  let id = 'synthetic-instance';
  const server = createServer((req, res) => {
    res.setHeader('Content-Type', 'application/json');
    if (req.url === '/api/ping') return res.end(JSON.stringify({app: 'BailianHUD', instance_id: id,
      data_id: createHash('sha256').update(root.toLowerCase()).digest('hex').slice(0, 24)}));
    if (req.headers['x-hud-token'] !== 'synthetic-token') { res.statusCode = 403; return res.end('{}'); }
    if (req.method === 'POST') {
      const action = req.url.split('/').at(-1);
      calls.push(action);
      state.shown = action === 'show';
      state.user_hidden = action === 'hide';
      if (action === 'quit') id = 'stopped';
    }
    res.end(JSON.stringify({ok: true, running: true, ...state}));
  });
  await new Promise(done => server.listen(0, '127.0.0.1', done));
  const port = server.address().port;
  await writeFile(join(root, '.hud_instance.json'), JSON.stringify({app: 'BailianHUD', port, instance_id: id}));
  await writeFile(join(root, '.hud_prefs.json'), JSON.stringify({api_token: 'synthetic-token'}));
  t.after(async () => { await new Promise(done => server.close(done)); await rm(root, {recursive: true}); });
  const controller = new HudController({enginePath: join(root, 'BailianHUD.exe'), dataDirectory: root});
  return {root, port, calls, controller, state};
}

test('DSH buttons control the original service without exposing tokens', async t => {
  const {controller, port, calls} = await fixture(t);
  const initial = await controller.state();
  assert.equal(initial.shown, true);
  assert.equal(initial.port, port);
  assert.equal('api_token' in initial, false);
  assert.equal((await controller.action('hide')).user_hidden, true);
  assert.equal((await controller.action('show')).shown, true);
  await controller.action('panel');
  assert.equal((await controller.action('quit')).running, false);
  assert.deepEqual(calls, ['hide', 'show', 'panel', 'quit']);
});

test('stale descriptors do not control a different process', async t => {
  const {controller, root} = await fixture(t);
  const descriptor = JSON.parse(await import('node:fs/promises').then(fs => fs.readFile(join(root, '.hud_instance.json'), 'utf8')));
  descriptor.instance_id = 'another-instance';
  await writeFile(join(root, '.hud_instance.json'), JSON.stringify(descriptor));
  assert.equal((await controller.state()).running, false);
  await assert.rejects(controller.action('exec'), /不支持/);
});

test('concurrent starts share one launch', async t => {
  const {controller} = await fixture(t);
  const instance = await controller.instance();
  let launched = 0;
  controller.instance = async () => launched ? instance : null;
  controller.launch = async () => { launched++; };
  const results = await Promise.all([controller.ensure(), controller.ensure(), controller.ensure()]);
  assert.equal(launched, 1);
  assert.equal(results.length, 3);
});

test('plugin controls reject cross-origin browser requests', () => {
  const headers = {'x-bailian-control': '1', host: 'localhost:3080', origin: 'http://localhost:3080'};
  assert.equal(safeControlRequest({headers}), true);
  assert.equal(safeControlRequest({headers: {...headers, origin: 'https://other.example'}}), false);
  assert.equal(safeControlRequest({headers: {...headers, 'sec-fetch-site': 'cross-site'}}), false);
  assert.equal(safeControlRequest({headers: {host: 'localhost:3080'}}), false);
});

test('disabling the plugin stops only its own worker', async t => {
  const own = await fixture(t);
  await own.controller.dispose();
  assert.deepEqual(own.calls, ['quit']);
  await assert.rejects(own.controller.ensure(), /已停用/);
  const other = await fixture(t);
  other.state.owner_pid = process.pid + 1;
  await other.controller.dispose();
  assert.deepEqual(other.calls, []);
});
