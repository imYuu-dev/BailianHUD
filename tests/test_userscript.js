// Execute the userscript against a fake coupon page and fake loopback endpoint.
const fs = require('fs');
const vm = require('vm');
const path = require('path');
const assert = require('assert');

const source = fs.readFileSync(path.join(__dirname, '..', 'extras', 'balance-sync.user.js'), 'utf8');

async function scenario(binding, coupons) {
  const pushes = [];
  const response = (data) => ({ok: true, status: 200, json: async () => data});
  const context = {
    window: {ALIYUN_CONSOLE_CONFIG: {SEC_TOKEN: 'fixture'}, addEventListener() {}},
    document: {hidden: false, addEventListener() {}},
    setInterval() {}, URLSearchParams, encodeURIComponent,
    console: {log() {}, warn() {}},
    fetch: async (url, options) => {
      if (url.includes('/api/coupon-binding')) return response({ok: true, ...binding});
      if (url.includes('/data/api.json')) return response({data: {Data: coupons}});
      if (url.includes('/api/balance')) {
        pushes.push(JSON.parse(options.body));
        return response({ok: true});
      }
      throw new Error('unexpected URL');
    },
  };
  vm.runInNewContext(source, context, {timeout: 1000});
  await new Promise((resolve) => setImmediate(resolve));
  return pushes;
}

const coupon = (id, balance, nominal = 300) => ({
  CouponNo: id, CouponId: `id-${id}`, RemainAmount: balance,
  NominalValue: nominal, Description: '百炼券',
});

(async () => {
  const a = await scenario({configured: 'A', coupon_no: 'A', coupon_id: 'id-A'},
    [coupon('A', 0), coupon('B', 200, 500)]);
  assert.strictEqual(a.length, 1);
  assert.strictEqual(a[0].coupon, 'A');
  assert.strictEqual(a[0].remain, 0);
  assert.strictEqual(a[0].coupon_id, 'id-A');

  const b = await scenario({configured: 'A'}, [coupon('B', 200)]);
  assert.strictEqual(b.length, 0);

  const c = await scenario({}, [coupon('A', 0, 500), coupon('B', 200, 300)]);
  assert.strictEqual(c.length, 1);
  assert.strictEqual(c[0].coupon, 'B');

  const d = await scenario({}, [coupon('A', null), coupon('B', 'bad')]);
  assert.strictEqual(d.length, 0);
  process.stdout.write('userscript: 4 scenarios passed\n');
})().catch((error) => { process.stderr.write(String(error.stack || error) + '\n'); process.exitCode = 1; });
