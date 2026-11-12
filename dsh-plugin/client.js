window.__ModuleLoader__.load({
  id: 'dsh-bailian-hud',
  factory: require => {
    const React = require('react');
    const h = React.createElement;

    async function api(action) {
      const response = await fetch('/bailian-hud/control', {
        method: action ? 'POST' : 'GET', cache: 'no-store',
        headers: {'X-Bailian-Control': '1', ...(action ? {'Content-Type': 'application/json'} : {})},
        ...(action ? {body: JSON.stringify({action})} : {}), signal: AbortSignal.timeout(18000)
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || `操作失败 (${response.status})`);
      return data;
    }

    async function credentials(body) {
      const response = await fetch('/bailian-hud/credentials', {
        method: body ? 'POST' : 'GET', cache: 'no-store',
        headers: {'X-Bailian-Control': '1', ...(body ? {'Content-Type': 'application/json'} : {})},
        ...(body ? {body: JSON.stringify(body)} : {}), signal: AbortSignal.timeout(25000)
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || '保存失败，请重试');
      return data;
    }

    function Credentials() {
      const [id, setId] = React.useState('');
      const [secret, setSecret] = React.useState('');
      const [state, setState] = React.useState(null);
      const [busy, setBusy] = React.useState(false);
      const [error, setError] = React.useState('');
      React.useEffect(() => {
        let live = true;
        let pending = false;
        const refresh = async () => {
          if (pending) return;
          pending = true;
          try { const value = await credentials(); if (live) setState(value); }
          catch (e) { if (live) setError(e.message); }
          finally { pending = false; }
        };
        void refresh();
        const timer = setInterval(refresh, 3000);
        return () => { live = false; clearInterval(timer); };
      }, []);
      const save = async event => {
        event.preventDefault();
        if (busy) return;
        setBusy(true); setError('');
        try {
          setState(await credentials({aliyunAkId: id, aliyunAkSecret: secret}));
          setId(''); setSecret('');
        } catch (e) { setError(e.message); }
        finally { setBusy(false); }
      };
      const field = {font: 'inherit', color: 'inherit', width: '100%', boxSizing: 'border-box',
        padding: '10px 12px', border: '1px solid #8885', borderRadius: 7, background: 'transparent'};
      const status = !state ? '正在读取配置' : !state.running ? '服务已停止'
        : !state.configured ? '未配置' : state.environment ? '当前使用环境变量中的 AK'
        : state.sync?.ok ? '已同步' : state.sync?.error || '已保存，正在同步';
      return h('section', {style: {maxWidth: 460, padding: '24px 0', display: 'grid', gap: 20}},
        h('h2', {style: {fontSize: 18, margin: 0}}, '阿里云 AccessKey'),
        h('form', {onSubmit: save, autoComplete: 'off', style: {display: 'grid', gap: 16}},
          h('label', {style: {display: 'grid', gap: 8}}, 'AccessKey ID',
            h('input', {value: id, onChange: e => setId(e.target.value), disabled: busy, required: true,
              spellCheck: false, autoComplete: 'off', maxLength: 512, style: field})),
          h('label', {style: {display: 'grid', gap: 8}}, 'AccessKey Secret',
            h('input', {type: 'password', value: secret, onChange: e => setSecret(e.target.value), disabled: busy,
              required: true, autoComplete: 'off', maxLength: 512, style: field})),
          h('button', {type: 'submit', disabled: busy || !id.trim() || !secret.trim(),
            style: {font: 'inherit', color: 'inherit', padding: '10px 16px', border: '1px solid #8885',
              borderRadius: 7, background: 'var(--dsw-alias-background-secondary, #8881)', justifySelf: 'start',
              cursor: 'pointer'}}, busy ? '正在保存…' : '保存并同步')),
        h('div', {role: error || state?.sync?.error ? 'alert' : 'status', 'aria-live': 'polite',
          style: {fontSize: 13}}, error || status),
        h('a', {href: 'https://github.com/imYuu-dev/BailianHUD#获取自己的-ak', target: '_blank',
          rel: 'noreferrer', style: {fontSize: 13, color: 'inherit'}}, '获取 AccessKey'));
    }

    function Controls({menu = false}) {
      const [state, setState] = React.useState(null);
      const [busy, setBusy] = React.useState(false);
      const [error, setError] = React.useState('');
      const [open, setOpen] = React.useState(false);
      const root = React.useRef(null);
      React.useEffect(() => {
        let live = true;
        let pending = false;
        const refresh = async () => {
          if (pending) return;
          pending = true;
          try { const value = await api(); if (live) setState(value); }
          catch (e) { if (live) setError(e.message); }
          finally { pending = false; }
        };
        void refresh();
        const timer = setInterval(refresh, 3000);
        const close = e => { if (root.current && !root.current.contains(e.target)) setOpen(false); };
        const escape = e => { if (e.key === 'Escape') setOpen(false); };
        document.addEventListener('pointerdown', close);
        document.addEventListener('keydown', escape);
        return () => { live = false; clearInterval(timer); document.removeEventListener('pointerdown', close); document.removeEventListener('keydown', escape); };
      }, []);
      const run = async action => {
        setBusy(true); setError('');
        try { setState(await api(action)); if (action !== 'quit') setOpen(false); }
        catch (e) { setError(e.message); setOpen(true); }
        finally { setBusy(false); }
      };
      const shown = state?.shown && !state?.user_hidden;
      const label = state ? !state.running ? '已停止' : state.user_hidden ? '已收起' : shown ? '已显示' : '跟随待显示' : '连接中';
      const button = {font: 'inherit', color: 'inherit', cursor: 'pointer', border: '1px solid #8884', borderRadius: 7,
        background: 'transparent', padding: '6px 9px', minHeight: 30};
      if (menu) return h('div', {style: {padding: 8, width: 240, display: 'grid', gap: 7, fontSize: 13}},
        h('strong', null, `百炼浮窗 · ${busy ? '处理中' : label}`),
        ...[['show', '显示浮窗'], ['hide', '隐藏浮窗'], ['panel', '打开面板'], ['quit', '退出服务']].map(([action, text]) =>
          h('button', {key: action, type: 'button', disabled: busy || (!state?.running && ['hide', 'quit'].includes(action)),
            style: {...button, textAlign: 'left'}, onClick: () => void run(action)}, text)),
        (error || state?.startupError) && h('div', {role: 'alert'}, error || state.startupError));
      return h('div', {ref: root, style: {position: 'relative', width: '100%', padding: '4px 0', WebkitAppRegion: 'no-drag'}},
        h('div', {style: {display: 'flex', gap: 4}},
          h('button', {type: 'button', disabled: busy || !state, style: {...button, flex: 1, textAlign: 'left'},
            title: shown ? '收起浮窗' : '显示浮窗',
            onClick: () => void run(shown ? 'hide' : 'show'), 'aria-label': `百炼浮窗：${label}`}, `◈ 百炼浮窗 · ${busy ? '处理中' : label}`),
          h('button', {type: 'button', style: button, onClick: () => setOpen(!open),
            'aria-label': '百炼监控选项', 'aria-expanded': open}, '⋯')),
        open && h('div', {role: 'group', 'aria-label': '百炼监控控制', style: {
          position: 'absolute', left: 0, right: 0, bottom: '100%', zIndex: 1000, padding: 10,
          border: '1px solid #8885', borderRadius: 9, boxShadow: '0 6px 24px #0003',
          background: 'var(--dsw-alias-background-primary, #fff)', display: 'grid', gap: 7}},
          h('strong', null, '百炼监控'),
          ...[['show', '显示浮窗'], ['hide', '隐藏浮窗'], ['panel', '打开面板'], ['quit', '退出服务']].map(([action, text]) =>
            h('button', {key: action, type: 'button', disabled: busy || (!state?.running && ['hide', 'quit'].includes(action)),
              style: {...button, textAlign: 'left'}, onClick: () => void run(action)}, text)),
          (error || state?.startupError) && h('div', {role: 'alert'}, error || state.startupError)));
    }

    return {
      inject: ['slots'],
      apply(ctx) {
        ctx.slots.inject('plugins.bundle.config', () => ctx.slots.register({
          name: 'plugins.bundle.config', key: 'dsh-bailian-hud', inject: () => ({})
        }, Credentials));
        ctx.slots.inject('sidebar.footer.action', () => ctx.slots.register({
          name: 'sidebar.footer.action', id: 'bailian-hud', key: 'bailian-hud', order: 30, inject: () => ({})
        }, Controls));
        // Some sidebar themes take over the footer; More remains a native, visible entry.
        ctx.slots.inject('sidebar.panellist.actions', () => ctx.slots.register({
          name: 'sidebar.panellist.actions', id: 'bailian-hud-actions', order: 30,
          inject: () => ({menu: true})
        }, Controls));
      }
    };
  }
});
