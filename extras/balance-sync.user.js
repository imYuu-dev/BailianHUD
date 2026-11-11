// ==UserScript==
// @name         百炼代金券余额 → 本地监控同步
// @namespace    local.voucher.balancesync
// @version      1.3
// @description  在阿里云卡券页读取真实余额并推送给本地监控代理（127.0.0.1:18765），覆盖所有渠道的消耗；后台标签页会被浏览器节流，过期时浮窗会提示
// @match        https://billing-cost.console.aliyun.com/coupons/*
// @grant        none
// @run-at       document-idle
// ==/UserScript==

(function () {
  "use strict";
  const PUSH = "http://127.0.0.1:18765/api/balance";
  const BINDING = "http://127.0.0.1:18765/api/coupon-binding";
  const INTERVAL = 20000;

  async function readBinding() {
    const r = await fetch(BINDING, { cache: "no-store" });
    if (!r.ok) throw new Error("读取本地绑定券失败，HTTP " + r.status);
    const binding = await r.json();
    if (!binding || !binding.ok) throw new Error("读取本地绑定券失败");
    return [binding.configured, binding.coupon_no, binding.coupon_id]
      .filter(Boolean).map((v) => String(v).trim().toLowerCase());
  }

  async function readBalance(binding) {
    const tok = (window.ALIYUN_CONSOLE_CONFIG || {}).SEC_TOKEN;
    if (!tok) return null;
    const body = new URLSearchParams({
      product: "BssOpenAPI-V3",
      action: "DescribeCoupon",
      params: JSON.stringify({ Nbid: "2684201000001", CurrentPage: 1, PageSize: 20 }),
      region: "cn-hangzhou",
    }).toString();
    const r = await fetch("/data/api.json?_fetcher_=BssOpenAPI-V3__DescribeCoupon&sec_token=" + encodeURIComponent(tok), {
      method: "POST",
      credentials: "include",
      headers: { "content-type": "application/x-www-form-urlencoded" },
      body,
    });
    const j = await r.json();
    const list = (j.data && j.data.Data) || [];
    if (!Array.isArray(list)) return null;
    // 有绑定时只选择同一张券；余额为 0 也是有效状态，不能沿用旧的正数。
    const matches = list.filter((x) =>
      /云工开物|学生|百炼/.test((x.Remark || "") + " " + (x.Description || ""))
      && x.RemainAmount != null && Number.isFinite(Number(x.RemainAmount))
      && Number(x.RemainAmount) >= 0
      && (x.CouponNo || x.CouponId || x.CashCouponId)
      && (!binding.length || [x.CouponNo, x.CouponId, x.CashCouponId]
        .filter(Boolean).some((id) => binding.includes(String(id).trim().toLowerCase()))));
    matches.sort((a, b) => (Number(b.RemainAmount) > 0) - (Number(a.RemainAmount) > 0)
      || Number(b.NominalValue) - Number(a.NominalValue));
    const c = matches[0];
    return c ? {
      remain: Number(c.RemainAmount),
      coupon: c.CouponNo,
      coupon_id: c.CouponId || c.CashCouponId || "",
      nominal: Number(c.NominalValue),
    } : null;
  }

  async function tick() {
    try {
      const binding = await readBinding();
      const b = await readBalance(binding);
      if (!b) return;
      const r = await fetch(PUSH, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ remain: b.remain, coupon: b.coupon,
                               coupon_id: b.coupon_id, nominal: b.nominal }),
      });
      if (r.ok) console.log("[balance-sync] 已推送 ¥" + b.remain.toFixed(2));
      else console.warn("[balance-sync] 本地代理拒绝推送，HTTP " + r.status);
    } catch (e) {
      console.warn("[balance-sync] 推送失败（本地代理是否在运行？）", e);
    }
  }

  tick();
  setInterval(tick, INTERVAL);
  // 后台标签会被节流，重新可见时立即补一次
  document.addEventListener("visibilitychange", () => { if (!document.hidden) tick(); });
  window.addEventListener("focus", tick);
})();
