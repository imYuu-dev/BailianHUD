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
      const [name, setName] = React.useState('');
      const [id, setId] = React.useState('');
      const [secret, setSecret] = React.useState('');
      const [adding, setAdding] = React.useState(false);
      const [state, setState] = React.useState(null);
      const [busy, setBusy] = React.useState(false);
      const [error, setError] = React.useState('');
      const revision = React.useRef(0);
      React.useEffect(() => {
        let live = true;
        let pending = false;
        const refresh = async () => {
          if (pending) return;
          pending = true;
          const version = revision.current;
          try { const value = await credentials(); if (live && version === revision.current) setState(value); }
          catch (e) { if (live && version === revision.current) setError(e.message); }
          finally { pending = false; }
        };
        void refresh();
        const timer = setInterval(refresh, 3000);
        return () => { live = false; clearInterval(timer); };
      }, []);
      const update = async body => {
        if (busy) return;
        setBusy(true); setError('');
        revision.current++;
        try {
          setState(await credentials(body));
          return true;
        } catch (e) { setError(e.message); }
        finally { revision.current++; setBusy(false); }
      };
      const save = async event => {
        event.preventDefault();
        if (await update({name, aliyunAkId: id, aliyunAkSecret: secret})) {
          setName(''); setId(''); setSecret(''); setAdding(false);
        }
      };
      const field = {font: 'inherit', color: 'inherit', width: '100%', boxSizing: 'border-box',
        padding: '10px 12px', border: '1px solid #8885', borderRadius: 7, background: 'transparent'};
      const status = !state ? '正在读取配置' : !state.running ? '服务已停止'
        : !state.configured ? '尚未导入账号'
        : state.sync?.ok ? '已同步' : state.sync?.error || '已保存，正在同步';
      const accounts = state?.accounts || [];
      const button = {font: 'inherit', color: 'inherit', padding: '9px 14px', border: '1px solid #8885',
        borderRadius: 7, background: 'var(--dsw-alias-background-secondary, #8881)', cursor: 'pointer'};
      return h('section', {style: {maxWidth: 460, padding: '24px 0', display: 'grid', gap: 20}},
        h('h2', {style: {fontSize: 18, margin: 0}}, '阿里云 AccessKey'),
        state?.running === false && h('button', {type: 'button', style: {...button, justifySelf: 'start'},
          disabled: busy, onClick: () => void update({action: 'list'})}, '读取账号'),
        Number.isInteger(state?.count) && h('div', {style: {display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 16}},
          h('span', null, `已导入 ${state.count} 个账号`),
          accounts.length > 0 && !adding && h('button', {type: 'button', style: button,
            disabled: busy, onClick: () => setAdding(true)}, '添加账号')),
        accounts.length > 0 && h('label', {style: {display: 'grid', gap: 8}}, '当前账号',
          h('select', {value: state.activeAccountId, disabled: busy, style: field,
            onChange: event => void update({action: 'select', accountId: event.target.value})},
          ...accounts.map(account => h('option', {key: account.id, value: account.id},
            `${account.name} · ${account.maskedAkId}`)))),
        (adding || (state?.running && !accounts.length)) && h('form', {onSubmit: save, autoComplete: 'off', style: {display: 'grid', gap: 16}},
          h('label', {style: {display: 'grid', gap: 8}}, '账号名称',
            h('input', {value: name, onChange: e => setName(e.target.value), disabled: busy,
              autoComplete: 'off', maxLength: 60, placeholder: '可选', style: field})),
          h('label', {style: {display: 'grid', gap: 8}}, 'AccessKey ID',
            h('input', {value: id, onChange: e => setId(e.target.value), disabled: busy, required: true,
              spellCheck: false, autoComplete: 'off', maxLength: 512, style: field})),
          h('label', {style: {display: 'grid', gap: 8}}, 'AccessKey Secret',
            h('input', {type: 'password', value: secret, onChange: e => setSecret(e.target.value), disabled: busy,
              required: true, autoComplete: 'off', maxLength: 512, style: field})),
          h('div', {style: {display: 'flex', gap: 8}},
            h('button', {type: 'submit', disabled: busy || !id.trim() || !secret.trim(), style: button},
              busy ? '正在导入…' : '导入账号'),
            accounts.length > 0 && h('button', {type: 'button', disabled: busy, style: button,
              onClick: () => { setAdding(false); setSecret(''); }}, '取消'))),
        h('div', {role: error || state?.sync?.error ? 'alert' : 'status', 'aria-live': 'polite',
          style: {fontSize: 13}}, error || (busy ? '正在更新…' : status)));
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
