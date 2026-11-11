import { readFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { HudController, allowedActions } from './controller.mjs';

export const name = 'bailian-hud';
export const inject = ['webServer'];
const root = dirname(fileURLToPath(import.meta.url));

export function safeControlRequest(req) {
  if (req.headers['x-bailian-control'] !== '1') return false;
  if (req.headers['sec-fetch-site'] === 'cross-site') return false;
  if (req.headers.origin) {
    try { if (new URL(req.headers.origin).host !== req.headers.host) return false; }
    catch { return false; }
  }
  return true;
}

export function apply(ctx) {
  const ready = (async () => {
    const local = await readFile(join(root, 'local-config.json'), 'utf8').then(JSON.parse).catch(error => {
      if (error.code === 'ENOENT') return {};
      throw error;
    });
    return {controller: new HudController({enginePath: join(root, 'bin', 'windows-x64', 'BailianHUD.exe'), ...local}), autoStart: local.autoStart !== false};
  })();
  let startupError = '';
  let disposed = false;
  ready.then(async ({controller, autoStart}) => {
    if (autoStart && !disposed) await controller.ensure(true);
  }).catch(error => { startupError = error.message; });
  ctx.effect(() => async () => {
    disposed = true;
    const value = await ready.catch(() => null);
    await value?.controller.dispose();
  }, 'bailian-hud lifecycle');
  ctx.effect(() => ctx.webServer.register({kind: 'exact', path: '/bailian-hud/control',
    handler: async (req, res) => {
      const send = (status, value) => {
        res.writeHead(status, {'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store'});
        res.end(JSON.stringify(value));
      };
      try {
        if (!safeControlRequest(req)) return send(403, {error: '请从 DSH 界面操作'});
        if (!['GET', 'POST'].includes(req.method)) return send(405, {error: '不支持的请求'});
        const {controller} = await ready;
        if (req.method === 'GET') return send(200, {...await controller.state(), startupError});
        let body = '';
        for await (const chunk of req) {
          body += chunk;
          if (body.length > 2048) return send(413, {error: '请求过大'});
        }
        const action = JSON.parse(body).action;
        if (!allowedActions.has(action)) return send(400, {error: '不支持的监控操作'});
        const result = await controller.action(action);
        startupError = '';
        send(200, result);
      } catch (error) { send(500, {error: error.message}); }
    }
  }), 'bailian-hud control');
}
