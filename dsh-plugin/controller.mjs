import { readFile, access } from 'node:fs/promises';
import { spawn } from 'node:child_process';
import { request } from 'node:http';
import { createHash } from 'node:crypto';
import { dirname, join, resolve } from 'node:path';
import { homedir } from 'node:os';

const pause = ms => new Promise(done => setTimeout(done, ms));
const readJSON = async path => JSON.parse(await readFile(path, 'utf8'));
export const allowedActions = new Set(['show', 'hide', 'toggle', 'panel', 'quit']);

export function localJSON(port, path, token = '', post = false, body) {
  return new Promise((done, fail) => {
    const payload = body === undefined ? '' : JSON.stringify(body);
    const req = request({hostname: '127.0.0.1', port, path, method: post ? 'POST' : 'GET',
      headers: {...(token ? {'X-HUD-Token': token} : {}),
        ...(post ? {'Content-Length': Buffer.byteLength(payload), 'Content-Type': 'application/json'} : {})}}, response => {
      let text = '';
      response.setEncoding('utf8');
      response.on('data', chunk => {
        text += chunk;
        if (text.length > 65536) req.destroy(new Error('监控响应过大'));
      });
      response.on('error', fail);
      response.on('end', () => {
        try {
          const data = JSON.parse(text);
          if (response.statusCode !== 200) {
            const error = new Error(data.error || `监控返回 ${response.statusCode}`);
            error.statusCode = response.statusCode;
            throw error;
          }
          done(data);
        } catch (error) { fail(error); }
      });
    });
    req.setTimeout(body === undefined ? 1500 : 12000, () => req.destroy(new Error('监控响应超时')));
    req.on('error', fail);
    req.end(payload);
  });
}

export class HudController {
  constructor(options) {
    this.enginePath = resolve(options.enginePath);
    this.dataDirectory = resolve(options.dataDirectory || join(homedir(), '.dsh', 'storages', 'bailian-hud'));
    this.ownerPid = process.pid;
    this.disposed = false;
    this.pending = null;
    this.launch = options.launch || (async background => {
      if (process.platform !== 'win32' || process.arch !== 'x64') throw new Error('当前插件包支持 Windows x64');
      await access(this.enginePath);
      await new Promise((done, fail) => {
        const child = spawn(this.enginePath, ['--plugin', '--parent-pid', String(this.ownerPid),
          ...(background ? ['--background'] : [])], {
          cwd: dirname(this.enginePath), windowsHide: true, detached: true, stdio: 'ignore',
          env: {...process.env, BAILIAN_HUD_HOME: this.dataDirectory}
        });
        child.once('error', fail);
        child.once('spawn', () => { child.unref(); done(); });
      });
    });
  }

  async instance() {
    const recorded = await readJSON(join(this.dataDirectory, '.hud_instance.json')).catch(() => null);
    if (!recorded || recorded.app !== 'BailianHUD' || !Number.isInteger(recorded.port) ||
        recorded.port < 1 || recorded.port > 65535 || !recorded.instance_id) return null;
    const ping = await localJSON(recorded.port, '/api/ping').catch(() => null);
    const dataId = createHash('sha256').update(this.dataDirectory.toLowerCase()).digest('hex').slice(0, 24);
    if (ping?.app !== 'BailianHUD' || ping.instance_id !== recorded.instance_id || ping.data_id !== dataId) return null;
    return recorded;
  }

  async call(instance, path, post = false, body) {
    const prefs = await readJSON(join(this.dataDirectory, '.hud_prefs.json'));
    if (!prefs.api_token) throw new Error('监控控制令牌尚未就绪');
    return localJSON(instance.port, path, prefs.api_token, post, body);
  }

  async credentials(body) {
    const instance = body === undefined ? await this.instance() : await this.ensure(true);
    if (!instance) return {configured: false, running: false};
    return {...await this.call(instance, '/api/credentials', body !== undefined, body), running: true};
  }

  async state() {
    const instance = await this.instance();
    if (!instance) return {running: false, shown: false};
    const state = await this.call(instance, '/api/hud/status');
    return {running: true, shown: Boolean(state.shown), user_hidden: Boolean(state.user_hidden),
      tray: Boolean(state.tray), port: instance.port, url: `http://127.0.0.1:${instance.port}/`};
  }

  async ensure(background = false) {
    if (this.disposed) throw new Error('百炼插件已停用');
    if (this.pending) return this.pending;
    this.pending = (async () => {
      let instance = await this.instance();
      if (instance) return instance;
      await this.launch(background);
      for (let i = 0; i < 60; i++) {
        await pause(200);
        instance = await this.instance();
        if (instance) return instance;
      }
      throw new Error('百炼插件引擎未能启动，请检查插件文件是否完整及数据目录中的 .hud.log');
    })();
    try { return await this.pending; } finally { this.pending = null; }
  }

  async dispose() {
    this.disposed = true;
    if (this.pending) await this.pending.catch(() => {});
    const instance = await this.instance();
    if (!instance) return;
    const state = await this.call(instance, '/api/hud/status').catch(() => null);
    if (state?.mode === 'plugin' && state.owner_pid === this.ownerPid) {
      await this.call(instance, '/api/hud/quit', true).catch(() => {});
    }
  }

  async action(action) {
    if (!allowedActions.has(action)) throw new Error('不支持的监控操作');
    let instance = await this.instance();
    if (!instance && ['show', 'toggle', 'panel'].includes(action)) instance = await this.ensure();
    if (!instance) return {running: false, shown: false};
    await this.call(instance, `/api/hud/${action}`, true);
    if (action === 'quit') {
      for (let i = 0; i < 35; i++) {
        await pause(100);
        if (!await this.instance()) return {running: false, shown: false};
      }
      throw new Error('服务正在退出，请稍后重试');
    }
    await pause(250);
    return this.state();
  }
}
