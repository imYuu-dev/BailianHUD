#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BailianHUD - 百炼代金券 / 峰谷价 桌面悬浮仪表（单文件，单进程）

一个进程同时承担：
  1. 本地反向代理（默认 127.0.0.1:8765），转发 ZCode / 任意 agent 的百炼请求；
  2. 用量统计（token、缓存命中、忙闲时计价、今日用量、历史曲线）；
  3. 毛玻璃悬浮卡片：余额、峰谷倒计时、消耗进度，悬停展开详情。

设计要点：
  * UI 由 Pillow 离屏绘制，经 UpdateLayeredWindow 贴到窗口 -> 圆角抗锯齿、真实半透明、柔和投影；
  * 所有网络 / 磁盘 IO 都在后台线程，UI 线程只读内存快照，永不卡顿；
  * 窗口 WS_EX_NOACTIVATE，不抢焦点；鼠标不在卡片上时自动点击穿透；前台全屏应用时自动隐藏；
  * 位置、校准值、透明度落盘，重启即恢复。

用法：
  pythonw BailianHUD.py                 # 常驻
  python  BailianHUD.py --preview out   # 只渲染预览图，不开窗口
"""
from __future__ import annotations

import argparse
import base64
import ctypes
import hashlib
import hmac
import json
import math
import os
import queue
import secrets
import sys
import tempfile
import threading
import time
import tkinter as tk
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from collections import deque
from ctypes import wintypes
from datetime import datetime, time as dtime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from hud_runtime import (DEFAULT_PORT, PLUGIN_PORT, ParentWatch, InstanceLock, clear_instance, directory_id, discover,
                         local_json, publish_instance)

APP_NAME = "BailianHUD"
APP_VERSION = "2.5"
# 数据与程序分离：更新 exe 或分享源码时不会带上个人余额和令牌。
BASE_DIR = Path(os.environ.get("BAILIAN_HUD_HOME") or
                (Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")) / APP_NAME))
USAGE_LOG = BASE_DIR / "bailian_usage.jsonl"
STATE_FILE = BASE_DIR / ".hud_state.json"
LOG_FILE = BASE_DIR / ".hud.log"

LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = DEFAULT_PORT
UPSTREAM = os.environ.get("BAILIAN_UPSTREAM", "https://dashscope.aliyuncs.com")

# 允许把余额推送到本地浮窗的浏览器 Origin（油猴脚本所在的阿里云卡券页）。
# 其他来源必须带 X-HUD-Token；CORS 不再使用通配符。
PUSH_ORIGINS = frozenset({"https://billing-cost.console.aliyun.com"})
MAX_API_BODY = 1_000_000
MAX_PROXY_BODY = 64 * 1024 * 1024
MAX_PARSE_BYTES = 2_000_000
ABORT_GRACE_SEC = 30.0

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Asia/Shanghai")
except Exception:  # Windows 没有 tzdata 时仍按北京时间计价
    TZ = timezone(timedelta(hours=8), "UTC+08:00")

# ---------------------------------------------------------------- 计价表
# 每百万 token 价格（人民币）。peak/offpeak 为 (输入, 输出)；flat 不分时段。
# 缓存命中按输入价 10%，缓存写入按 125%（百炼通用规则）。
PRICING = {
    "deepseek-v4.1-flash": {"peak": (2.0, 8.0), "offpeak": (1.0, 4.0)},
    "deepseek-v4.1-flash-1017": {"peak": (2.0, 8.0), "offpeak": (1.0, 4.0)},
    "deepseek-v4-pro": {"flat": (12.0, 24.0)},
    "deepseek-v4-pro-0813": {"peak": (9.0, 27.0), "offpeak": (4.5, 13.5)},
    "deepseek-v4-flash-0731": {"peak": (3.0, 9.0), "offpeak": (1.5, 4.5)},
    "qwen3.8-max": {"flat": (12.0, 36.0)},
    "qwen3.8-max-0902": {"flat": (12.0, 36.0)},
    "qwen3.7-max": {"flat": (12.0, 36.0)},
    "qwen3.8-flash": {"flat": (0.8, 2.7)},
    "qwen3.7-plus": {"flat": (2.0, 8.0)},
    "qwen3.7-flash": {"flat": (0.2, 0.8)},
    "qwen-flash": {"flat": (0.15, 1.5)},
    "qwen-plus": {"flat": (0.8, 2.0)},
    "kimi-k3": {"flat": (20.0, 100.0)},
    "kimi/kimi-k3": {"flat": (20.0, 100.0)},
    "kimi/kimi-k3.1-pro": {"flat": (26.0, 136.0)},
    "kimi/kimi-k3.1-pro-highspeed": {"flat": (26.0, 136.0)},
    "glm-5.3": {"flat": (8.0, 28.0)},
    "glm-5.2": {"flat": (8.0, 28.0)},
}

PEAK_START = dtime(8, 0)
PEAK_END = dtime(22, 0)

# ---------------------------------------------------------------- 小工具
_LOG_LOCK = threading.Lock()


def log(msg: str) -> None:
    """pythonw 下没有 stderr，写文件；绝不让日志异常影响主流程。"""
    try:
        with _LOG_LOCK:
            if LOG_FILE.exists() and LOG_FILE.stat().st_size > 1_500_000:
                LOG_FILE.write_text("", encoding="utf-8")
            with LOG_FILE.open("a", encoding="utf-8") as f:
                f.write(f"{datetime.now().strftime('%m-%d %H:%M:%S')} {msg}\n")
    except Exception:
        pass


def now_dt() -> datetime:
    return datetime.now(TZ) if TZ else datetime.now()


def local_dt(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, TZ) if TZ else datetime.fromtimestamp(ts)


def is_peak(dt: datetime | None = None) -> bool:
    dt = dt or now_dt()
    return PEAK_START <= dt.time() < PEAK_END


def window_bounds(dt: datetime) -> tuple[datetime, datetime]:
    """当前计价时段的起止。"""
    day = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    if is_peak(dt):
        return day.replace(hour=8), day.replace(hour=22)
    if dt.time() < PEAK_START:
        return day - timedelta(hours=2), day.replace(hour=8)   # 前一天 22:00 -> 今天 08:00
    return day.replace(hour=22), day + timedelta(days=1, hours=8)  # 今天 22:00 -> 次日 08:00


def price_for(model: str, peak: bool):
    p = PRICING.get(model)
    if not p:
        for key in sorted(PRICING, key=len, reverse=True):
            if model and model.startswith(key):
                p = PRICING[key]
                break
    if not p:
        return None
    return p["flat"] if "flat" in p else (p["peak"] if peak else p["offpeak"])


def compute_cost(model, inp, cached, cache_write, out, peak):
    rate = price_for(model, peak)
    if rate is None:
        return None
    in_price, out_price = rate
    return (inp / 1_000_000 * in_price
            + cached / 1_000_000 * in_price * 0.10
            + cache_write / 1_000_000 * in_price * 1.25
            + out / 1_000_000 * out_price)


def compute_cache_saved(model, cached, peak):
    """用请求发生时的价格计算缓存节省；未知模型不猜价格。"""
    rate = price_for(model, peak)
    return None if rate is None else cached / 1_000_000 * rate[0] * 0.90


def _tokens(value) -> int:
    """把上游返回的 token 数夹到合理范围，避免负数/异常值把成本算反。"""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, min(n, 1_000_000_000_000))


def parse_usage_anthropic(obj):
    usage = obj.get("usage") or {}
    if not isinstance(usage, dict):
        return None
    inp = _tokens(usage.get("input_tokens"))
    cached = _tokens(usage.get("cache_read_input_tokens"))
    write = _tokens(usage.get("cache_creation_input_tokens"))
    out = _tokens(usage.get("output_tokens"))
    if not any((inp, cached, write, out)):
        return None
    return inp, cached, write, out, obj.get("model")


def parse_usage_openai(obj):
    usage = obj.get("usage") or {}
    if not isinstance(usage, dict) or not usage:
        return None
    cached = _tokens((usage.get("prompt_tokens_details") or {}).get("cached_tokens"))
    inp = max(0, _tokens(usage.get("prompt_tokens")) - cached)
    out = _tokens(usage.get("completion_tokens"))
    if not any((inp, cached, out)):
        return None
    return inp, cached, 0, out, obj.get("model")


class SseUsageParser:
    """增量解析 SSE，避免把整个流缓存到内存，也避免长流丢最后的 usage。"""

    __slots__ = ("_buf", "inp", "cached", "write", "out", "model")

    def __init__(self):
        self._buf = b""
        self.inp = 0
        self.cached = 0
        self.write = 0
        self.out = 0
        self.model = None

    def feed(self, chunk: bytes) -> None:
        if not chunk:
            return
        self._buf += chunk
        while True:
            idx = self._buf.find(b"\n")
            if idx < 0:
                return
            line, self._buf = self._buf[:idx], self._buf[idx + 1:]
            self._line(line)

    def finish(self) -> None:
        if self._buf.strip():
            self._line(self._buf)
        self._buf = b""

    def _line(self, line: bytes) -> None:
        line = line.strip()
        if not line.startswith(b"data:"):
            return
        payload = line[5:].strip()
        if not payload or payload == b"[DONE]":
            return
        try:
            obj = json.loads(payload.decode("utf-8", "ignore"))
        except Exception:
            return
        if isinstance(obj, dict):
            self._merge(obj)

    def _merge(self, obj: dict) -> None:
        t = obj.get("type")
        self.model = obj.get("model") or self.model
        usage = obj.get("usage")
        if t == "message_start" and isinstance(obj.get("message"), dict):
            self.model = obj["message"].get("model") or self.model
            usage = obj["message"].get("usage") or usage
        if not isinstance(usage, dict):
            return
        self.inp = max(self.inp, _tokens(usage.get("input_tokens")))
        self.cached = max(self.cached, _tokens(usage.get("cache_read_input_tokens")))
        self.write = max(self.write, _tokens(usage.get("cache_creation_input_tokens")))
        self.out = max(self.out, _tokens(usage.get("output_tokens")))
        if usage.get("prompt_tokens") is not None:
            c2 = _tokens((usage.get("prompt_tokens_details") or {}).get("cached_tokens"))
            self.cached = max(self.cached, c2)
            self.inp = max(self.inp, max(0, _tokens(usage.get("prompt_tokens")) - c2))
        if usage.get("completion_tokens") is not None:
            self.out = max(self.out, _tokens(usage.get("completion_tokens")))

    def result(self):
        if not any((self.inp, self.cached, self.write, self.out)):
            return None
        return self.inp, self.cached, self.write, self.out, self.model


def extract_from_sse(text):
    parser = SseUsageParser()
    parser.feed(text.encode("utf-8", "ignore"))
    parser.finish()
    return parser.result()


def fmt_age(sec: float | None) -> str:
    if sec is None:
        return ""
    sec = max(0, int(sec))
    if sec < 10:
        return "刚刚"
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m"
    if sec < 86400:
        return f"{sec // 3600}h"
    return f"{sec // 86400}d"


def fmt_compact_money(v: float) -> str:
    if v >= 1000:
        return f"{v:,.0f}"
    if v >= 100:
        return f"{v:.1f}"
    return f"{v:.2f}"


def fmt_tokens(n: float) -> str:
    n = float(n or 0)
    if n >= 1_000_000_000:
        return f"{n / 1e9:.2f}B"
    if n >= 1_000_000:
        return f"{n / 1e6:.1f}M"
    if n >= 1_000:
        return f"{n / 1e3:.0f}K"
    return f"{int(n)}"


# ---------------------------------------------------------------- 阿里云 BSS（真实券余额）
# 百炼的 sk- key 查不到余额；代金券余额只能走 BSS OpenAPI（QueryCashCoupons）。
# 环境变量优先，其次程序私有配置；兼容已有 dsh-qwen-balance 配置。
AK_FILE = BASE_DIR / "bss-credentials.json"
LEGACY_AK_FILE = Path.home() / ".dsh" / "qwen-coupon.json"
BSS_VERSION = "2017-12-14"
BSS_HOST = "business.aliyuncs.com"


class BssError(RuntimeError):
    pass


class CouponBindingError(ValueError):
    """推送的数据不属于当前绑定的券。"""


def aliyun_conf() -> dict:
    for path in (AK_FILE, LEGACY_AK_FILE):
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text("utf-8-sig"))
            return data if isinstance(data, dict) else {}
        except Exception as exc:
            log(f"ak file: {exc!r}")
            return {}  # 显式配置损坏时不默默切换到另一个账号。
    return {}


def load_aliyun_ak():
    """返回 (ak_id, ak_secret, sts_token, 来源) 或 None。"""
    envs = (
        ("ALIYUN_AK_ID", "ALIYUN_AK_SECRET", "ALIYUN_STS_TOKEN"),
        ("ALIBABA_CLOUD_ACCESS_KEY_ID", "ALIBABA_CLOUD_ACCESS_KEY_SECRET",
         "ALIBABA_CLOUD_SECURITY_TOKEN"),
        ("ALIYUN_ACCESS_KEY_ID", "ALIYUN_ACCESS_KEY_SECRET", "ALIYUN_SECURITY_TOKEN"),
    )
    for kid, ksec, ktok in envs:
        ak = (os.environ.get(kid) or "").strip()
        sk = (os.environ.get(ksec) or "").strip()
        if ak and sk:
            return ak, sk, (os.environ.get(ktok) or "").strip(), kid
    conf = aliyun_conf()
    ak = str(conf.get("aliyunAkId") or conf.get("accessKeyId") or "").strip()
    sk = str(conf.get("aliyunAkSecret") or conf.get("accessKeySecret") or "").strip()
    tok = str(conf.get("aliyunStsToken") or conf.get("securityToken") or "").strip()
    if ak and sk:
        return ak, sk, tok, "本地配置"
    return None


def _pct(value) -> str:
    """BSS 要求的 RFC3986 编码：空格 %20、~ 保留，其余全部转义。"""
    return urllib.parse.quote(str(value), safe="~")


def bss_call(action: str, params: dict | None = None, timeout: float = 8.0) -> dict:
    cred = load_aliyun_ak()
    if not cred:
        raise BssError("未配置 AccessKey")
    ak, sk, token, _src = cred
    q = {
        "AccessKeyId": ak,
        "Action": action,
        "Format": "JSON",
        "SignatureMethod": "HMAC-SHA1",
        "SignatureVersion": "1.0",
        "SignatureNonce": uuid.uuid4().hex,
        "Timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "Version": BSS_VERSION,
    }
    if token:
        q["SecurityToken"] = token
    for key, val in (params or {}).items():
        if val is not None and val != "":
            q[key] = str(val)
    canon = "&".join(f"{_pct(k)}={_pct(q[k])}" for k in sorted(q))
    to_sign = "GET&%2F&" + _pct(canon)
    sig = base64.b64encode(
        hmac.new((sk + "&").encode(), to_sign.encode(), hashlib.sha1).digest()).decode()
    host = str(aliyun_conf().get("bssEndpoint") or BSS_HOST).strip() or BSS_HOST
    url = f"https://{host}/?{canon}&Signature={_pct(sig)}"
    req = urllib.request.Request(url, headers={
        "Accept": "application/json", "User-Agent": f"{APP_NAME}/{APP_VERSION}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace") if exc.fp else ""
        try:
            err = json.loads(body)
        except Exception:
            err = {}
        raise BssError(f"[{err.get('Code') or 'HTTP ' + str(exc.code)}] "
                       f"{err.get('Message') or body[:120]}") from None
    except Exception as exc:
        raise BssError(f"请求失败：{exc}") from None
    try:
        data = json.loads(body)
    except Exception:
        raise BssError("返回非 JSON：" + body[:140].replace("\n", " ")) from None
    code = data.get("Code")
    if data.get("Success") is False or str(code) not in ("None", "200", "Success"):
        raise BssError(f"[{code}] {data.get('Message') or ''}".strip())
    payload = data.get("Data")
    return payload if isinstance(payload, dict) else {}


def _num(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def finite_float(value, name: str, lo: float, hi: float) -> float:
    """只接受有限且落在 [lo, hi] 内的数字；NaN/Infinity 一律拒绝。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} 不是数字") from None
    if not math.isfinite(v):
        raise ValueError(f"{name} 必须是有限数")
    if not (lo <= v <= hi):
        raise ValueError(f"{name} 超出范围")
    return v


def fetch_coupons() -> list:
    data = bss_call("QueryCashCoupons")
    raw = data.get("CashCoupon") or data.get("CashCoupons") or data.get("Coupon") or []
    if isinstance(raw, dict):
        raw = [raw]
    out = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        try:
            balance = finite_float(item.get("Balance"), "券余额", 0.0, 1e8)
        except ValueError:
            # 缺失或无效余额不等于零，不能用来覆盖上次真实值。
            continue
        out.append({
            "id": str(item.get("CashCouponId") or ""),
            "no": str(item.get("CashCouponNo") or ""),
            "status": str(item.get("Status") or ""),
            "nominal": _num(item.get("NominalValue")),
            "balance": balance,
            "expire": str(item.get("ExpiryTime") or ""),
            "desc": str(item.get("Description") or ""),
            "remark": str(item.get("Remark") or ""),
        })
    return out


COUPON_HINTS = ("云工开物", "百炼", "学生")


def coupon_matches(c: dict) -> bool:
    text = f"{c.get('desc') or ''} {c.get('remark') or ''}"
    return any(hint in text for hint in COUPON_HINTS)


class CouponSync(threading.Thread):
    """每 60 秒查一次阿里云代金券余额，成功即校准，不成功保持上次数据。"""

    INTERVAL = 60.0

    def __init__(self, store, coupon_no: str = ""):
        super().__init__(daemon=True, name="coupon-sync")
        self.store = store
        self.coupon_no = (coupon_no or "").strip()
        self.stop_evt = threading.Event()
        self.wake_evt = threading.Event()
        self.poll_lock = threading.Lock()
        self.lock = threading.Lock()
        self.state = {"ok": False, "error": "", "at": 0.0, "balance": None,
                      "coupon": "", "desc": "", "count": 0, "next": 0.0}

    def status(self) -> dict:
        with self.lock:
            return dict(self.state)

    def credential_status(self) -> dict:
        cred = load_aliyun_ak()
        state = self.status()
        return {"configured": bool(cred),
                "environment": bool(cred and cred[3] != "本地配置"),
                "sync": {key: state[key] for key in ("ok", "error", "at", "next")}}

    def configure_credentials(self, data: dict) -> dict:
        if not isinstance(data, dict):
            raise ValueError("请填写 AccessKey ID 和 AccessKey Secret")
        values = {}
        for key, label in (("aliyunAkId", "AccessKey ID"),
                           ("aliyunAkSecret", "AccessKey Secret")):
            value = data.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"请填写 {label}")
            value = value.strip()
            if len(value) > 512 or any(not 33 <= ord(char) <= 126 for char in value):
                raise ValueError(f"{label} 格式有误，请重新粘贴")
            values[key] = value
        if values["aliyunAkId"].startswith("sk-"):
            raise ValueError("请粘贴阿里云 AccessKey ID")
        with self.poll_lock:
            previous = load_aliyun_ak()
            path = self.store.base_dir / AK_FILE.name
            existing = aliyun_conf()
            if existing.get("bssEndpoint"):
                values["bssEndpoint"] = existing["bssEndpoint"]
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                                 prefix=".ak-", suffix=".tmp", delete=False) as file:
                    temporary = Path(file.name)
                    json.dump(values, file, ensure_ascii=False)
                    file.flush()
                    os.fsync(file.fileno())
                os.replace(temporary, path)
            finally:
                if temporary:
                    temporary.unlink(missing_ok=True)
            current = load_aliyun_ak()
            if current and (not previous or previous[0] != current[0]):
                identity = self.store.coupon_identity()
                self.store.configure_coupon(identity["configured"], reset=True)
                self.coupon_no = identity["configured"]
            self._set(ok=False, error="", at=0.0, next=time.time())
            self.store.set_coupon_sync_error(True)
            self.wake_evt.set()
            return self.credential_status()

    def _pick(self, coupons: list):
        identity = self.store.coupon_identity()
        want = (self.coupon_no or identity["coupon_no"] or identity["coupon_id"]
                or identity["configured"]).lower()
        if want:
            for c in coupons:
                if want in (c["no"].lower(), c["id"].lower()):
                    if c["status"].lower() != "available" and c["balance"] > 0:
                        return None, "绑定的券已失效或不可用"
                    return c, "指定券号"
            return None, "绑定的券号不在可用列表"
        matched = [c for c in coupons if (c["no"] or c["id"]) and c["status"].lower() == "available"
                   and c["balance"] >= 0 and coupon_matches(c)]
        if not matched:
            return None, "没有匹配的百炼/云工开物券"
        matched.sort(key=lambda c: (c["balance"] > 0, c["nominal"], c["balance"]), reverse=True)
        return matched[0], "百炼/云工开物券"

    def _set(self, **kw):
        with self.lock:
            self.state.update(kw)

    def run(self):
        while not self.stop_evt.is_set():
            with self.poll_lock:
                self.wake_evt.clear()
                self._poll()
            self.wake_evt.wait(self.INTERVAL)

    def _poll(self):
        cred = load_aliyun_ak()
        if not cred:
            self._set(ok=False, error="未配置 AccessKey", next=time.time() + self.INTERVAL)
            self.store.set_coupon_sync_error(True)
            return
        try:
            coupons = fetch_coupons()
            chosen, why = self._pick(coupons)
            if chosen is None:
                self._set(ok=False, error=why or "没有可用代金券", at=time.time(),
                          count=len(coupons), next=time.time() + self.INTERVAL)
                issue = ("unavailable" if "已失效" in why else
                         "missing" if "绑定的券号" in why else "error")
                self.store.set_coupon_sync_error(True, issue)
            else:
                bal = float(chosen["balance"])
                nominal = float(chosen["nominal"])
                self.store.push_balance(
                    bal, "real", kind="bss", coupon_no=chosen["no"],
                    coupon_id=chosen["id"], nominal=nominal if 0.01 <= nominal <= 1e7 else None)
                self.store.set_coupon_sync_error(False)
                if not self.coupon_no:
                    self.coupon_no = chosen["no"] or chosen["id"]
                self._set(ok=True, error="", at=time.time(), balance=bal,
                          coupon=chosen["no"], desc=chosen["desc"] or why,
                          count=len(coupons), next=time.time() + self.INTERVAL)
                log(f"coupon sync: {chosen['no']} 余额 {bal:.2f}（{why}）")
        except Exception as exc:
            error = str(exc)
            for value in cred[:3]:
                if value:
                    error = error.replace(value, "***")
            self._set(ok=False, error=error[:180], at=time.time(),
                      next=time.time() + self.INTERVAL)
            self.store.set_coupon_sync_error(True)
            log(f"coupon sync failed: {error[:180]}")

    def stop(self):
        self.stop_evt.set()
        self.wake_evt.set()


# ---------------------------------------------------------------- 状态存储
class Store:
    """代理与 UI 共享的唯一数据源；所有读写都在锁内完成。"""

    def __init__(self, base_dir: Path = BASE_DIR):
        self.base_dir = Path(base_dir)
        self.lock = threading.RLock()
        self._save_lock = threading.Lock()
        self.face = 300.0
        self.base_remain = 0.0        # 首次运行无锚点；界面显示“待同步”，不把 0 当真实余额
        self.base_cost = 0.0          # 校准点的累计估算消费
        self.cost_factor = 1.0        # 估算消费 -> 实际消费
        self.balance_source = "estimate"   # estimate / real
        self.balance_kind = ""             # bss / push / manual / seed
        self.balance_ts = 0.0
        self.coupon_config = ""
        self.coupon_no = ""
        self.coupon_id = ""
        self.coupon_sync_error = False
        self.coupon_sync_issue = ""
        self.seed_note = ""
        self.totals = {"input": 0, "cached": 0, "cache_write": 0, "output": 0,
                       "cost": 0.0, "cache_saved": 0.0, "count": 0,
                       "unpriced_count": 0, "unpriced_tokens": 0}
        self.per_model: dict[str, dict] = {}
        self.daily: dict[str, dict] = {}
        self.hourly: dict[int, dict] = {}
        self.recent: deque = deque(maxlen=200)
        # 统计窗口与最近请求展示列表分开，不能用固定条数截断近 3 小时的消费。
        self._cost_events: deque = deque()
        self.balance_hist: list[tuple[float, float]] = []
        self.started_at = time.time()
        self.seq = 0
        self.last_request_at = 0.0
        self._load()
        self.replay()

    # ---------- 持久化 ----------
    def _state_path(self) -> Path:
        return self.base_dir / STATE_FILE.name

    def _load(self):
        try:
            p = self._state_path()
            if not p.exists():
                return
            d = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(d, dict):
                raise ValueError("state is not an object")

            def num(key, default, lo, hi):
                try:
                    return finite_float(d.get(key, default), key, lo, hi)
                except Exception:
                    log(f"state {key} invalid; using {default}")
                    return default

            self.face = num("face", 300.0, 0.01, 1e7)
            self.base_remain = num("base_remain", self.face, 0.0, self.face * 10)
            self.base_cost = num("base_cost", 0.0, 0.0, 1e12)
            self.cost_factor = num("cost_factor", 1.0, 0.02, 2.0)
            source = str(d.get("balance_source", "estimate"))
            self.balance_source = source if source in ("real", "estimate") else "estimate"
            kind = str(d.get("balance_kind", ""))
            self.balance_kind = kind if kind in ("bss", "push", "manual", "seed") else ""
            self.balance_ts = num("balance_ts", 0.0, 0.0, 1e12)
            self.seed_note = str(d.get("seed_note", ""))[:500]
            self.coupon_config = str(d.get("coupon_config") or "").strip()[:200]
            self.coupon_no = str(d.get("coupon_no") or "").strip()[:200]
            self.coupon_id = str(d.get("coupon_id") or "").strip()[:200]

            raw_hist = d.get("balance_hist") or []
            hist: list[tuple[float, float]] = []
            if isinstance(raw_hist, list):
                for pair in raw_hist[-240:]:
                    try:
                        ts, val = float(pair[0]), float(pair[1])
                    except Exception:
                        continue
                    if (math.isfinite(ts) and math.isfinite(val)
                            and 0.0 <= val <= self.face * 10):
                        hist.append((ts, val))
            self.balance_hist = hist[-120:]
        except Exception as exc:
            log(f"state load failed: {exc!r}")

    def save(self, force: bool = False):
        try:
            with self._save_lock:
                # 在持久化锁内取快照，避免并发同步时旧快照晚于新快照写入。
                with self.lock:
                    data = {
                        "face": round(self.face, 4),
                        "base_remain": round(self.base_remain, 6),
                        "base_cost": round(self.base_cost, 6),
                        "cost_factor": round(self.cost_factor, 6),
                        "balance_source": self.balance_source,
                        "balance_kind": self.balance_kind,
                        "balance_ts": self.balance_ts,
                        "coupon_config": self.coupon_config,
                        "coupon_no": self.coupon_no,
                        "coupon_id": self.coupon_id,
                        "seed_note": self.seed_note,
                        "balance_hist": [[round(t, 1), round(v, 6)] for t, v in self.balance_hist[-120:]],
                        "updated": time.time(),
                        "app": APP_NAME,
                        "version": APP_VERSION,
                    }
                tmp = self._state_path().with_suffix(".tmp")
                tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1,
                                          allow_nan=False), encoding="utf-8")
                tmp.replace(self._state_path())
        except Exception as exc:
            log(f"state save failed: {exc!r}")

    # ---------- 历史回放 ----------
    def replay(self):
        path = self.base_dir / USAGE_LOG.name
        if not path.exists():
            return 0
        totals = {k: 0 for k in ("input", "cached", "cache_write", "output")}
        totals.update({"cost": 0.0, "cache_saved": 0.0, "count": 0,
                       "unpriced_count": 0, "unpriced_tokens": 0})
        per: dict[str, dict] = {}
        daily: dict[str, dict] = {}
        hourly: dict[int, dict] = {}
        recent: deque = deque(maxlen=200)
        cost_events: deque = deque()
        since = time.time() - 3 * 3600
        try:
            with path.open("r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                    except Exception:
                        continue
                    if not isinstance(e, dict):
                        continue
                    self._accumulate(e, totals, per, daily, hourly)
                    slim = self._slim(e)
                    recent.append(slim)
                    if slim["t"] is not None and slim["t"] >= since and slim["cost"] is not None:
                        cost_events.append((slim["t"], min(slim["cost"], 1e9)))
        except Exception as exc:
            log(f"replay failed: {exc!r}")
            return 0
        with self.lock:
            self.totals = totals
            self.per_model = per
            self.daily = daily
            self.hourly = hourly
            self.recent = recent
            self._cost_events = cost_events
            ts = [e.get("t", 0) for e in recent if e.get("t")]
            self.last_request_at = max(ts) if ts else 0.0
        log(f"replayed {totals['count']} requests, cost {totals['cost']:.4f}")
        return totals["count"]

    @staticmethod
    def _slim(e: dict) -> dict:
        try:
            t = float(e.get("t"))
        except (TypeError, ValueError):
            t = None
        if t is not None and not math.isfinite(t):
            t = None
        cost_raw = e.get("cost")
        if cost_raw is None:
            cost = None
        else:
            try:
                cost = float(cost_raw)
            except (TypeError, ValueError):
                cost = 0.0
            if not math.isfinite(cost) or cost < 0:
                cost = 0.0
        ts = e.get("ts")
        model = e.get("model")
        return {
            "ts": (ts[:32] if isinstance(ts, str) else ""),
            "t": t,
            "model": ((model[:120] or "unknown") if isinstance(model, str) else "unknown"),
            "input": _tokens(e.get("input")),
            "cached": _tokens(e.get("cached")),
            "cache_write": _tokens(e.get("cache_write")),
            "output": _tokens(e.get("output")),
            "peak": bool(e.get("peak")),
            "cost": cost,
        }

    @staticmethod
    def _accumulate(e, totals, per, daily, hourly):
        inp = _tokens(e.get("input"))
        cached = _tokens(e.get("cached"))
        write = _tokens(e.get("cache_write"))
        out = _tokens(e.get("output"))
        cost_raw = e.get("cost")
        unpriced = cost_raw is None
        try:
            cost = float(cost_raw) if not unpriced else 0.0
        except (TypeError, ValueError):
            cost = 0.0
        if not math.isfinite(cost) or cost < 0:
            cost = 0.0
        cost = min(cost, 1e9)
        model = e.get("model") or "unknown"
        if not isinstance(model, str):
            model = "unknown"
        model = model[:120] or "unknown"
        totals["input"] += inp
        totals["cached"] += cached
        totals["cache_write"] += write
        totals["output"] += out
        totals["cost"] += cost
        totals["count"] += 1
        saved = e.get("cache_saved")
        if saved is None and not unpriced:
            # 旧流水没有记录节省值，按记录的忙闲档位和当前价目表回放。
            saved = compute_cache_saved(model, cached, bool(e.get("peak")))
        try:
            saved = float(saved or 0.0)
        except (TypeError, ValueError):
            saved = 0.0
        if not math.isfinite(saved) or saved < 0:
            saved = 0.0
        totals["cache_saved"] = totals.get("cache_saved", 0.0) + min(saved, 1e9)
        if unpriced:
            totals["unpriced_count"] = totals.get("unpriced_count", 0) + 1
            totals["unpriced_tokens"] = (totals.get("unpriced_tokens", 0)
                                         + inp + cached + write + out)
        pm = per.setdefault(model, {"count": 0, "input": 0, "cached": 0,
                                    "cache_write": 0, "output": 0, "cost": 0.0})
        pm["count"] += 1
        pm["input"] += inp
        pm["cached"] += cached
        pm["cache_write"] += write
        pm["output"] += out
        pm["cost"] += cost
        try:
            ts = float(e.get("t") or 0)
        except (TypeError, ValueError):
            ts = 0.0
        if not math.isfinite(ts):
            ts = 0.0
        if ts > 0:
            dt = local_dt(ts)
            dk = dt.strftime("%Y-%m-%d")
            day = daily.setdefault(dk, {"cost": 0.0, "tokens": 0, "count": 0})
            day["cost"] += cost
            day["tokens"] += inp + cached + write + out
            day["count"] += 1
            hk = int(ts // 3600)
            hour = hourly.setdefault(hk, {"cost": 0.0, "tokens": 0, "count": 0})
            hour["cost"] += cost
            hour["tokens"] += inp + cached + write + out
            hour["count"] += 1

    # ---------- 记录一次调用 ----------
    def record(self, model, inp, cached, write, out, peak, path=""):
        inp = _tokens(inp)
        cached = _tokens(cached)
        write = _tokens(write)
        out = _tokens(out)
        if not isinstance(model, str):
            model = "unknown"
        model = model[:120] or "unknown"
        cost = compute_cost(model, inp, cached, write, out, peak)
        cache_saved = compute_cache_saved(model, cached, peak)
        ts = time.time()
        entry = {
            "ts": local_dt(ts).strftime("%H:%M:%S"),
            "t": ts,
            "model": model,
            "input": inp, "cached": cached, "cache_write": write, "output": out,
            "peak": bool(peak), "cost": cost, "cache_saved": cache_saved,
            "path": str(path or "").split("?", 1)[0][:200],
        }
        with self.lock:
            self._accumulate(entry, self.totals, self.per_model, self.daily, self.hourly)
            self.recent.append(self._slim(entry))
            if cost is not None:
                self._cost_events.append((ts, min(cost, 1e9)))
            self.seq += 1
            self.last_request_at = ts
            self._trim_hourly()
        try:
            with (self.base_dir / USAGE_LOG.name).open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False, allow_nan=False) + "\n")
        except Exception as exc:
            log(f"append usage failed: {exc!r}")
        return entry

    def _trim_hourly(self):
        if len(self.hourly) <= 80:
            return
        keep = sorted(self.hourly)[-48:]
        self.hourly = {k: self.hourly[k] for k in keep}

    # ---------- 校准 / 推送 ----------
    def coupon_identity(self):
        with self.lock:
            return {"configured": self.coupon_config, "coupon_no": self.coupon_no,
                    "coupon_id": self.coupon_id}

    def configure_coupon(self, coupon: str, *, reset=False):
        """配置变更时清除旧身份，等待新券同步。"""
        coupon = str(coupon or "").strip()[:200]
        with self.lock:
            if coupon == self.coupon_config and not reset:
                return
            known = {s.lower() for s in (self.coupon_no, self.coupon_id) if s}
            reset = bool(reset or (coupon and coupon.lower() not in known)
                         or (not coupon and self.coupon_config))
            self.coupon_config = coupon
            if reset:
                self.coupon_no = self.coupon_id = ""
                self.balance_source = "estimate"
                self.balance_kind = "seed"
                self.balance_ts = 0.0
                self.balance_hist.clear()
                self.seed_note = "等待同步新的余额"
        self.save()

    def set_coupon_sync_error(self, failed: bool, issue: str = "error"):
        with self.lock:
            self.coupon_sync_error = bool(failed)
            self.coupon_sync_issue = issue if failed and issue in ("error", "missing", "unavailable") else ""

    def push_balance(self, remain: float, source: str = "real", kind: str = "push",
                     coupon_no: str = "", coupon_id: str = "", nominal=None):
        coupon_no = str(coupon_no or "").strip()[:200]
        coupon_id = str(coupon_id or "").strip()[:200]
        with self.lock:
            face = self.face if nominal is None else finite_float(nominal, "nominal", 0.01, 1e7)
            remain = finite_float(remain, "remain", 0.0, max(1.0, face * 10))
            incoming = {s.lower() for s in (coupon_no, coupon_id) if s}
            bound = {s.lower() for s in (self.coupon_config, self.coupon_no, self.coupon_id) if s}
            if bound and not incoming.intersection(bound):
                raise CouponBindingError("推送券号与当前绑定的券不一致")
            if ((self.coupon_no and coupon_no and self.coupon_no.lower() != coupon_no.lower())
                    or (self.coupon_id and coupon_id and self.coupon_id.lower() != coupon_id.lower())):
                raise CouponBindingError("推送券身份与当前绑定的券不一致")
            if coupon_no:
                self.coupon_no = coupon_no
            if coupon_id:
                self.coupon_id = coupon_id
            self.face = face
            self.base_remain = remain
            self.base_cost = self.totals["cost"]
            self.balance_source = "real" if source == "real" else "estimate"
            self.balance_kind = kind if kind in ("bss", "push", "manual") else "push"
            self.seed_note = ""
            self.balance_ts = time.time()
            self.balance_hist.append((self.balance_ts, remain))
            if len(self.balance_hist) > 120:
                del self.balance_hist[: len(self.balance_hist) - 120]
        self.save()
        log(f"balance push {remain:.4f} ({source}/{kind})")

    def calibrate(self, remain: float, face=None):
        with self.lock:
            face = self.face if face is None else finite_float(face, "face", 0.01, 1e7)
            remain = finite_float(remain, "remain", 0.0, max(1.0, face * 10))
            interval_cost = self.totals["cost"] - self.base_cost
            interval_actual = self.base_remain - remain
            if interval_cost > 0.5 and interval_actual > 0:
                ratio = interval_actual / interval_cost
                self.cost_factor = max(0.02, min(2.0, ratio))
            self.face = face
            self.base_remain = remain
            self.base_cost = self.totals["cost"]
            self.balance_source = "real"
            self.balance_kind = "manual"
            self.seed_note = ""
            self.balance_ts = time.time()
            self.balance_hist.append((self.balance_ts, self.base_remain))
        self.save()
        return self.cost_factor

    # ---------- 快照 ----------
    def snapshot(self) -> dict:
        dt = now_dt()
        peak = is_peak(dt)
        w_start, w_end = window_bounds(dt)
        wall = time.time()
        since = wall - 3 * 3600
        with self.lock:
            totals = dict(self.totals)
            per_model = {k: dict(v) for k, v in self.per_model.items()}
            all_recent = list(self.recent)
            recent = all_recent[-30:][::-1]
            # 流水写入顺序可能与时间戳稍有不同，不能只从 deque 左侧删除。
            self._cost_events = deque((t, c) for t, c in self._cost_events if t >= since)
            cost_events = list(self._cost_events)
            daily = {k: dict(v) for k, v in self.daily.items()}
            hourly = {k: dict(v) for k, v in self.hourly.items()}
            face = self.face
            base_remain = self.base_remain
            base_cost = self.base_cost
            factor = self.cost_factor
            source = self.balance_source
            kind = self.balance_kind
            coupon_sync_error = self.coupon_sync_error
            coupon_sync_issue = self.coupon_sync_issue
            seed_note = self.seed_note
            bts = self.balance_ts
            bal_hist = list(self.balance_hist)
            last_req = self.last_request_at
            seq = self.seq

        balance_available = bool(source == "real" and bts)
        delta_cost = max(0.0, totals["cost"] - base_cost)
        if not math.isfinite(face) or face <= 0:
            face = 300.0
        if not math.isfinite(factor):
            factor = 1.0
        if not math.isfinite(base_remain):
            base_remain = face
        est_spend = max(0.0, min(delta_cost * factor, 1e9)) if balance_available else 0.0
        if not math.isfinite(est_spend):
            est_spend = 0.0
        remain = max(0.0, min(face, base_remain - est_spend))
        if not math.isfinite(remain):
            remain = 0.0
        spent = max(0.0, face - remain) if balance_available else 0.0
        pct = (remain / face * 100.0) if face > 0 else 0.0
        if not math.isfinite(pct):
            pct = 0.0

        total_in = totals["input"] + totals["cached"] + totals["cache_write"]
        hit_rate = (totals["cached"] / total_in * 100.0) if total_in else 0.0

        saved = totals.get("cache_saved", 0.0)

        today_key = dt.strftime("%Y-%m-%d")
        today = daily.get(today_key, {"cost": 0.0, "tokens": 0, "count": 0})

        # 最近 12 小时的每小时花费（补齐空洞）
        hour_now = int(time.time() // 3600)
        series = []
        for i in range(11, -1, -1):
            hk = hour_now - i
            series.append((hk, float(hourly.get(hk, {}).get("cost", 0.0))))
        peak_hour = max((c for _, c in series), default=0.0)

        # 燃烧速率：优先取近 3 小时，否则用今日均值
        cost_3h = sum(cost for ts, cost in cost_events if since <= ts <= wall)
        burn = cost_3h / 3.0 if cost_3h > 0 else 0.0
        if burn <= 0 and today["cost"] > 0:
            hours_today = max(1.0, (dt - dt.replace(hour=0, minute=0, second=0, microsecond=0)).total_seconds() / 3600)
            burn = today["cost"] / hours_today
        burn *= factor
        days_left = (remain / (burn * 24.0)) if balance_available and burn > 0.004 else None

        ext = self._external_drop(dt, cost_events, factor)

        last_model = (recent[0].get("model") if recent else "") or ""
        last_ts = _num(recent[0].get("t")) if recent else 0
        mode = "忙时" if peak else "闲时"

        next_mode = "闲时" if peak else "忙时"
        switch_in = max(0, int((w_end - dt).total_seconds()))
        window_total = max(1.0, (w_end - w_start).total_seconds())
        window_frac = max(0.0, min(1.0, (w_end - dt).total_seconds() / window_total))

        status = "unknown"
        if balance_available:
            # BSS 每 60 秒同步一次，新鲜窗口按 2.5 分钟算；浏览器推送按 75 秒算
            fresh = 150 if kind == "bss" else 75
            status = "live" if bts and (wall - bts) < fresh else "stale"
            if kind == "bss" and coupon_sync_error:
                status = "stale"

        return {
            "now": dt,
            "clock": dt.strftime("%H:%M:%S"),
            "date": dt.strftime("%m-%d"),
            "peak": peak,
            "mode": mode,
            "next_mode": next_mode,
            "switch_in": switch_in,
            "switch_at": w_end.strftime("%H:%M"),
            "window_frac": window_frac,
            "face": face,
            "remain": remain,
            "balance_available": balance_available,
            "spent": spent,
            "synced_remain": (base_remain if source == "real" else None),
            "estimated_spend": est_spend,
            "pct_remain": pct,
            "pct_used": max(0.0, min(100.0, 100.0 - pct)),
            "status": status,
            "balance_age": (time.time() - bts) if bts else None,
            "balance_ts": bts or None,
            "balance_note": seed_note,
            "balance_kind": kind,
            "balance_sync_error": bool(kind == "bss" and coupon_sync_error),
            "balance_sync_issue": coupon_sync_issue if kind == "bss" else "",
            "external_drop": ext,
            "totals": totals,
            "unpriced_count": int(totals.get("unpriced_count", 0) or 0),
            "unpriced_tokens": int(totals.get("unpriced_tokens", 0) or 0),
            "per_model": per_model,
            "recent": recent,
            "last_model": last_model,
            "last_model_at": last_ts,
            "hit_rate": hit_rate,
            "cache_saved": saved,
            "today": today,
            "hourly": series,
            "peak_hour": peak_hour,
            "burn_per_hour": burn,
            "days_left": days_left,
            "seq": seq,
            "last_request_at": last_req,
            "total_tokens": total_in + totals["output"],
            "input_tokens": total_in,
            "output_tokens": totals["output"],
            "pricing_note": "缓存命中按输入价 10%，缓存写入 125%",
            "app": APP_NAME,
            "version": APP_VERSION,
        }

    def _external_drop(self, dt, recent, factor, window_sec=180) -> float:
        with self.lock:
            hist = list(self.balance_hist)
        if len(hist) < 2:
            return 0.0
        t_now, r_now = hist[-1]
        if time.time() - t_now > 600:
            return 0.0
        base = hist[0]
        for ts, rv in hist:
            if t_now - ts <= window_sec:
                base = (ts, rv)
                break
        drop = base[1] - r_now
        proxy_cost = sum(cost * factor for ts, cost in recent if base[0] <= ts <= t_now)
        return max(0.0, drop - proxy_cost)

# ---------------------------------------------------------------- 本地代理
def public_snapshot(snap: dict) -> dict:
    """去掉不可 JSON 序列化的字段，给网页面板 / 其它客户端用。"""
    out = {}
    for k, v in snap.items():
        if k == "now":
            out[k] = v.strftime("%Y-%m-%d %H:%M:%S")
        elif k == "hourly":
            out[k] = [[h, round(c, 6)] for h, c in v]
        else:
            out[k] = v
    return out


class QuietServer(ThreadingHTTPServer):
    daemon_threads = True
    # 关键：Windows 上 SO_REUSEADDR 会把「端口已被占用」变成静默抢占，
    # 关掉它才能可靠地做单实例判断。
    allow_reuse_address = False

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, TimeoutError)):
            return
        log("server error: " + "".join(traceback.format_exception_only(type(exc), exc)).strip())


_DROP_REQ_HEADERS = {
    "host", "content-length", "connection", "accept-encoding", "cookie", "cookie2",
    "origin", "referer", "proxy-authorization", "proxy-connection", "keep-alive",
    "te", "trailer", "upgrade", "upgrade-insecure-requests", "transfer-encoding",
    "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "x-real-ip",
    "forwarded", "via",
}
_SAFE_RESP_HEADERS = (
    "Cache-Control", "Retry-After", "Request-Id", "X-Request-Id",
    "anthropic-ratelimit-requests-remaining",
    "anthropic-ratelimit-tokens-remaining",
    "anthropic-ratelimit-input-tokens-remaining",
    "anthropic-ratelimit-output-tokens-remaining",
)


def _header_safe(value) -> bool:
    return isinstance(value, str) and "\r" not in value and "\n" not in value


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    """同主机重定向可以跟随；跨主机/跨协议一律拒绝，避免 API Key 被带走。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old = urllib.parse.urlsplit(req.full_url)
        new = urllib.parse.urlsplit(urllib.parse.urljoin(req.full_url, newurl))
        if (old.scheme.lower(), old.netloc.lower()) != (new.scheme.lower(), new.netloc.lower()):
            raise urllib.error.HTTPError(
                req.full_url, code, "cross-host redirect blocked", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_UPSTREAM_OPENER = urllib.request.build_opener(SafeRedirect())


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = APP_NAME
    timeout = 120
    store: Store = None  # 启动时注入

    # ---------- 安全边界 ----------
    def version_string(self):
        return APP_NAME

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").strip().lower()
        if not host:
            return True
        try:
            port = int(self.server.server_address[1])
        except Exception:
            port = LISTEN_PORT
        return host in {f"127.0.0.1:{port}", f"localhost:{port}",
                        "127.0.0.1", "localhost"}

    def _token_ok(self) -> bool:
        want = str(getattr(self.server, "api_token", "") or "")
        got = str(self.headers.get("X-HUD-Token") or "")
        if not want:
            return False
        try:
            return hmac.compare_digest(got.encode("utf-8", "ignore"),
                                       want.encode("utf-8", "ignore"))
        except Exception:
            return False

    def _origin(self) -> str:
        origin = (self.headers.get("Origin") or "").strip()
        return origin[:-1] if origin.endswith("/") else origin

    def _is_json(self) -> bool:
        ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0]
        return ctype.strip().lower() == "application/json"

    def _route(self) -> str:
        return self.path.split("?", 1)[0]

    def reject(self, status: int, error: str):
        self.close_connection = True
        return self.send_json({"ok": False, "error": error}, status)

    # ---------- 基础 ----------
    def log_message(self, fmt, *args):
        pass  # 静默；异常统一进 .hud.log

    def send_json(self, obj, status=200, cors_origin=None):
        try:
            data = json.dumps(obj, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError):
            log("send_json rejected non-finite/non-serializable payload")
            status = 500
            data = json.dumps({"ok": False, "error": "internal state is not serializable"},
                              ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if cors_origin:
            self.send_header("Access-Control-Allow-Origin", cors_origin)
            self.send_header("Vary", "Origin")
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(data)
        except OSError:
            pass

    def send_html(self, html):
        nonce = secrets.token_hex(16)
        token = str(getattr(self.server, "api_token", "") or "")
        html = html.replace("__CSP_NONCE__", nonce).replace("__HUD_TOKEN__", token)
        data = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; img-src 'self' data:; "
                         "style-src 'unsafe-inline'; "
                         f"script-src 'nonce-{nonce}'; connect-src 'self'; "
                         "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        try:
            self.wfile.write(data)
        except OSError:
            pass

    def do_OPTIONS(self):
        if not self._host_ok():
            return self.reject(403, "forbidden host")
        origin = self._origin()
        self.send_response(204)
        if origin in PUSH_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "content-type, x-hud-token")
            self.send_header("Access-Control-Allow-Private-Network", "true")
            self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ---------- 路由 ----------
    def do_GET(self):
        if not self._host_ok():
            return self.reject(403, "forbidden host")
        route = self._route()
        if route in ("/", "/index.html"):
            return self.send_html(DASHBOARD)
        if route == "/api/stats":
            return self.send_json(public_snapshot(self.store.snapshot()))
        if route == "/api/plugin/balance":
            # 给 dsh-cost-meter 的「自定义 Provider 余额」用：extract.remaining 取 remain
            s = self.store.snapshot()
            remain = round(float(s["remain"]), 4)
            face = round(float(s["face"]), 2)
            known = bool(s.get("balance_available"))
            return self.send_json({
                "ok": known, "label": f"百炼 {face:g} 元券", "unit": "CNY",
                "remaining": remain if known else None, "remain": remain if known else None, "face": face,
                "used": round(face - remain, 4) if known else None,
                "status": s.get("status"), "balance_ts": s.get("balance_ts"),
                "balance_age": s.get("balance_age"),
                "synced_remaining": (None if s.get("synced_remain") is None
                                     else round(float(s["synced_remain"]), 4)),
                "estimated_spend": round(float(s.get("estimated_spend") or 0), 4),
                "unpriced_count": int(s.get("unpriced_count") or 0),
            })
        if route == "/api/ping":
            return self.send_json({"ok": True, "app": APP_NAME, "version": APP_VERSION,
                                   "instance_id": getattr(self.server, "instance_id", ""),
                                   "data_id": getattr(self.server, "data_id", "")})
        if route == "/api/hud/status":
            if not self._token_ok():
                return self.reject(403, "authentication required")
            return self.send_json(dict(getattr(self.server, "hud_state", {})))
        if route == "/api/credentials":
            if not self._token_ok():
                return self.reject(403, "authentication required")
            sync = getattr(self.server, "coupon_sync", None)
            if not sync:
                return self.reject(503, "余额同步服务尚未就绪")
            return self.send_json(sync.credential_status())
        if route == "/api/coupon-binding":
            origin = self._origin()
            if origin not in PUSH_ORIGINS and not self._token_ok():
                return self.reject(403, "authentication required")
            return self.send_json({"ok": True, **self.store.coupon_identity()},
                                  cors_origin=origin if origin in PUSH_ORIGINS else None)
        if route == "/api/debug":
            if not self._token_ok():
                return self.reject(403, "authentication required")
            fn = getattr(self.server, "debug_state", None)
            return self.send_json(fn() if fn else {})
        if route.startswith("/api/"):
            return self.send_json({"ok": False, "error": "not found"}, 404)
        return self.proxy(b"")


    def do_POST(self):
        if not self._host_ok():
            return self.reject(403, "forbidden host")
        if self.headers.get("Transfer-Encoding"):
            return self.reject(400, "chunked request body not supported")
        raw_len = self.headers.get("Content-Length")
        if raw_len in (None, ""):
            length = 0
        else:
            try:
                length = int(raw_len)
            except (TypeError, ValueError):
                return self.reject(400, "invalid content-length")
        if length < 0:
            return self.reject(400, "invalid content-length")
        route = self._route()
        is_api = route.startswith("/api/")
        if length > (MAX_API_BODY if is_api else MAX_PROXY_BODY):
            return self.reject(413, "request body too large")

        # 写接口在读取请求体之前先鉴权，避免未授权的大请求占内存。
        origin = self._origin()
        if route == "/api/balance":
            if origin not in PUSH_ORIGINS and not self._token_ok():
                return self.reject(403, "authentication required")
            if not self._is_json():
                return self.reject(415, "application/json required")
        elif route in ("/api/calibrate", "/api/credentials"):
            if not self._token_ok():
                return self.reject(403, "authentication required")
            if not self._is_json():
                return self.reject(415, "application/json required")
        elif route in ("/api/hud/show", "/api/hud/hide", "/api/hud/toggle",
                       "/api/hud/quit", "/api/hud/panel", "/api/debug"):
            if not self._token_ok():
                return self.reject(403, "authentication required")
        elif is_api:
            return self.reject(404, "not found")

        try:
            body = self.rfile.read(length) if length else b""
        except (ConnectionResetError, TimeoutError):
            return
        if length and len(body) != length:
            return self.reject(400, "incomplete request body")

        if route == "/api/balance":
            cors_origin = origin if origin in PUSH_ORIGINS else None
            try:
                data = json.loads(body.decode("utf-8") or "{}")
                if not isinstance(data, dict):
                    raise ValueError("body must be an object")
                self.store.push_balance(data["remain"], "real", kind="push",
                                        coupon_no=data.get("coupon"),
                                        coupon_id=data.get("coupon_id"),
                                        nominal=data.get("nominal"))
                remain = float(data["remain"])
                return self.send_json({"ok": True, "remain": remain}, 200,
                                      cors_origin=cors_origin)
            except CouponBindingError as exc:
                return self.send_json({"ok": False, "error": str(exc)}, 409,
                                      cors_origin=cors_origin)
            except (KeyError, TypeError, ValueError) as exc:
                return self.send_json({"ok": False, "error": str(exc)}, 400,
                                      cors_origin=cors_origin)
        if route == "/api/calibrate":
            try:
                data = json.loads(body.decode("utf-8") or "{}")
                if not isinstance(data, dict):
                    raise ValueError("body must be an object")
                factor = self.store.calibrate(data["remain"], face=data.get("face"))
                remain = float(data["remain"])
                return self.send_json({"ok": True, "base_remain": remain,
                                       "cost_factor": factor})
            except (KeyError, TypeError, ValueError) as exc:
                return self.send_json({"ok": False, "error": str(exc)}, 400)
        if route == "/api/credentials":
            sync = getattr(self.server, "coupon_sync", None)
            if not sync:
                return self.reject(503, "余额同步服务尚未就绪")
            try:
                data = json.loads(body.decode("utf-8"))
                return self.send_json(sync.configure_credentials(data))
            except (ValueError, UnicodeError) as exc:
                return self.send_json({"ok": False, "error": str(exc)}, 400)
            except OSError:
                return self.send_json({"ok": False, "error": "保存失败，请检查数据目录的写入权限"}, 500)
        if route.startswith("/api/hud/"):
            command = route.rsplit("/", 1)[-1]
            control = getattr(self.server, "hud_control", None)
            if control:
                control(command)
            elif command == "show":
                self.server.request_show()  # type: ignore[attr-defined]
            return self.send_json({"ok": True})
        if route == "/api/debug":
            fn = getattr(self.server, "debug_state", None)
            return self.send_json(fn() if fn else {})
        return self.proxy(body)


    # ---------- 反向代理 ----------
    def send_proxy_error(self, status: int, message: str):
        msg = json.dumps({"error": {"type": "proxy_error", "message": message}},
                         ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(msg)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(msg)
        except OSError:
            pass

    def proxy(self, body: bytes):
        url = UPSTREAM.rstrip("/") + self.path
        headers = {}
        for key, val in self.headers.items():
            lk = key.lower()
            if lk in _DROP_REQ_HEADERS or lk.startswith("sec-"):
                continue
            headers[key] = val
        headers["Accept-Encoding"] = "identity"
        peak = is_peak()
        req = urllib.request.Request(url, data=body if body else None,
                                     headers=headers, method=self.command)
        try:
            upstream = _UPSTREAM_OPENER.open(req, timeout=900)
        except urllib.error.HTTPError as exc:
            if 300 <= exc.code < 400:
                try:
                    exc.close()
                except Exception:
                    pass
                return self.send_proxy_error(502, "upstream cross-host redirect blocked")
            try:
                payload = exc.read(MAX_PARSE_BYTES)
            except Exception:
                payload = b""
            try:
                exc.close()
            except Exception:
                pass
            ctype = (exc.headers.get("Content-Type", "application/json")
                     if exc.headers else "application/json")
            if not _header_safe(ctype):
                ctype = "application/json"
            self.send_response(exc.code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            try:
                self.wfile.write(payload)
            except OSError:
                pass
            return
        except Exception as exc:
            return self.send_proxy_error(502, str(exc))

        ctype = upstream.headers.get("Content-Type", "") or "application/json"
        is_stream = "event-stream" in ctype.lower()
        raw_cl = upstream.headers.get("Content-Length")
        try:
            clen = int(raw_cl) if raw_cl else 0
        except (TypeError, ValueError):
            clen = 0
        if clen < 0:
            clen = 0

        self.send_response(upstream.status)
        self.send_header("Content-Type", ctype if _header_safe(ctype) else "application/json")
        for h in _SAFE_RESP_HEADERS:
            val = upstream.headers.get(h)
            if val and _header_safe(val):
                self.send_header(h, val)
        self.send_header("X-Content-Type-Options", "nosniff")
        if clen:
            self.send_header("Content-Length", str(clen))
        else:
            self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        parser = SseUsageParser() if is_stream else None
        raw = bytearray() if not is_stream else None
        total = 0
        client_gone = False
        abort_started = 0.0
        read_failed = False
        # HTTPResponse.read(n) 会等满 n 字节或上游结束；SSE 必须及时转发小事件。
        read_chunk = getattr(upstream, "read1", upstream.read) if is_stream else upstream.read
        try:
            while True:
                try:
                    chunk = read_chunk(8192)
                except Exception as exc:
                    read_failed = True
                    self.close_connection = True
                    log(f"upstream read failed: {exc!r}")
                    break
                if not chunk:
                    break
                total += len(chunk)
                if parser is not None:
                    parser.feed(chunk)
                elif raw is not None and len(raw) < MAX_PARSE_BYTES:
                    raw.extend(chunk[: MAX_PARSE_BYTES - len(raw)])
                if not client_gone:
                    try:
                        if clen:
                            self.wfile.write(chunk)
                        else:
                            self.wfile.write(b"%X\r\n%s\r\n" % (len(chunk), chunk))
                        self.wfile.flush()
                    except OSError:
                        # 客户端取消很常见：继续把上游读完，拿到最后 usage 再记账。
                        client_gone = True
                        abort_started = time.time()
                        self.close_connection = True
                if client_gone and (time.time() - abort_started > ABORT_GRACE_SEC
                                    or total > MAX_PROXY_BODY):
                    break
        finally:
            try:
                upstream.close()
            except Exception:
                pass

        if not client_gone and not clen and not read_failed:
            try:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except Exception:
                pass

        usage = None
        try:
            if parser is not None:
                parser.finish()
                usage = parser.result()
            elif raw:
                obj = json.loads(bytes(raw).decode("utf-8", "ignore"))
                usage = parse_usage_anthropic(obj) or parse_usage_openai(obj)
                if not usage and isinstance(obj, dict) and obj.get("type") == "message":
                    usage = parse_usage_anthropic(obj)
        except Exception:
            usage = None

        if usage:
            model = usage[4]
            if not model and body:
                try:
                    model = json.loads(body.decode("utf-8", "ignore")).get("model")
                except Exception:
                    pass
            try:
                self.store.record(model, *usage[:4], peak, self._route())
            except Exception as exc:
                log(f"record failed: {exc!r}")


class ProxyService:
    """把 HTTP 服务跑在后台线程里；UI 只读 Store。"""

    def __init__(self, store: Store, port: int = LISTEN_PORT, on_show=None,
                 api_token: str = ""):
        self.store = store
        self.port = port
        self.on_show = on_show
        self.api_token = api_token
        self.server: QuietServer | None = None
        self.thread: threading.Thread | None = None

    def start(self) -> bool:
        Handler.store = self.store
        srv = QuietServer((LISTEN_HOST, self.port), Handler)
        srv.request_show = self.on_show or (lambda: None)  # type: ignore[attr-defined]
        srv.api_token = self.api_token  # type: ignore[attr-defined]
        self.server = srv
        self.port = int(srv.server_address[1])
        self.thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.25},
                                       name="bailian-proxy", daemon=True)
        self.thread.start()
        log(f"proxy listening on {LISTEN_HOST}:{self.port}")
        return True


    def stop(self):
        try:
            if self.server:
                self.server.shutdown()
                self.server.server_close()
        except Exception:
            pass


def probe_proxy(port: int = LISTEN_PORT, timeout: float = 0.8) -> dict | None:
    """探测端口上是不是另一个本程序实例。"""
    try:
        d = local_json(port, "/api/ping", timeout=timeout)
        if isinstance(d, dict) and d.get("app") == APP_NAME:
            return d
    except Exception:
        pass
    return None



# ---------------------------------------------------------------- 视觉：字体与主题
try:
    from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageStat
except Exception as exc:  # pragma: no cover
    Image = None
    _PIL_IMPORT_ERROR = exc

FONT_DIR = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
_FONT_CANDIDATES = {
    "cjk": ["msyh.ttc", "msyhl.ttc", "simhei.ttf"],
    "cjk_b": ["msyhbd.ttc", "msyh.ttc", "simhei.ttf"],
    "num": ["seguisb.ttf", "segoeuib.ttf", "arialbd.ttf"],
    "num_r": ["segoeui.ttf", "arial.ttf"],
}


def _resolve_font(key: str) -> str | None:
    for name in _FONT_CANDIDATES.get(key, []):
        p = FONT_DIR / name
        if p.exists():
            return str(p)
    return None


class FontBook:
    def __init__(self, ss: float):
        self.ss = ss
        self._cache: dict[tuple[str, int], object] = {}
        self.paths = {k: _resolve_font(k) for k in _FONT_CANDIDATES}

    def get(self, key: str, px: float):
        size = max(6, int(round(px * self.ss)))
        ck = (key, size)
        f = self._cache.get(ck)
        if f is None:
            path = self.paths.get(key) or self.paths.get("cjk")
            try:
                f = ImageFont.truetype(path, size)
            except Exception:
                f = ImageFont.load_default()
            self._cache[ck] = f
        return f


LIGHT = {
    "tint": (250, 251, 253, 190),
    "tint_top": (255, 255, 255, 46),
    "border": (255, 255, 255, 175),
    "border_lo": (17, 21, 28, 26),
    "text": (22, 25, 31),
    "sub": (82, 90, 103),
    "faint": (100, 108, 122),
    "accent": (33, 93, 235),
    "accent2": (92, 156, 255),
    "live": (8, 124, 66),
    "warn": (150, 90, 2),
    "danger": (186, 32, 32),
    "track": (18, 22, 30, 24),
    "hair": (18, 22, 30, 20),
    "hover": (18, 22, 30, 14),
    "shadow": (12, 16, 24, 84),
}

DARK = {
    "tint": (33, 36, 44, 200),
    "tint_top": (255, 255, 255, 30),
    "border": (255, 255, 255, 46),
    "border_lo": (0, 0, 0, 70),
    "text": (238, 241, 246),
    "sub": (186, 193, 205),
    "faint": (150, 158, 172),
    "accent": (108, 158, 255),
    "accent2": (176, 210, 255),
    "live": (74, 216, 136),
    "warn": (245, 183, 68),
    "danger": (255, 120, 114),
    "track": (255, 255, 255, 30),
    "hair": (255, 255, 255, 26),
    "hover": (255, 255, 255, 24),
    "shadow": (0, 0, 0, 150),
}

W, H = 320, 124
H_EXP = 204
H_MENU = 248

# 跟随目标：off=始终显示，其余按「目标进程是否在运行」显隐
FOLLOW_ORDER = ["off", "zcode", "dsh", "both"]
FOLLOW_LABEL = {"off": "关（常显）", "zcode": "ZCode",
                "dsh": "DeepSeek Harness", "both": "两者"}
FOLLOW_MATCHERS = {"zcode": ["zcode"], "dsh": ["deepseek harness"],
                   "both": ["zcode", "deepseek harness"]}


def normalize_follow(v) -> str:
    v = str(v or "both").lower()
    if v == "auto":
        return "both"
    if v == "always":
        return "off"
    return v if v in FOLLOW_LABEL else "both"
MAX_H = H_MENU
PAD = 16
SS = 2  # 超采样倍数
# 显隐过渡时长（秒）：按时间缓动，和帧率无关
FADE_IN = 0.12
FADE_OUT = 0.09
# 明暗主题过渡时长（秒）。D+D 切窗口时整张卡换色，用 smoothstep 慢慢推，
# 端点两张卡各自缓存，中间每帧只做一次交叉淡入（见 Renderer.render）。
DARK_DUR = 0.24
# 跟随显隐：正常情况下由 WinEvent 钩子即时通知，这个间隔只是钩子装不上时的安全网
VIS_POLL = 0.1
# 收起防抖：目标窗口还在、只是焦点被别的窗口抢走时，等这一下确认不是闪跳。
# 目标窗口已经销毁/全部最小化时不用等——那种情况没有歧义。
HIDE_SETTLE = 0.12
# 空闲态的空转节奏（毫秒）。Windows 上 Tk 定时器落在 15.6ms 刻度上，
# 请求 8 会并到 15.6（≈64Hz），请求 16/24 都会并到 31.2（≈32Hz）。
# 实测空转 tick 只要 50~160µs，31Hz 的代价是单核 0.4% 以内，换的是
# "事件一到就能在 31ms 内反应"，比原来 80ms（还落在 93.6ms）划算得多。
IDLE_TICK = 16
# 每秒一行"现在为什么还显示"的现场快照（只在设了 HUD_VIS_LOG=1 的进程里开）。
# 排查"关掉 agent 卡片不走"这类体感问题需要它：日志里能直接看到当时前台是谁。
VIS_LOG = bool(os.environ.get("HUD_VIS_LOG"))


def mix(a, b, t):
    t = max(0.0, min(1.0, t))
    if isinstance(a, (int, float)):
        return a + (b - a) * t
    return tuple(int(round(x + (y - x) * t)) for x, y in zip(a, b))


def blend(fg, bg, alpha):
    return tuple(int(round(x * alpha + y * (1 - alpha))) for x, y in zip(fg, bg))


def theme_at(mix_val: float) -> dict:
    out = {}
    for k, lv in LIGHT.items():
        dv = DARK[k]
        out[k] = mix(lv, dv, mix_val)
    return out


class _InkDraw:
    """文字层的画笔：命中缓存时跳过字形渲染，只保留装饰。"""

    __slots__ = ("draw", "enabled")

    def __init__(self, draw, enabled=True):
        self.draw = draw
        self.enabled = enabled

    def text(self, *a, **kw):
        if self.enabled:
            return self.draw.text(*a, **kw)
        return None

    def textlength(self, *a, **kw):
        return self.draw.textlength(*a, **kw)

    def __getattr__(self, name):
        return getattr(self.draw, name)


class Renderer:
    """把一份快照画成一张带 alpha 的 RGBA 图。纯函数式，便于离屏预览。"""

    def __init__(self, scale: float = 1.0, ss: int = SS):
        self.scale = max(1.0, float(scale))
        self.ss = ss
        # 只在卡片范围内超采样；1.3x 左右足够，纹理边缘靠重采样收尾
        self.S = max(1.7, min(2.5, self.scale * 1.3))
        self.fonts = FontBook(self.S)
        self._glass_cache: dict = {}
        self._shadow_cache: dict = {}
        # 满高整卡（玻璃+装饰+文字）：内容按固定坐标排版、与卡片当前高度无关，
        # 所以只在数据/主题/背景变化时重建，展开动画里每帧只做裁切。
        self._card_cache: dict = {}
        self._canvas_cache: dict = {}
        self._botmask_cache: dict = {}
        self._ramp_cache: dict = {}

    # ---- 小工具 ----
    def u(self, v):
        return int(round(v * self.S))

    def f(self, key, px):
        return self.fonts.get(key, px)

    def _rrect(self, draw, box, radius, **kw):
        draw.rounded_rectangle([self.u(box[0]), self.u(box[1]), self.u(box[2]), self.u(box[3])],
                               radius=self.u(radius), **kw)

    def _mask(self, size, radius):
        m = Image.new("L", size, 0)
        d = ImageDraw.Draw(m)
        d.rounded_rectangle([0, 0, size[0] - 1, size[1] - 1],
                            radius=int(round(radius * self.S)), fill=255)
        return m

    def _ramp(self, w, h):
        """竖直线性渐变（上 255 → 下 0），按尺寸缓存。"""
        key = (w, h)
        hit = self._ramp_cache.get(key)
        if hit is None:
            g = Image.new("L", (2, 2))
            g.putpixel((0, 0), 255)
            g.putpixel((1, 0), 255)
            g.putpixel((0, 1), 0)
            g.putpixel((1, 1), 0)
            hit = g.resize((w, h), Image.Resampling.BILINEAR)
            if len(self._ramp_cache) > 8:
                self._ramp_cache.clear()
            self._ramp_cache[key] = hit
        return hit

    def _glass(self, backdrop, size, radius, th, key):
        hit = self._glass_cache.get(key)
        if hit is not None:
            return hit
        w, h = size
        # 背景降采样模糊：比全尺寸高斯快一个数量级，观感一致
        if backdrop is None:
            bd = Image.new("RGBA", (32, 32), (232, 236, 243, 255))
            bd = bd.resize((w, h))
        else:
            small = backdrop.convert("RGBA").resize((max(8, w // 4), max(8, h // 4)),
                                                    Image.Resampling.BILINEAR)
            small = small.filter(ImageFilter.GaussianBlur(max(1.2, 3.0 * self.S / 2)))
            bd = small.resize((w, h), Image.Resampling.BILINEAR)
        bd = bd.convert("RGB")
        # 去饱和 + 轻微雾化，让玻璃不抢背景的戏
        g = bd.convert("L").convert("RGB")
        bd = Image.blend(bd, g, 0.16)
        dark_surface = th["tint"][0] < 110
        wash = (30, 33, 41) if dark_surface else (255, 255, 255)
        bd = Image.blend(bd, Image.new("RGB", (w, h), wash), 0.26 if dark_surface else 0.12)
        base = bd.convert("RGBA")
        tint = Image.new("RGBA", (w, h), tuple(int(v) for v in th["tint"]))
        base = Image.alpha_composite(base, tint)
        # 顶部高光：2px 渐变图拉伸，避免逐像素循环。
        # 渐变长度固定在"折叠高度"上：卡片长高时高光不跟着拉长，否则同一张卡
        # 在不同高度亮度不同，展开动画里会看出"呼吸"。
        ramp_h = max(2, min(h, int(round(H * self.S))))
        grad = Image.new("L", (w, h), 0)
        grad.paste(self._ramp(w, ramp_h), (0, 0))
        top = Image.new("RGBA", (w, h), tuple(int(v) for v in th["tint_top"]))
        top.putalpha(ImageChops.multiply(top.getchannel("A"), grad))
        base = Image.alpha_composite(base, top)
        base.putalpha(self._mask(size, radius))
        if len(self._glass_cache) > 6:
            self._glass_cache.clear()
        self._glass_cache[key] = base
        return base

    def _shadow(self, pw, ph, th, tag):
        key = (pw, ph, tag)
        hit = self._shadow_cache.get(key)
        if hit is not None:
            return hit
        m = max(6, int(round(13 * self.scale)))
        w, h = pw + 2 * m, ph + 2 * m
        small = Image.new("L", (max(2, w // 4), max(2, h // 4)), 0)
        sd = ImageDraw.Draw(small)
        off = m / 4.0
        sd.rounded_rectangle([off, off, off + pw / 4.0, off + ph / 4.0],
                             radius=max(2, 18 * self.scale / 4.0), fill=255)
        small = small.filter(ImageFilter.GaussianBlur(max(1.0, 4.0 * self.scale / 4.0)))
        mask = small.resize((w, h), Image.Resampling.BILINEAR)
        a = int(th["shadow"][3])
        mask = mask.point(lambda v: v * a // 255)
        sh = Image.new("RGBA", (w, h), tuple(int(v) for v in th["shadow"][:3]) + (0,))
        sh.putalpha(mask)
        if len(self._shadow_cache) > 6:
            self._shadow_cache.clear()
        self._shadow_cache[key] = (sh, m)
        return sh, m

    # ---- 主入口 ----
    @staticmethod
    def _detail_a(ui) -> float:
        """展开详情的整体不透明度：0=完全不画，1=全亮。"""
        a = ui.get("detail_a")
        if a is None:
            a = (float(ui.get("expand", 0.0) or 0.0) - 0.55) / 0.45
        return max(0.0, min(1.0, float(a)))

    def _botmask(self, cw, r2):
        """底部两个圆角用的蒙版带（只随卡片宽度变化，缓存住）。"""
        key = (cw, r2)
        m = self._botmask_cache.get(key)
        if m is None:
            tall = self._mask((cw, r2 * 3), 16)
            m = tall.crop((0, r2 * 2, cw, r2 * 3))
            if len(self._botmask_cache) > 12:
                self._botmask_cache.clear()
            self._botmask_cache[key] = m
        return m

    @staticmethod
    def _surface(glass, y0, y1, cw):
        band = glass.crop((0, max(0, y0), cw, max(1, y1)))
        if band.width < 4 or band.height < 4:
            band = glass
        return tuple(int(v) for v in ImageStat.Stat(
            band.resize((8, 8)).convert("RGB")).mean)

    def _card_full(self, snap, ui, backdrop, th, cw, ch_max, tag, card_key):
        """画一张满高的整卡（玻璃 + 装饰 + 文字），供裁切复用。

        tag 是"纯主题"标记（"L" 亮 / "D" 暗）：只有两个端点主题进缓存，
        中间过渡交给 render() 做两张端点的交叉淡入，因此过渡期间不再
        每个灰阶都重砌一遍玻璃和文字（那正是"一帧一帧"的来源）。
        """
        if card_key is not None:
            hit = self._card_cache.get(card_key)
            if hit is not None:
                return hit
        op = float(ui.get("opacity", 1.0))
        glass_key = (cw, ch_max, tag, int(round(op * 20)),
                     ui.get("backdrop_seq", 0), 16)
        glass = self._glass(backdrop, (cw, ch_max), 16, th, glass_key)
        # 文字颜色取自卡片下部的背景均值。取样区固定在"折叠状态"的那一块，
        # 这样同一主题/背景的整卡能一直复用，不会因为高度变化而每帧换色。
        ch_col = min(ch_max, int(round(H * self.S)))
        surface = self._surface(glass, int(ch_col * 0.35), ch_col, cw)
        ink = Image.new("RGBA", (cw, ch_max), (0, 0, 0, 0))
        deco = Image.new("RGBA", (cw, ch_max), (0, 0, 0, 0))
        if ui.get("menu"):
            self._draw_menu(ink, deco, th, surface, ui, float(H_MENU))
        else:
            self._draw_stats(ink, deco, th, surface, snap, ui, float(MAX_H))
        card = glass.copy()
        card.alpha_composite(deco)
        card.alpha_composite(ink)
        if card_key is not None:
            if len(self._card_cache) > 8:
                self._card_cache.clear()
            self._card_cache[card_key] = card
        return card

    def render(self, snap: dict, ui: dict, backdrop, size_units=(W, MAX_H)) -> "Image.Image":
        """明暗过渡：两张"纯主题"的成品画布做交叉淡入，中间每一帧只付一次混合的钱。

        原来的做法是按 dark 量化成 21 个灰阶，每跨一档就把玻璃 + 文字层整个重画
        一遍（14ms 以上），于是过渡看起来就是一格一格跳（还掉帧）。现在两端
        各缓存一张，过渡期每帧只 blend 一次（1~2ms），颜色是连续的。
        """
        dark = float(ui.get("dark", 0.0) or 0.0)
        if dark != dark:            # NaN 兜底
            dark = 0.0
        if dark <= 0.001:
            return self._canvas_at(snap, ui, backdrop, 0.0, "L")
        if dark >= 0.999:
            return self._canvas_at(snap, ui, backdrop, 1.0, "D")
        light = self._canvas_at(snap, ui, backdrop, 0.0, "L")
        night = self._canvas_at(snap, ui, backdrop, 1.0, "D")
        if light.size != night.size:
            return night if dark > 0.5 else light
        return Image.blend(light, night, dark)

    def _canvas_at(self, snap, ui, backdrop, dark_value, tag):
        """取某个"纯主题"端点下的整张窗口画布（含阴影），带缓存。

        过渡时每帧要拿两张端点，如果每帧都把玻璃、文字、阴影、缩放重做一遍，
        光准备工作就 6ms 起步；缓存住之后每帧只剩下一次混合。
        """
        key = (tag, self.scale, ui.get("ink_sig"),
               round(float(ui.get("content_h", H)), 2),
               round(float(ui.get("card_y", 0.0) or 0.0), 2),
               round(float(ui.get("opacity", 1.0)), 2),
               ui.get("backdrop_seq", 0),
               1 if ui.get("menu") else 0,
               int(round(self._detail_a(ui) * 3)))
        cacheable = ui.get("ink_sig") is not None
        if cacheable:
            hit = self._canvas_cache.get(key)
            if hit is not None:
                return hit
        canvas = self._render_at(snap, ui, backdrop, dark_value, tag)
        if cacheable:
            if len(self._canvas_cache) > 4:
                self._canvas_cache.clear()
            self._canvas_cache[key] = canvas
        return canvas

    def _render_at(self, snap, ui, backdrop, dark_value, tag):
        scale = self.scale
        S = self.S
        content_h = float(ui.get("content_h", H))
        th = theme_at(dark_value)
        op = float(ui.get("opacity", 1.0))
        if op < 0.999:
            th["tint"] = tuple(th["tint"][:3]) + (int(th["tint"][3] * op),)
            th["tint_top"] = tuple(th["tint_top"][:3]) + (int(th["tint_top"][3] * op),)

        cw, ch_max = int(round(W * S)), int(round(MAX_H * S))
        ch = max(8, min(ch_max, int(round(content_h * S))))
        card_y = float(ui.get("card_y", PAD + MAX_H - content_h))
        ink_sig = ui.get("ink_sig")
        # 详情淡入量化成 4 档：只有这几档要重画整卡，其余帧全部走裁切
        detail_a = self._detail_a(ui)
        card_key = None
        if ink_sig is not None:
            card_key = (tag, cw, ch_max, int(round(op * 20)),
                        ui.get("backdrop_seq", 0), ink_sig,
                        1 if ui.get("menu") else 0,
                        int(round(detail_a * 3)), 16)
        card = self._card_full(snap, ui, backdrop, th, cw, ch_max, tag, card_key)

        # 裁到当前高度；满高蒙版的圆角只在 ch_max 的底边，这里把底边重新磨圆
        if ch >= ch_max:
            card = card.copy()
        else:
            card = card.crop((0, 0, cw, ch))
            r2 = min(ch, int(round(16 * S * 2)))
            if r2 > 4:
                bot = card.crop((0, ch - r2, cw, ch))
                bot.putalpha(ImageChops.multiply(bot.getchannel("A"),
                                                 self._botmask(cw, r2)))
                card.paste(bot, (0, ch - r2))

        edge = ImageDraw.Draw(card)
        edge.rounded_rectangle([0, 0, cw - 1, ch - 1], radius=self.u(16),
                               outline=tuple(int(v) for v in th["border"]), width=max(1, self.u(0.9)))
        edge.rounded_rectangle([0, 0, cw - 1, ch - 1], radius=self.u(16),
                               outline=tuple(int(v) for v in th["border_lo"]), width=1)
        edge.line([self.u(18), ch - 1, cw - self.u(18), ch - 1],
                  fill=tuple(int(v) for v in th["border_lo"]), width=1)

        # 缩放到物理像素，再贴进窗口大小的透明画布
        pw, ph = int(round(W * scale)), int(round(content_h * scale))
        if (pw, ph) != (cw, ch):
            card = card.resize((pw, ph), Image.Resampling.BILINEAR)

        win_w = int(round((W + 2 * PAD) * scale))
        win_h = int(round((MAX_H + 2 * PAD) * scale))
        canvas = Image.new("RGBA", (win_w, win_h), (0, 0, 0, 0))
        sh, m = self._shadow(pw, ph, th, tag)
        ox = int(round(PAD * scale))
        oy = int(round(card_y * scale))
        canvas.paste(sh, (ox - m, oy - m + max(1, int(round(3 * scale)))), sh)
        canvas.paste(card, (ox, oy), card)
        return canvas

    # ---- 统计卡片 ----
    def _draw_stats(self, ink, deco, th, surface, snap, ui, content_h, draw_text=True):
        d = _InkDraw(ImageDraw.Draw(ink), draw_text)
        dd = ImageDraw.Draw(deco)
        txt = th["text"]
        sub = th["sub"]
        faint = blend(th["faint"], surface, 1.0)

        status = snap.get("status", "estimate")
        live = th["live"]
        warn = th["warn"]
        danger = th["danger"]
        accent = th["accent"]
        accent2 = th["accent2"]

        # ---------- 顶部：徽标 + 标题 + 状态 + 关闭 ----------
        bx, by = 16.0, 12.0
        badge = self.u(15)
        bx_px, by_px = self.u(bx), self.u(by)
        gb = Image.new("RGBA", (badge, badge), (0, 0, 0, 0))
        gd = ImageDraw.Draw(gb)
        for i in range(badge):
            t = i / max(1, badge - 1)
            gd.line([(0, i), (badge, i)], fill=mix(accent, accent2, t) + (255,))
        gb.putalpha(self._mask((badge, badge), 4.5))
        deco.alpha_composite(gb, (bx_px, by_px))
        d.rounded_rectangle([bx_px, by_px, bx_px + badge - 1, by_px + badge - 1],
                            radius=self.u(4.5), outline=(255, 255, 255, 60), width=max(1, self.u(0.7)))
        d.text((bx_px + badge / 2, by_px + badge / 2 + self.u(0.4)), "¥",
               font=self.f("num", 9.2), fill=(255, 255, 255), anchor="mm")

        face = snap.get("face") or 0
        face_txt = f"{face:.0f}" if abs(face - round(face)) < 0.01 else f"{face:.1f}"
        title = f"百炼 {face_txt} 元券"
        d.text((self.u(bx + 15 + 8), self.u(18.4)), title,
               font=self.f("cjk", 10.6), fill=blend(sub, surface, 1.0), anchor="ls")

        # 状态
        kind = snap.get("balance_kind", "")
        age = fmt_age(snap.get("balance_age"))
        bts = snap.get("balance_ts")
        stamp = ""
        if bts:
            try:
                stamp = local_dt(float(bts)).strftime("%H:%M")
            except (TypeError, ValueError, OSError):
                stamp = ""
        if status == "live":
            if kind == "bss":
                # 同步是固定 60s 一轮，"1m 前"永远显示 1m，没有信息量；
                # 换成时钟更方便对表，也顺便把 BSS 这个缩写从卡片上拿掉。
                dot_c, label = live, (f"已同步 {stamp}" if stamp else "实时")
            elif kind == "manual":
                dot_c, label = live, (f"已校准 {stamp}" if stamp else "实时")
            else:
                dot_c, label = live, "实时"
        elif status == "stale":
            issue = snap.get("balance_sync_issue")
            reason = ({"unavailable": "券不可用", "missing": "未找到券"}.get(issue)
                      or ("同步失败" if snap.get("balance_sync_error") else "已过期"))
            dot_c, label = warn, (reason + (f" · {stamp}" if stamp else ""))
        elif status == "unknown":
            dot_c, label = warn, "待同步"
        else:
            dot_c, label = warn, "估算"
        lw = d.textlength(label, font=self.f("cjk", 9.8))
        sx = self.u(291) - lw
        sy = self.u(18.4)
        if status in ("estimate", "unknown"):
            d.ellipse([sx - self.u(9.5), sy - self.u(6.6), sx - self.u(3.5), sy - self.u(0.6)],
                      outline=dot_c, width=max(1, self.u(1.0)))
        else:
            pulse = ui.get("pulse", 0.0)
            if pulse > 0:
                r = self.u(2.4 + 3.6 * pulse)
                pad = int(r) + max(2, self.u(3))
                box = pad * 2 + 2
                gl = Image.new("RGBA", (box, box), (0, 0, 0, 0))
                gd2 = ImageDraw.Draw(gl)
                gd2.ellipse([pad - r + self.u(1), pad - r + self.u(1),
                             pad + r + self.u(1), pad + r + self.u(1)],
                            fill=dot_c + (int(80 * (1 - pulse)),))
                gl = gl.filter(ImageFilter.GaussianBlur(max(1, self.u(2))))
                deco.alpha_composite(gl, (int(sx - self.u(6.5) - pad), int(sy - self.u(3.6) - pad)))
            d.ellipse([sx - self.u(9.5), sy - self.u(6.6), sx - self.u(3.5), sy - self.u(0.6)],
                      fill=dot_c)
        d.text((sx, sy), label, font=self.f("cjk", 9.8),
               fill=dot_c if status != "estimate" else blend(dot_c, surface, 0.92), anchor="ls")

        # 关闭：对比度要够（旧版 faint*0.55 只有 1.8:1，等于隐形）
        cc = blend(txt if ui.get("hover_close") else sub, surface, 1.0)
        cx, cy = 305.0, 17.4
        if ui.get("hover_close"):
            dd.rounded_rectangle([self.u(cx - 9), self.u(cy - 9), self.u(cx + 9), self.u(cy + 9)],
                                 radius=self.u(9), fill=tuple(int(v) for v in th["hover"]))
        r = 3.4
        d.line([self.u(cx - r), self.u(cy - r), self.u(cx + r), self.u(cy + r)], fill=cc,
               width=max(1, self.u(1.25)))
        d.line([self.u(cx - r), self.u(cy + r), self.u(cx + r), self.u(cy - r)], fill=cc,
               width=max(1, self.u(1.25)))

        # ---------- 余额 ----------
        remain = snap.get("remain", 0.0)
        known = snap.get("balance_available", True)
        money_c = (danger if remain < 30 else (warn if remain < 100 else txt)) if known else txt
        try:
            est_spend = float(snap.get("estimated_spend") or 0)
        except (TypeError, ValueError):
            est_spend = 0.0
        approx = "≈" if est_spend > 0.005 else ""
        money = f"{approx}¥{remain:,.2f}" if known else "--"
        base_y = 58.5
        d.text((self.u(16), self.u(base_y)), money, font=self.f("num", 29.5),
               fill=money_c, anchor="ls")
        mw = d.textlength(money, font=self.f("num", 29.5))
        d.text((self.u(16) + mw + self.u(8), self.u(base_y - 1.0)), f"/ ¥{face_txt}",
               font=self.f("num_r", 10.2), fill=blend(sub, surface, 0.92), anchor="ls")

        # ---------- 右侧：计时环 ----------
        cxr, cyr, rr = 288.0, 45.0, 15.6
        frac = max(0.0, min(1.0, float(snap.get("window_frac", 1.0))))
        ring_c = warn if snap.get("peak") else live
        cx_u, cy_u, r_u = self.u(cxr), self.u(cyr), self.u(rr)
        sw = self.u(3.1)
        dd.arc([cx_u - r_u, cy_u - r_u, cx_u + r_u, cy_u + r_u], 0, 360,
               fill=tuple(int(v) for v in th["track"]), width=sw)
        if frac > 0.004:
            start = -90.0
            end = -90.0 + 360.0 * frac
            # 渐变尾巴：分段绘制
            steps = max(6, min(26, int(28 * frac) + 4))
            for i in range(steps):
                a0 = start + (end - start) * i / steps
                a1 = start + (end - start) * (i + 1) / steps + 2.4
                c = mix(ring_c, accent2 if not snap.get("peak") else (255, 220, 150), i / steps)
                dd.arc([cx_u - r_u, cy_u - r_u, cx_u + r_u, cy_u + r_u], a0, a1,
                       fill=tuple(int(v) for v in c), width=sw)
            for ang in (start, end):
                ax = cx_u + math.cos(math.radians(ang)) * r_u
                ay = cy_u + math.sin(math.radians(ang)) * r_u
                dd.ellipse([ax - sw / 2, ay - sw / 2, ax + sw / 2, ay + sw / 2],
                           fill=tuple(int(v) for v in ring_c))
        # 环内图标：忙时太阳 / 闲时月亮
        ic = tuple(int(v) for v in ring_c)
        if snap.get("peak"):
            dd.ellipse([cx_u - self.u(2.4), cy_u - self.u(2.4),
                        cx_u + self.u(2.4), cy_u + self.u(2.4)], fill=ic)
            for k in range(8):
                ang = math.radians(k * 45)
                x1 = cx_u + math.cos(ang) * self.u(4.4)
                y1 = cy_u + math.sin(ang) * self.u(4.4)
                x2 = cx_u + math.cos(ang) * self.u(6.2)
                y2 = cy_u + math.sin(ang) * self.u(6.2)
                dd.line([x1, y1, x2, y2], fill=ic, width=max(1, self.u(1.0)))
        else:
            m = Image.new("L", (self.u(18), self.u(18)), 0)
            md = ImageDraw.Draw(m)
            md.ellipse([self.u(4), self.u(3), self.u(15), self.u(14)], fill=255)
            md.ellipse([self.u(8), self.u(1), self.u(19), self.u(12)], fill=0)
            deco.paste(ic + (255,), (cx_u - self.u(9), cy_u - self.u(8)), m)

        # 环的说明：不写这一句，它和下面的进度条会被一起读成"进度"，含义其实不同
        d.text((self.u(cxr), self.u(72.6)), "本时段", font=self.f("cjk", 8.2),
               fill=blend(faint, surface, 1.0), anchor="ms")

        # ---------- 进度条 ----------
        bx0, bx1 = 16.0, 304.0
        by0, bh = 78.0, 5.0
        pct = max(0.0, min(1.0, remain / face if face > 0 else 0.0))
        bar_c = danger if remain < 30 else (warn if remain < 100 else accent)
        track_box = [self.u(bx0), self.u(by0), self.u(bx1), self.u(by0 + bh)]
        dd.rounded_rectangle(track_box, radius=self.u(bh / 2),
                             fill=tuple(int(v) for v in th["track"]))
        fw = (bx1 - bx0) * pct
        if fw > 0.8:
            bw_px = max(2, self.u(fw))
            bh_px = max(2, self.u(bh))
            g2 = Image.new("RGB", (2, 1))
            g2.putpixel((0, 0), tuple(int(v) for v in mix(bar_c, accent2, 0.18)))
            g2.putpixel((1, 0), tuple(int(v) for v in accent2))
            bar = g2.resize((bw_px, bh_px), Image.Resampling.BILINEAR).convert("RGBA")
            bar.putalpha(self._mask(bar.size, bh / 2))
            # 顶面高光
            hl = Image.new("RGBA", bar.size, (255, 255, 255, 0))
            ImageDraw.Draw(hl).rectangle([0, 0, bw_px, max(1, int(bh_px * 0.34))],
                                         fill=(255, 255, 255, 46))
            bar.alpha_composite(Image.composite(hl, Image.new("RGBA", bar.size, (0, 0, 0, 0)),
                                                bar.getchannel("A")))
            # 流光
            phase = ui.get("shine", -1.0)
            if 0 <= phase <= 1 and bw_px > self.u(24):
                gl = Image.new("RGBA", bar.size, (0, 0, 0, 0))
                gx = int(-self.u(30) + phase * (bw_px + self.u(60)))
                ImageDraw.Draw(gl).rectangle([gx, 0, gx + self.u(26), bh_px],
                                             fill=(255, 255, 255, 60))
                bar.alpha_composite(Image.composite(gl, Image.new("RGBA", bar.size, (0, 0, 0, 0)),
                                                    bar.getchannel("A")))
            deco.alpha_composite(bar, (self.u(bx0), self.u(by0)))
            # 进度头部光点
            if 0.02 < pct < 0.995:
                hx = self.u(bx0 + fw)
                hy = self.u(by0 + bh / 2)
                gr = max(3, self.u(4.6))
                pad = gr + max(2, self.u(2))
                glow = Image.new("RGBA", (pad * 2, pad * 2), (0, 0, 0, 0))
                ImageDraw.Draw(glow).ellipse([pad - gr, pad - gr, pad + gr, pad + gr],
                                             fill=tuple(int(v) for v in bar_c) + (120,))
                glow = glow.filter(ImageFilter.GaussianBlur(max(1, self.u(2.2))))
                deco.alpha_composite(glow, (hx - pad, hy - pad))
                dd.ellipse([hx - self.u(1.9), hy - self.u(1.9), hx + self.u(1.9), hy + self.u(1.9)],
                           fill=(255, 255, 255, 240))
        # 刻度
        for k in (0.25, 0.5, 0.75):
            x = self.u(bx0 + (bx1 - bx0) * k)
            inside = pct > k
            dd.line([x, self.u(by0 + 0.8), x, self.u(by0 + bh - 0.8)],
                    fill=(255, 255, 255, 120) if inside else tuple(int(v) for v in th["hair"]),
                    width=1)

        # 进度条的说明 + 低余额的文字提示（只靠变色，色觉障碍用户读不到）
        rem_pct = int(round(pct * 100))
        note = f"剩余 {rem_pct}%" if known else "余额待同步或手动校准"
        note_c = blend(sub, surface, 1.0)
        if known and remain < 30:
            note += " · 余额不足"
            note_c = danger
        elif known and remain < 100:
            days = snap.get("days_left")
            if days is not None and days >= 0:
                note += (" · 不到 1 天用完" if days < 1
                         else f" · 约 {max(1, int(round(days)))} 天用完")
            note_c = warn
        d.text((self.u(16), self.u(74.4)), note, font=self.f("cjk", 8.8),
               fill=note_c, anchor="ls")

        # ---------- 底部信息 ----------
        dot_c = warn if snap.get("peak") else live
        mode_txt = snap.get("mode", "")
        switch_in = int(snap.get("switch_in", 0))
        hh, mm = switch_in // 3600, (switch_in % 3600) // 60
        left = self.u(16)
        dd.ellipse([left, self.u(99.6), left + self.u(5), self.u(104.6)], fill=dot_c)
        mode = f"{mode_txt} · {hh}h{mm:02d}m 后转{snap.get('next_mode','')}"
        d.text((left + self.u(10), self.u(105.2)), mode, font=self.f("cjk", 10.2),
               fill=blend(txt if not snap.get("peak") else txt, surface, 1.0), anchor="ls")

        today = snap.get("today") or {}
        tcost = today.get("cost", 0.0)
        ttok = today.get("tokens", 0)
        if ui.get("hover_close"):
            # 悬停在 ✕ 上时把"它只是隐藏"说清楚，省得以为是退出程序
            d.text((self.u(304), self.u(105.6)), "× 收起",
                   font=self.f("cjk", 9.0), fill=blend(sub, surface, 1.0), anchor="rs")
        else:
            right = f"代理今日 ≈¥{tcost:.2f} · {fmt_tokens(ttok)} tok"
            d.text((self.u(304), self.u(105.6)), right, font=self.f("cjk", 9.4),
                   fill=blend(sub, surface, 1.0), anchor="rs")

        # 首次使用的一次性操作提示（prefs.hint_seen 之后就再也不出现）
        hint = float(ui.get("hint", 0.0) or 0.0)
        if hint > 0.01:
            d.text((self.u(160), self.u(117.4)), "悬停展开 · 右键设置 · 双击面板",
                   font=self.f("cjk", 8.2), fill=blend(faint, surface, hint), anchor="ms")

        # ---------- 展开详情 ----------
        a = self._detail_a(ui)
        if a > 0.004:
            self._draw_details(d, dd, deco, th, surface, snap, a, ui)

    def _draw_details(self, d, dd, deco, th, surface, snap, a, ui):
        accent = th["accent"]
        sub = blend(th["sub"], surface, a)
        faint = blend(th["faint"], surface, a)
        hair = tuple(int(v * a) for v in th["hair"])

        dd.line([self.u(16), self.u(114), self.u(304), self.u(114)], fill=hair, width=1)
        d.text((self.u(16), self.u(126)), "近 12 小时 · 代理趋势", font=self.f("cjk", 9.0), fill=faint, anchor="ls")
        peak_hour = snap.get("peak_hour", 0.0)
        if peak_hour > 0.0005:
            d.text((self.u(304), self.u(126)), f"峰值 ¥{peak_hour:.2f}/h",
                   font=self.f("cjk", 8.8), fill=faint, anchor="rs")

        series = snap.get("hourly") or []
        x0, x1 = 16.0, 304.0
        top, bot = 130.0, 142.0
        n = len(series)
        if n >= 2:
            mx = max(1e-9, max(c for _, c in series))
            step = (x1 - x0) / (n - 1)
            pts = []
            for i, (_hk, c) in enumerate(series):
                pts.append((x0 + i * step, bot - (bot - top) * math.sqrt(max(0.0, c) / mx)))
            # 面积：竖向渐变
            gx, gy = max(2, self.u(x1 - x0)), max(2, self.u(bot - top))
            grad = Image.new("RGBA", (gx, gy), (0, 0, 0, 0))
            gd = ImageDraw.Draw(grad)
            for yy in range(gy):
                av = int(62 * (1 - yy / max(1, gy - 1)) ** 0.75 * a)
                gd.line([(0, yy), (gx, yy)], fill=tuple(int(v) for v in accent) + (max(0, av),))
            mask = Image.new("L", (gx, gy), 0)
            md = ImageDraw.Draw(mask)
            poly = [(0, gy)] + [(self.u(px - x0), self.u(py - top)) for px, py in pts] + [(gx, gy)]
            md.polygon(poly, fill=255)
            grad.putalpha(ImageChops.multiply(grad.getchannel("A"), mask))
            deco.alpha_composite(grad, (self.u(x0), self.u(top)))
            # 折线
            line_c = tuple(int(v) for v in accent) + (int(225 * a),)
            for i in range(n - 1):
                dd.line([self.u(pts[i][0]), self.u(pts[i][1]),
                         self.u(pts[i + 1][0]), self.u(pts[i + 1][1])],
                        fill=line_c, width=max(1, self.u(1.5)), joint="curve")
            dd.line([self.u(x0), self.u(bot + 0.4), self.u(x1), self.u(bot + 0.4)],
                    fill=hair, width=1)
            # 当前小时的点
            ex, ey = pts[-1]
            dd.ellipse([self.u(ex - 2.1), self.u(ey - 2.1), self.u(ex + 2.1), self.u(ey + 2.1)],
                       fill=(255, 255, 255, int(235 * a)),
                       outline=tuple(int(v) for v in accent) + (int(240 * a),),
                       width=max(1, self.u(1.3)))

        # 三行各说一件事，不再左右并排（并排会被读成一对，语义却是错配的）
        row1_y = 158.0
        hit = snap.get("hit_rate", 0.0)
        saved = snap.get("cache_saved", 0.0)
        d.text((self.u(16), self.u(row1_y)),
               f"缓存命中 {hit:.0f}% · 累计省 ¥{saved:.2f}",
               font=self.f("cjk", 9.4), fill=sub, anchor="ls")

        row2_y = 174.0
        count = (snap.get("totals") or {}).get("count", 0)
        unpriced = int((snap.get("totals") or {}).get("unpriced_count", 0) or 0)
        d.text((self.u(16), self.u(row2_y)),
               f"输入 {fmt_tokens(snap.get('input_tokens', 0))} · "
               f"输出 {fmt_tokens(snap.get('output_tokens', 0))} tok · {count} 次请求",
               font=self.f("cjk", 9.0), fill=blend(faint, surface, 1.0), anchor="ls")

        row3_y = 190.0
        if unpriced:
            d.text((self.u(16), self.u(row3_y)), f"{unpriced} 次未计价",
                   font=self.f("cjk", 9.0), fill=th["warn"], anchor="ls")
        ext = snap.get("external_drop") or 0.0
        days = snap.get("days_left")
        if ext > 0.005:
            d.text((self.u(304), self.u(row3_y)), f"外部渠道 -¥{ext:.2f}",
                   font=self.f("cjk", 9.0), fill=th["warn"], anchor="rs")
        elif days is not None:
            label = "不到 1 天" if days < 1 else f"约 {days:.0f} 天"
            d.text((self.u(304), self.u(row3_y)), f"按代理速度 {label}用完",
                   font=self.f("cjk", 9.0), fill=blend(th["warn"], surface, 1.0), anchor="rs")

    # ---- 设置菜单 ----
    def _draw_menu(self, ink, deco, th, surface, ui, content_h, draw_text=True):
        d = _InkDraw(ImageDraw.Draw(ink), draw_text)
        dd = ImageDraw.Draw(deco)
        txt = th["text"]
        sub = th["sub"]
        faint = th["faint"]
        accent = th["accent"]

        d.text((self.u(16), self.u(22)), "浮窗设置", font=self.f("cjk_b", 11.0),
               fill=txt, anchor="ls")
        d.text((self.u(304), self.u(22)), f"v{APP_VERSION}", font=self.f("num_r", 8.6),
               fill=blend(faint, surface, 0.85), anchor="rs")
        dd.line([self.u(16), self.u(31), self.u(304), self.u(31)],
                fill=tuple(int(v) for v in th["hair"]), width=1)

        items = ui.get("menu_items") or []
        sel = ui.get("menu_index", -1)
        for i, it in enumerate(items):
            cy = 46.0 + i * 28.0
            if it.get("sep") and i > 0:
                dd.line([self.u(18), self.u(cy - 14), self.u(302), self.u(cy - 14)],
                        fill=tuple(int(v) for v in th["hair"]), width=1)
            if i == sel:
                dd.rounded_rectangle([self.u(10), self.u(cy - 12), self.u(310), self.u(cy + 12)],
                                     radius=self.u(8), fill=tuple(int(v) for v in th["hover"]))
            color = txt
            if it.get("danger"):
                color = th["danger"]
            if it.get("accent"):
                color = accent
            d.text((self.u(18), self.u(cy + 3.6)), it.get("label", ""),
                   font=self.f("cjk", 10.4), fill=color, anchor="ls")
            val = it.get("value")
            if val:
                d.text((self.u(304), self.u(cy + 3.6)), val, font=self.f("cjk", 9.6),
                       fill=blend(sub, surface, 0.95), anchor="rs")
            elif it.get("arrow"):
                ax, ay = 300.0, cy
                d.line([self.u(ax - 3), self.u(ay), self.u(ax + 3), self.u(ay)],
                       fill=blend(faint, surface, 0.9), width=max(1, self.u(1)))
                d.line([self.u(ax + 3), self.u(ay), self.u(ax - 1), self.u(ay - 4)],
                       fill=blend(faint, surface, 0.9), width=max(1, self.u(1)))
                d.line([self.u(ax + 3), self.u(ay), self.u(ax - 1), self.u(ay + 4)],
                       fill=blend(faint, surface, 0.9), width=max(1, self.u(1)))


# ---------------------------------------------------------------- Win32
HWND_TOPMOST = -1
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_NOOWNERZORDER = 0x0200
GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
ULW_ALPHA = 0x00000002
AC_SRC_OVER = 0x00
AC_SRC_ALPHA = 0x01
DIB_RGB_COLORS = 0
GA_ROOT = 2
MONITOR_DEFAULTTONEAREST = 2
SW_HIDE, SW_SHOWNOACTIVATE = 0, 4

_user32 = ctypes.windll.user32
_gdi32 = ctypes.windll.gdi32

# ---- 前台窗口事件：把"每 0.2 秒问一次 Windows"换成"Windows 主动通知" ----
# 轮询最多要等一个周期才发现目标窗口没了，收起这一类"跟着 app 一起消失"的动作
# 因此总是慢半拍；挂 WinEvent 钩子后是事件发生即通知。
EVENT_SYSTEM_FOREGROUND = 0x0003
EVENT_SYSTEM_MINIMIZESTART = 0x0016
EVENT_SYSTEM_MINIMIZEEND = 0x0017
EVENT_OBJECT_DESTROY = 0x8001
WINEVENT_OUTOFCONTEXT = 0x0000
WINEVENT_SKIPOWNPROCESS = 0x0002
WM_QUIT = 0x0012
WINEVENTPROC = ctypes.WINFUNCTYPE(None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND,
                                  wintypes.LONG, wintypes.LONG, wintypes.DWORD,
                                  wintypes.DWORD)


class ForegroundWatcher:
    """前台切换 / 顶层窗口销毁 / 窗口最小化的即时通知。

    WinEvent 只会投递给"装了钩子并且正在跑消息循环"的那个线程。Tk 的 mainloop
    理论上也会 dispatch，但把 ctypes 回调塞进 Tcl 的事件循环里容易和 Tcl 的
    线程模型打架（回调里一旦碰 Tk 就可能重入），所以单独开一个线程跑
    GetMessageW 循环：回调只置几个 bool（CPython 里 bool 赋值是原子的，不用锁），
    主线程的 tick 去消费。装不上钩子也不影响功能，自动回落到轮询。
    """

    def __init__(self):
        self.dirty = False       # 有事件 → 立刻重算显隐
        self.gone = False        # 有顶层窗口被销毁（跟随目标可能刚关掉）
        self.minimized = False   # 有窗口刚最小化
        self.dead: list = []     # 刚被销毁的顶层窗口句柄（tick 取走，诊断+立刻收）
        self.events = 0          # 累计收到的事件数（诊断用）
        self.ready = False       # 钩子是否装上了
        self._hooks: list = []
        self._proc = None
        self._thread = None
        self._tid = 0
        self._running = False

    def start(self):
        if self._thread is not None:
            return
        self._running = True
        self._thread = threading.Thread(target=self._pump, name="hud-winevent",
                                       daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        for h in self._hooks:
            try:
                _user32.UnhookWinEvent(h)
            except Exception:
                pass
        self._hooks = []
        if self._tid:
            try:
                ctypes.windll.kernel32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)
            except Exception:
                pass

    def take(self) -> tuple[bool, bool]:
        """取走事件标记：(需要立刻重算, 是否有窗口刚消失)。"""
        if not self.dirty:
            return False, False
        gone = self.gone
        self.dirty = False
        self.gone = False
        self.minimized = False
        return True, gone

    def _on_event(self, hook, event, hwnd, id_object, id_child, thread, ts):
        try:
            self.events += 1
            if event == EVENT_OBJECT_DESTROY:
                # 只关心顶层窗口：菜单/控件销毁的事件量大得离谱
                if id_object != 0 or id_child != 0:
                    return
                self.gone = True
                h = int(hwnd or 0)
                if h:
                    self.dead.append(h)
                    if len(self.dead) > 8:
                        del self.dead[:4]
            elif event == EVENT_SYSTEM_MINIMIZESTART:
                self.minimized = True
            self.dirty = True
        except Exception:
            pass

    def _pump(self):
        try:
            _user32.SetWinEventHook.argtypes = [wintypes.DWORD, wintypes.DWORD,
                                                wintypes.HMODULE, WINEVENTPROC,
                                                wintypes.DWORD, wintypes.DWORD,
                                                wintypes.DWORD]
            _user32.SetWinEventHook.restype = wintypes.HANDLE
            _user32.UnhookWinEvent.argtypes = [wintypes.HANDLE]
            _user32.UnhookWinEvent.restype = wintypes.BOOL
            proc = WINEVENTPROC(self._on_event)
            self._proc = proc          # 防 GC：回调对象要活到 UnhookWinEvent
            flags = WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS
            for lo, hi in ((EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND),
                           (EVENT_SYSTEM_MINIMIZESTART, EVENT_SYSTEM_MINIMIZEEND),
                           (EVENT_OBJECT_DESTROY, EVENT_OBJECT_DESTROY)):
                h = _user32.SetWinEventHook(lo, hi, None, proc, 0, 0, flags)
                if h:
                    self._hooks.append(h)
        except Exception:
            self._hooks = []
        self.ready = bool(self._hooks)
        if not self.ready:
            return
        try:
            self._tid = ctypes.windll.kernel32.GetCurrentThreadId()
            msg = wintypes.MSG()
            while self._running:
                got = _user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if got == 0 or got == -1:
                    break
                _user32.TranslateMessage(ctypes.byref(msg))
                _user32.DispatchMessageW(ctypes.byref(msg))
        except Exception:
            pass
        # 线程退出了就把 ready 打回 False：/api/debug 能看出来，
        # 而显隐逻辑本来就有 100ms 轮询兜底，功能不受影响。
        self.ready = False


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_byte), ("BlendFlags", ctypes.c_byte),
                ("SourceConstantAlpha", ctypes.c_byte), ("AlphaFormat", ctypes.c_byte)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


if ctypes.sizeof(ctypes.c_void_p) == 8:
    _get_long = _user32.GetWindowLongPtrW
    _set_long = _user32.SetWindowLongPtrW
else:  # pragma: no cover
    _get_long = _user32.GetWindowLongW
    _set_long = _user32.SetWindowLongW
_get_long.restype = ctypes.c_ssize_t
_get_long.argtypes = [wintypes.HWND, ctypes.c_int]
_set_long.restype = ctypes.c_ssize_t
_set_long.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]

_user32.GetDC.restype = wintypes.HDC
_user32.GetDC.argtypes = [wintypes.HWND]
_user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
_gdi32.CreateCompatibleDC.restype = wintypes.HDC
_gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
_gdi32.CreateDIBSection.restype = wintypes.HBITMAP
_gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.POINTER(BITMAPINFO), wintypes.UINT,
                                    ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD]
_gdi32.SelectObject.restype = wintypes.HGDIOBJ
_gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
_gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
_gdi32.DeleteDC.argtypes = [wintypes.HDC]
_gdi32.BitBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                          wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD]
_user32.UpdateLayeredWindow.argtypes = [wintypes.HWND, wintypes.HDC, ctypes.POINTER(wintypes.POINT),
                                        ctypes.POINTER(wintypes.SIZE), wintypes.HDC,
                                        ctypes.POINTER(wintypes.POINT), wintypes.COLORREF,
                                        ctypes.POINTER(BLENDFUNCTION), wintypes.DWORD]


def set_dpi_awareness():
    try:
        if _user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            _user32.SetProcessDPIAware()
        except Exception:
            pass


def get_dpi_for_window(hwnd) -> int:
    try:
        return int(_user32.GetDpiForWindow(hwnd)) or 96
    except Exception:
        try:
            return int(ctypes.windll.shcore.GetScaleFactorForDevice(0))
        except Exception:
            return 96


def work_area_at(x: int, y: int) -> tuple[int, int, int, int]:
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    hmon = _user32.MonitorFromPoint(wintypes.POINT(x, y), MONITOR_DEFAULTTONEAREST)
    if hmon and _user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
        r = mi.rcWork
        return r.left, r.top, r.right, r.bottom
    return 0, 0, _user32.GetSystemMetrics(0), _user32.GetSystemMetrics(1)


def virtual_screen() -> tuple[int, int, int, int]:
    return (_user32.GetSystemMetrics(76), _user32.GetSystemMetrics(77),
            _user32.GetSystemMetrics(78), _user32.GetSystemMetrics(79))


def cursor_pos() -> tuple[int, int]:
    p = wintypes.POINT()
    _user32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


def foreground_window() -> int:
    """当前前台顶层窗口句柄（0 = 没有）。单独封一层，测试里可以替换。"""
    try:
        return int(_user32.GetForegroundWindow() or 0)
    except Exception:
        return 0


class ScreenGrab:
    """屏幕截图（BitBlt 不带 CAPTUREBLT，天然排除本程序这类分层窗口）。"""

    def __init__(self):
        self._size = None
        self._hdc = None
        self._hdc_mem = None
        self._bmp = None
        self._bits = None

    def _ensure(self, w, h):
        if self._size == (w, h):
            return
        self.close()
        self._hdc = _user32.GetDC(None)
        self._hdc_mem = _gdi32.CreateCompatibleDC(self._hdc)
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = -h
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0
        bits = ctypes.c_void_p()
        self._bmp = _gdi32.CreateDIBSection(self._hdc_mem, ctypes.byref(bmi), DIB_RGB_COLORS,
                                           ctypes.byref(bits), None, 0)
        _gdi32.SelectObject(self._hdc_mem, self._bmp)
        self._bits = bits
        self._size = (w, h)

    def grab(self, x, y, w, h):
        if w <= 0 or h <= 0:
            return None
        self._ensure(w, h)
        if not _gdi32.BitBlt(self._hdc_mem, 0, 0, w, h, self._hdc, x, y, 0x00CC0020):
            return None
        buf = ctypes.string_at(self._bits, w * h * 4)
        return Image.frombuffer("RGB", (w, h), buf, "raw", "BGRX", 0, 1)

    def close(self):
        try:
            if self._bmp:
                _gdi32.DeleteObject(self._bmp)
            if self._hdc_mem:
                _gdi32.DeleteDC(self._hdc_mem)
            if self._hdc:
                _user32.ReleaseDC(None, self._hdc)
        except Exception:
            pass
        self._size = None
        self._hdc = self._hdc_mem = self._bmp = self._bits = None


class LayeredPainter:
    """把 RGBA 图贴到 WS_EX_LAYERED 窗口上。"""

    def __init__(self, hwnd):
        self.hwnd = hwnd
        self._size = None
        self._hdc_screen = None
        self._hdc_mem = None
        self._bmp = None
        self._bits = None
        self._painted = None       # 已经上传过像素的尺寸
        self._pos = None
        self._alpha = -1

    def _ensure(self, w, h):
        if self._size == (w, h):
            return
        self.close()
        self._hdc_screen = _user32.GetDC(None)
        self._hdc_mem = _gdi32.CreateCompatibleDC(self._hdc_screen)
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = -h
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0
        bits = ctypes.c_void_p()
        self._bmp = _gdi32.CreateDIBSection(self._hdc_mem, ctypes.byref(bmi), DIB_RGB_COLORS,
                                           ctypes.byref(bits), None, 0)
        _gdi32.SelectObject(self._hdc_mem, self._bmp)
        self._bits = bits
        self._size = (w, h)

    def has_pixels(self) -> bool:
        return self._painted is not None

    def paint(self, img, x, y, alpha=255):
        w, h = img.size
        self._ensure(w, h)
        # UpdateLayeredWindow 需要预乘 alpha 的 BGRA。"RGBa" 就是预乘过的 RGBA，
        # 一次 convert 交给 C 做，比 split+3 次 multiply+merge 快一倍（1.2 vs 2.1ms）。
        data = img.convert("RGBa").tobytes("raw", "BGRa")
        ctypes.memmove(self._bits, data, len(data))
        self._painted = (w, h)
        return self._blit(x, y, alpha)

    def realpha(self, x, y, alpha=255):
        """像素没变、只有整体不透明度在变（淡入淡出）：跳过重绘，0.3ms 出一帧。"""
        if self._painted is None:
            return False
        return self._blit(x, y, alpha)

    def _blit(self, x, y, alpha):
        w, h = self._painted
        blend = BLENDFUNCTION(AC_SRC_OVER, 0, max(0, min(255, int(alpha))), AC_SRC_ALPHA)
        size = wintypes.SIZE(w, h)
        src = wintypes.POINT(0, 0)
        dst = wintypes.POINT(int(x), int(y))
        ok = _user32.UpdateLayeredWindow(self.hwnd, self._hdc_screen, ctypes.byref(dst),
                                         ctypes.byref(size), self._hdc_mem, ctypes.byref(src),
                                         0, ctypes.byref(blend), ULW_ALPHA)
        self._pos = (int(x), int(y))
        self._alpha = int(alpha)
        return bool(ok)

    def close(self):
        try:
            if self._bmp:
                _gdi32.DeleteObject(self._bmp)
            if self._hdc_mem:
                _gdi32.DeleteDC(self._hdc_mem)
            if self._hdc_screen:
                _user32.ReleaseDC(None, self._hdc_screen)
        except Exception:
            pass
        self._size = None
        self._painted = None
        self._pos = None
        self._alpha = -1
        self._hdc_screen = self._hdc_mem = self._bmp = self._bits = None


def process_exe(pid: int) -> str:
    """返回进程可执行文件名（小写）；失败返回空串。"""
    try:
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            # QueryFullProcessImageNameW 要求 size 入参先填缓冲区字符数，
            # 传 0 会直接失败且不设 GetLastError（表现为"永远查不到进程名"）。
            size = wintypes.DWORD(len(buf))
            if ctypes.windll.kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return Path(buf.value).name.lower()
        finally:
            ctypes.windll.kernel32.CloseHandle(h)
    except Exception:
        pass
    return ""


def find_windows(matchers) -> list[tuple[int, str, str]]:
    """枚举顶层窗口，返回 (hwnd, title, exe) 里命中 matchers 的。"""
    hits = []
    EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    pid_cache: dict[int, str] = {}

    def exe_of(pid):
        if pid in pid_cache:
            return pid_cache[pid]
        name = process_exe(pid)
        pid_cache[pid] = name
        return name

    def cb(hwnd, _):
        try:
            n = _user32.GetWindowTextLengthW(hwnd)
            title = ""
            if n > 0:
                buf = ctypes.create_unicode_buffer(n + 1)
                _user32.GetWindowTextW(hwnd, buf, n + 1)
                title = buf.value
            pid = wintypes.DWORD()
            _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            exe = exe_of(pid.value)
            for m in matchers:
                m = m.lower()
                if (m in title.lower() or m in exe) and _user32.IsWindowVisible(hwnd):
                    hits.append((hwnd, title, exe))
                    break
        except Exception:
            pass
        return True

    _user32.EnumWindows(EnumProc(cb), 0)
    return hits


def app_visible(matchers) -> bool | None:
    hits = find_windows(matchers)
    if not hits:
        return None
    return any(not _user32.IsIconic(h) for h, _, _ in hits)


_FOLLOW_PIDS: dict[tuple, tuple[float, set]] = {}


def _app_match(title: str, exe: str, matchers) -> bool:
    """严格匹配当前窗口：优先进程名；查不到进程名时只接受标题精确/前缀匹配。"""
    t = (title or "").strip().lower()
    e = (exe or "").lower()
    for m in matchers:
        m = str(m).lower().strip()
        if not m:
            continue
        if m in e:
            return True
        if not e and (t == m or t.startswith(m + " ") or t.startswith(m + " -")
                      or t.startswith(m + "—")):
            return True
    return False


def target_pids(matchers) -> set:
    """跟随目标的进程 PID 集合，短缓存；用于前台窗口没有标题/进程名的情况。"""
    key = tuple(str(m).lower() for m in matchers)
    now = time.time()
    hit = _FOLLOW_PIDS.get(key)
    if hit and now - hit[0] < 3.0:
        return hit[1]
    pids = set()
    try:
        for hwnd, title, exe in find_windows(matchers):
            if not _app_match(title, exe, matchers):
                continue
            pid = wintypes.DWORD()
            _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value:
                pids.add(pid.value)
    except Exception:
        pass
    _FOLLOW_PIDS[key] = (now, pids)
    return pids


def foreground_matches(matchers) -> bool:
    """当前前台窗口是否属于跟随目标；只查一个窗口，比 EnumWindows 快很多。"""
    try:
        fg = _user32.GetForegroundWindow()
        if not fg:
            return False
        chain = [fg]
        # 内嵌的 WebView / 浏览器窗口可能由宿主窗口 owner；把 owner/root 也纳入判断
        try:
            owner = _user32.GetWindow(fg, 4)  # GW_OWNER
            if owner and owner not in chain:
                chain.append(owner)
            root = _user32.GetAncestor(fg, 3)  # GA_ROOTOWNER
            if root and root not in chain:
                chain.append(root)
        except Exception:
            pass
        fg_pids = set()
        unknown = False
        for hwnd in chain:
            pid = wintypes.DWORD()
            _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if not pid.value or pid.value == os.getpid():
                continue
            fg_pids.add(pid.value)
            n = _user32.GetWindowTextLengthW(hwnd)
            title = ""
            if n > 0:
                buf = ctypes.create_unicode_buffer(n + 1)
                _user32.GetWindowTextW(hwnd, buf, n + 1)
                title = buf.value
            exe = process_exe(pid.value)
            if not exe:
                unknown = True
            if _app_match(title, exe, matchers):
                return True
        # 只有确实拿不到进程名时才走 PID 兜底（全量 EnumWindows 很贵，不能每 0.2s 跑一次）
        if unknown:
            return bool(fg_pids & target_pids(matchers))
        return False
    except Exception:
        return False


def foreground_is_fullscreen(own_hwnd: int) -> bool:
    try:
        fg = _user32.GetForegroundWindow()
        if not fg or fg == own_hwnd:
            return False
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(fg, ctypes.byref(pid))
        if pid.value == os.getpid():
            return False
        r = wintypes.RECT()
        if not _user32.GetWindowRect(fg, ctypes.byref(r)):
            return False
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        hmon = _user32.MonitorFromWindow(fg, MONITOR_DEFAULTTONEAREST)
        if not hmon or not _user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            return False
        mon = mi.rcMonitor
        return (r.left <= mon.left and r.top <= mon.top
                and r.right >= mon.right and r.bottom >= mon.bottom)
    except Exception:
        return False


def _demo_snapshot(remain=173.25, face=300.0, today_cost=1.48, today_tokens=22.1e6,
                   peak=True, status="live", age=2.0) -> dict:
    dt = now_dt()
    w_start, w_end = window_bounds(dt)
    return {
        "now": dt, "clock": dt.strftime("%H:%M:%S"), "date": dt.strftime("%m-%d"),
        "peak": peak, "mode": "忙时" if peak else "闲时", "next_mode": "闲时" if peak else "忙时",
        "switch_in": 8060, "switch_at": w_end.strftime("%H:%M"),
        "window_frac": 0.42, "face": face, "remain": remain, "spent": face - remain,
        "pct_remain": remain / face * 100, "pct_used": (1 - remain / face) * 100,
        "status": status, "balance_age": age, "balance_note": "",
        "external_drop": 0.0,
        "totals": {"input": 15200000, "cached": 76000000, "cache_write": 3100000,
                   "output": 3150000, "cost": 45.68, "count": 223},
        "per_model": {"deepseek-v4.1-flash-1017": {"count": 180, "cost": 12.4}},
        "recent": [{"ts": "18:31:20", "t": time.time() - 40, "model": "deepseek-v4.1-flash-1017",
                    "input": 1200, "cached": 86000, "cache_write": 0, "output": 900,
                    "peak": True, "cost": 0.0019}],
        "last_model": "deepseek-v4.1-flash-1017", "last_model_at": time.time() - 40,
        "hit_rate": 80.1, "cache_saved": 3.21,
        "today": {"cost": today_cost, "tokens": int(today_tokens), "count": 118},
        "hourly": [(h, c) for h, c in zip(range(12), [0.2, 0.0, 0.0, 1.4, 0.8, 3.6, 8.2,
                                                      22.0, 71.3, 43.0, 12.4, 1.9])],
        "peak_hour": 71.32, "burn_per_hour": 1.9, "days_left": 5.6,
        "seq": 223, "last_request_at": time.time() - 40,
        "total_tokens": 97_800_000, "input_tokens": 94_300_000, "output_tokens": 3_150_000,
        "app": APP_NAME, "version": APP_VERSION,
    }


def _demo_backdrop(kind: str, size) -> "Image.Image":
    w, h = size
    if kind == "dark":
        img = Image.new("RGB", (w, h), (18, 20, 25))
        d = ImageDraw.Draw(img)
        for y in range(h):
            d.line([(0, y), (w, y)], fill=(18 + int(10 * y / h), 20 + int(9 * y / h), 26 + int(10 * y / h)))
        d.rectangle([0, 0, int(w * 0.18), h], fill=(24, 27, 33))
        for i in range(9):
            bar_h = h * 0.035
            radius = max(1, min(4, int(bar_h / 2) - 1))
            d.rounded_rectangle([w * 0.25, h * (0.12 + i * 0.09), w * 0.25 + w * (0.2 + 0.4 * ((i * 37) % 10) / 10), h * (0.12 + i * 0.09) + bar_h],
                                radius=radius, fill=(38, 42, 52))
        img = img.filter(ImageFilter.GaussianBlur(1.2))
    else:
        img = Image.new("RGB", (w, h), (238, 241, 246))
        d = ImageDraw.Draw(img)
        d.ellipse([-w * 0.2, -h * 0.4, w * 0.7, h * 0.9], fill=(224, 233, 248))
        d.ellipse([w * 0.45, -h * 0.2, w * 1.2, h * 0.8], fill=(246, 233, 240))
        d.ellipse([w * 0.1, h * 0.45, w * 0.9, h * 1.4], fill=(233, 245, 238))
        img = img.filter(ImageFilter.GaussianBlur(18))
        d = ImageDraw.Draw(img)
        d.rectangle([0, h * 0.86, w, h], fill=(250, 250, 252))
    return img


def run_preview(outdir: Path, scale: float = 1.0):
    outdir.mkdir(parents=True, exist_ok=True)
    r = Renderer(scale=scale)
    card_w = int(round((W + 2 * PAD) * scale))
    card_h = int(round((MAX_H + 2 * PAD) * scale))
    results = []
    cases = [
        ("collapsed_light", "light", _demo_snapshot(), {"content_h": H, "card_y": PAD + MAX_H - H,
                                                        "dark": 0.0, "expand": 0.0}),
        ("collapsed_dark", "dark", _demo_snapshot(), {"content_h": H, "card_y": PAD + MAX_H - H,
                                                      "dark": 1.0, "expand": 0.0}),
        ("expanded_light", "light", _demo_snapshot(), {"content_h": H_EXP, "card_y": PAD,
                                                       "dark": 0.0, "expand": 1.0}),
        ("expanded_dark", "dark", _demo_snapshot(), {"content_h": H_EXP, "card_y": PAD,
                                                     "dark": 1.0, "expand": 1.0}),
        ("menu_dark", "dark", _demo_snapshot(), {"content_h": H_MENU, "card_y": PAD,
                                                 "dark": 1.0, "menu": True, "menu_index": 2,
                                                 "menu_items": MENU_ITEMS_PREVIEW}),
        ("low_light", "light", _demo_snapshot(remain=26.4), {"content_h": H, "card_y": PAD + MAX_H - H,
                                                             "dark": 0.0, "expand": 0.0}),
        ("hint_light", "light", _demo_snapshot(), {"content_h": H, "card_y": PAD + MAX_H - H,
                                                   "dark": 0.0, "expand": 0.0, "hint": 1.0}),
        ("close_hover_light", "light", _demo_snapshot(), {"content_h": H, "card_y": PAD + MAX_H - H,
                                                          "dark": 0.0, "expand": 0.0,
                                                          "hover_close": True}),
    ]
    for name, kind, snap, ui in cases:
        bd = _demo_backdrop(kind, (card_w, card_h))
        img = r.render(snap, ui, bd, (W, MAX_H))
        if img.size != (card_w, card_h):
            img = img.resize((card_w, card_h), Image.Resampling.LANCZOS)
        scene = bd.convert("RGBA")
        scene.alpha_composite(img)
        # 额外贴一个不同的背景，观察玻璃适应性
        path = outdir / f"{name}.png"
        scene.convert("RGB").save(path)
        results.append(path)
    # 透明卡单独存一份，方便检查边缘
    img = r.render(_demo_snapshot(), {"content_h": H_EXP, "card_y": PAD, "dark": 0.0,
                                      "expand": 1.0}, _demo_backdrop("light", (card_w, card_h)),
                   (W, MAX_H))
    img.save(outdir / "card_expanded_alpha.png")
    return results


def ring_luminance(img, box, size) -> float:
    """取卡片四周约 16px 的页面背景平均亮度；不用卡片正下方内容判断主题。"""
    w, h = size
    x0 = max(0, min(w, int(round(box[0]))))
    y0 = max(0, min(h, int(round(box[1]))))
    x1 = max(0, min(w, int(round(box[2]))))
    y1 = max(0, min(h, int(round(box[3]))))
    pad = 16
    parts = []
    if y0 > 0:
        parts.append(img.crop((0, max(0, y0 - pad), w, y0)))
    if y1 < h:
        parts.append(img.crop((0, y1, w, min(h, y1 + pad))))
    if x1 > x0 and y1 > y0:
        if x0 > 0:
            parts.append(img.crop((max(0, x0 - pad), y0, x0, y1)))
        if x1 < w:
            parts.append(img.crop((x1, y0, min(w, x1 + pad), y1)))
    if not parts:
        parts = [img.crop((x0, y0, x1, y1)) if x1 > x0 and y1 > y0 else img]
    total = 0.0
    weight = 0
    for part in parts:
        if part.width <= 0 or part.height <= 0:
            continue
        gray = part.convert("L")
        area = gray.width * gray.height
        total += ImageStat.Stat(gray).mean[0] * area
        weight += area
    return (total / weight) if weight else 200.0


MENU_ITEMS_PREVIEW = [
    {"key": "panel", "label": "打开监控面板", "arrow": True},
    {"key": "cal", "label": "余额校准 / 同步…", "arrow": True},
    {"key": "follow", "label": "跟随", "value": "两者", "sep": True},
    {"key": "through", "label": "点击穿透", "value": "开"},
    {"key": "glass", "label": "不透明度", "value": "100%  >"},
    {"key": "hide", "label": "收起浮窗", "value": "", "sep": True},
    {"key": "quit", "label": "退出监控", "danger": True, "sep": True},
]


# ---------------------------------------------------------------- 偏好 / 数据源
PREFS_FILE = BASE_DIR / ".hud_prefs.json"
_PREFS_LOCK = threading.Lock()


def load_prefs() -> dict:
    d = {}
    try:
        if PREFS_FILE.exists():
            d = json.loads(PREFS_FILE.read_text(encoding="utf-8"))
    except Exception:
        d = {}
    if not isinstance(d, dict):
        d = {}
    d.setdefault("follow", "zcode")    # 独立程序默认跟随 ZCode
    d.setdefault("through", True)
    d.setdefault("glass", 1.0)
    d.setdefault("hint_seen", False)
    d.setdefault("coupon", "")         # 绑定券号；空=自动挑匹配的可用券
    d.setdefault("api_token", "")
    d["coupon"] = str(d.get("coupon") or "")
    d["api_token"] = str(d.get("api_token") or "")
    # 老版本把 follow 写成过 "auto" 之类的脏值，这里统一成合法枚举，
    # 否则界面上"跟随"会一直显示成不是你选的那个。
    d["follow"] = normalize_follow(d.get("follow"))
    d["through"] = bool(d.get("through", True))
    d["hint_seen"] = bool(d.get("hint_seen"))
    try:
        d["glass"] = max(0.35, min(1.0, float(d.get("glass", 1.0))))
    except (TypeError, ValueError):
        d["glass"] = 1.0
    return d


def ensure_api_token(prefs: dict) -> str:
    """本地写接口的令牌；首次启动生成并写入 .hud_prefs.json。"""
    token = str(prefs.get("api_token") or "")
    if not token:
        token = secrets.token_urlsafe(24)
        prefs["api_token"] = token
        save_prefs(prefs)
    return token


def save_prefs(d: dict):
    try:
        with _PREFS_LOCK:
            tmp = PREFS_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(PREFS_FILE)
    except Exception as exc:
        log(f"prefs save failed: {exc!r}")


class RemoteFeed(threading.Thread):
    """别人的实例在跑时，本窗口只当显示端。"""

    def __init__(self, port: int):
        super().__init__(daemon=True, name="hud-feed")
        self.port = port
        self.lock = threading.Lock()
        self.latest = None
        self._stop = threading.Event()

    def run(self):
        url = f"http://{LISTEN_HOST}:{self.port}/api/stats"
        while not self._stop.wait(1.0):
            try:
                with urllib.request.urlopen(url, timeout=1.5) as r:
                    data = json.loads(r.read().decode("utf-8"))
                with self.lock:
                    self.latest = data
            except Exception:
                pass

    def snapshot(self):
        with self.lock:
            return self.latest

    def stop(self):
        self._stop.set()


class DemoSource:
    def __init__(self):
        self.t0 = time.time()
        self.base = 173.25

    def snapshot(self):
        el = time.time() - self.t0
        s = _demo_snapshot(remain=max(4.0, self.base - el * 0.02))
        s["seq"] = 223 + int(el // 2.5)
        s["last_request_at"] = time.time()
        s["today"] = {"cost": 1.48 + el * 0.0004, "tokens": int(22.1e6 + el * 1000),
                      "count": 118 + int(el // 30)}
        return s

    def save(self):
        pass


# ---------------------------------------------------------------- 悬浮窗
class HudApp(tk.Tk):
    def __init__(self, source, store: Store | None = None, service: ProxyService | None = None,
                 port: int = LISTEN_PORT, prefs: dict | None = None, show_flag: dict | None = None,
                 coupon_sync: CouponSync | None = None, plugin_mode=False, owner_pid=0):
        super().__init__()
        self.source = source
        self.store = store
        self.service = service
        self.port = port
        self.prefs = prefs or load_prefs()
        self.show_flag = show_flag if show_flag is not None else {}
        self.controls = queue.Queue()
        self.tray = None
        self._closing = False
        self.plugin_mode, self.owner_pid = plugin_mode, owner_pid
        self.coupon_sync = coupon_sync
        self.resizable(False, False)

        self.withdraw()
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.title(APP_NAME)
        self.configure(bg="#000000")
        self.geometry("200x200+-20000+-20000")
        self.update_idletasks()
        self.hwnd = _user32.GetAncestor(self.winfo_id(), GA_ROOT) or self.winfo_id()
        self.scale = max(1.0, get_dpi_for_window(self.hwnd) / 96.0)
        self.win_w = int(round((W + 2 * PAD) * self.scale))
        self.win_h = int(round((MAX_H + 2 * PAD) * self.scale))
        self.geometry(f"{self.win_w}x{self.win_h}+-20000+-20000")
        self.update_idletasks()
        self._style()

        self.painter = LayeredPainter(self.hwnd)
        self.grabber = ScreenGrab()
        self.renderer = Renderer(self.scale)
        self.backdrop = None
        self.backdrop_seq = 0
        self._bg_pos = None
        self._bg_thumb = None
        self._bg_skip = 0
        self.last_img = None

        # 状态
        self.snap = None
        self.data_sig = None
        self.dark = 0.0
        self.dark_target = 0.0
        self._dark_from = 0.0
        self._dark_t0 = 0.0
        self._theme_lums = deque(maxlen=2)
        self._lum_last = None          # 上一帧取样亮度（判"背景换了张脸"）
        self._last_fg = 0
        self.expand = 0.0
        self._hold_expand = False
        self.shown = 0.0
        self.shown_target = False
        self.hover_close = False
        self.menu_open = False
        self.glass_menu = False
        self.menu_index = -1
        self.menu_items = []
        self._quit_armed_until = 0.0
        self._user_hidden = False
        self._dragging = False
        self._drag_dx = self._drag_dy = 0
        self._inside = False
        self._inside_since = 0.0
        self._outside_since = 0.0
        self.expand_want = 0.0
        self.pulse_t0 = 0.0
        self.shine_t0 = 0.0
        self._last_tick = time.perf_counter()
        self._t_data = 0.0
        self._t_vis = 0.0
        self._t_bg = 0.0
        self._bg_ts = 0.0
        self._last_drag_paint = 0.0
        self._paint_ms = 0.0
        self._render_ms = 0.0
        self._prev_key = None
        self._paint_reason = []
        self._pix_key = None       # 上一次真正上传的像素指纹（只变 alpha 时省掉重渲染）
        self._paint_err_ts = 0.0
        self._bg_err_ts = 0.0
        self._tick_err_ts = 0.0
        self._fade_t0 = 0.0
        self._fade_from = 0.0
        self._anchor_dirty = False
        self._t_dpi = 0.0          # 上一次检查窗口所在显示器 DPI 的时刻
        self._press_x = 0
        self._press_y = 0
        self._drag_moved = False
        self._last_click = None
        self._t_hot = 0.0
        self._hot_down = False
        self._peek_fg = 0          # 热键/接口唤出时锁定的前台窗口，切走即失效
        self._hidden_armed = False  # ✕ 收起后，是否已经离开过目标一次
        self._hidden_since = 0.0
        self._was_matched = True
        self._hide_since = 0.0      # 收起滞回计时
        self._hide_fast = False     # 这次收起是否"目标已消失"，可跳过防抖
        self.watcher = ForegroundWatcher()   # 前台/销毁/最小化的即时通知
        self._vis_dirty = False     # 钩子报过事件，这一拍立刻重算显隐
        self._vis_win = True       # 目标还有没有可见窗口（None=一个都没了）
        self._t_vis_win = 0.0
        self._vis_log_ts = 0.0
        self._vis_live_ts = 0.0
        self._fg_win = 0            # 正在跟随的前台窗口（被销毁=用户关掉了 agent）
        self._fg_hold = 0           # 关掉 agent 后 Windows 回退的那个前台窗口，别急着再浮出来
        self._fg_settle = 0.0       # 关窗后的"前台重排"窗口期，到点前一律保持收起
        self._ct_off = False        # 当前是否为了操作临时关闭了点击穿透
        self._hint_until = 0.0
        self._hint_done = False
        self._shown_since = 0.0
        self._hint_seen = bool(self.prefs.get("hint_seen"))

        # 位置
        px, py = self.prefs.get("x"), self.prefs.get("y")
        if px is None or py is None or self.prefs.get("_reset_pos"):
            px, py = self._default_pos()
        self.pos_x, self.pos_y = self._clamp_pos(int(px), int(py))
        self.anchor = self._anchor_for()

        self.bind("<Button-1>", self._on_click)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Button-3>", self._on_right)
        self.bind("<Double-Button-1>", self._on_double)

        self.protocol("WM_DELETE_WINDOW", self._hide_by_user)

    # ---------- 窗口 ----------
    def _style(self):
        ex = _get_long(self.hwnd, GWL_EXSTYLE)
        _set_long(self.hwnd, GWL_EXSTYLE,
                  ex | WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)
        _user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                             SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)

    def _set_click_through(self, on: bool):
        ex = _get_long(self.hwnd, GWL_EXSTYLE)
        new = (ex | WS_EX_TRANSPARENT) if on else (ex & ~WS_EX_TRANSPARENT)
        if new != ex:
            _set_long(self.hwnd, GWL_EXSTYLE, new)

    def _arm(self, on: bool):
        """on=True 表示暂时接管鼠标（关掉穿透），只在真的有操作意图时调用。"""
        if on == self._ct_off:
            return
        self._ct_off = on
        self._set_click_through(not on)

    def start(self):
        self.deiconify()
        _user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE)
        self._set_click_through(bool(self.prefs.get("through", True)))
        self._ct_off = not bool(self.prefs.get("through", True))
        if self.service is not None and self.service.server is not None:
            self.service.server.debug_state = self._debug_state  # type: ignore[attr-defined]
        self._update_data(force=True)
        self._refresh_backdrop()
        self.shown_target = True
        self.shown = 0.0
        self._shown_since = time.time()
        self._paint()
        self.watcher.start()
        self.after(16, self._tick)

    def _control(self, command):
        """Called only on the Tk thread; HTTP/tray callbacks enqueue commands."""
        log(f"hud control: {command}")
        if command == "quit":
            self._quit_now()
        elif command == "panel":
            self._open_dashboard()
        elif command == "hide" or (command == "toggle" and self.shown_target):
            self._hide_by_user()
        elif command in ("show", "toggle"):
            self._user_hidden = False
            self._hidden_armed = False
            self._fg_hold = 0
            self._fg_settle = 0.0
            self._peek_fg = foreground_window() or 1
            self._t_vis = 0.0
            self._set_shown(True)

    def _publish_hud_state(self):
        if self.service and self.service.server:
            self.service.server.hud_state = {
                "ok": True, "running": True, "shown": bool(self.shown_target),
                "user_hidden": bool(self._user_hidden),
                "follow": self.prefs.get("follow", "both"),
                "port": self.port, "tray": bool(self.tray and self.tray.hwnd),
                "mode": "plugin" if self.plugin_mode else "standalone",
                "owner_pid": self.owner_pid,
            }

    # ---------- 几何 ----------
    def _default_pos(self):
        x, y = cursor_pos()
        l, t, r, b = work_area_at(x, y)
        return (r - int(W * self.scale) - int(40 * self.scale),
                b - int(H * self.scale) - int(40 * self.scale))

    def _clamp_pos(self, x, y):
        w, h = W * self.scale, H * self.scale
        # MonitorFromPoint 选最近的真实显示器，包含其工作区（排除任务栏）。
        l, t, r, b = work_area_at(int(float(x) + w / 2), int(float(y) + h / 2))
        pad = PAD * self.scale
        x = max(l + pad, min(float(x), max(l + pad, r - w - pad)))
        y = max(t + pad, min(float(y), max(t + pad, b - h - pad)))
        return int(round(x)), int(round(y))

    def _anchor_for(self):
        """选长高方向。默认优先 "top"（顶边钉住、向下长）：
        ✕ 画在卡片顶部，只有顶边固定时它才不会在展开/开菜单时从鼠标底下溜走
        ——那正是"点了 ✕ 没反应"的来源。只有下面实在放不下才改成向上长。
        """
        l, t, r, b = work_area_at(int(self.pos_x + W * self.scale / 2),
                                  int(self.pos_y + H * self.scale / 2))
        need = (H_EXP - H) * self.scale      # 悬停展开多出来的高度（菜单另有自己的判断）
        room_up = self.pos_y - t
        room_down = b - (self.pos_y + H * self.scale)
        if room_down >= need:
            return "top"
        if room_up >= need:
            return "bottom"
        return "bottom" if room_up >= room_down else "top"

    def _set_anchor(self, a):
        if a == self.anchor:
            return
        self.anchor = a
        self._bg_ts = 0.0          # 窗口换了位置，背景要重抓
        self._anchor_dirty = True
        self._refresh_backdrop(force=True)

    def _apply_scale(self, scale):
        """显示器 DPI 变了（例如拖到另一块不同缩放的屏）：整体重建。"""
        scale = max(1.0, min(4.0, float(scale)))
        if abs(scale - self.scale) < 0.02:
            return
        self.scale = scale
        self.win_w = int(round((W + 2 * PAD) * scale))
        self.win_h = int(round((MAX_H + 2 * PAD) * scale))
        self.renderer = Renderer(scale)
        self._pix_key = None
        self.pos_x, self.pos_y = self._clamp_pos(self.pos_x, self.pos_y)
        self.anchor = self._anchor_for()
        self._bg_ts = 0.0
        self._refresh_backdrop(force=True)
        self._paint()
        log(f"dpi scale -> {scale:.2f} ({self.win_w}x{self.win_h})")

    def _content_units(self):
        if self.menu_open:
            return float(H_MENU)
        return H + (H_EXP - H) * max(0.0, min(1.0, self.expand))

    def _card_y_units(self):
        ch = self._content_units()
        return float(PAD) if self.anchor == "top" else float(PAD + MAX_H - ch)

    def _window_xy(self):
        if self.anchor == "top":
            return (int(round(self.pos_x - PAD * self.scale)),
                    int(round(self.pos_y - PAD * self.scale)))
        return (int(round(self.pos_x - PAD * self.scale)),
                int(round(self.pos_y + H * self.scale - (PAD + MAX_H) * self.scale)))

    def _card_top_px(self):
        ch = self._content_units() * self.scale
        if self.anchor == "top":
            return float(self.pos_y)
        return float(self.pos_y + H * self.scale - ch)

    def _card_rect(self):
        top = self._card_top_px()
        return (float(self.pos_x), top,
                float(self.pos_x) + W * self.scale, top + self._content_units() * self.scale)

    def _hit_card(self, cx, cy):
        l, t, r, b = self._card_rect()
        return l <= cx <= r and t <= cy <= b

    def _hit_close(self, cx, cy):
        l, t, _, _ = self._card_rect()
        ccx = l + 305 * self.scale
        ccy = t + 17.4 * self.scale
        return (cx - ccx) ** 2 + (cy - ccy) ** 2 <= (14 * self.scale) ** 2

    def _near_close(self, cx, cy):
        """比 ✕ 命中圈更大的"预备圈"（2 倍半径）。

        接管鼠标要等下一拍才生效（Windows 的 Tk 定时器最小刻度 15.6ms），
        光标如果直接甩到 ✕ 上再点，第一下有可能还穿到下层窗口去。
        先把预备圈当接管范围，等手指真的按下去时窗口早就武装好了；
        这个圈只有 2 倍半径、还压在卡片右上角，代价是"这里点不穿"，可以接受。
        """
        l, t, _, _ = self._card_rect()
        ccx = l + 305 * self.scale
        ccy = t + 17.4 * self.scale
        return (cx - ccx) ** 2 + (cy - ccy) ** 2 <= (28 * self.scale) ** 2

    def _hit_menu_row(self, cx, cy):
        if not self.menu_open:
            return -1
        l, t, r, b = self._card_rect()
        if not (l <= cx <= r):
            return -1
        for i in range(len(self.menu_items)):
            cy0 = t + (46 + i * 28 - 13) * self.scale
            cy1 = t + (46 + i * 28 + 13) * self.scale
            if cy0 <= cy <= cy1:
                return i
        return -1

    # ---------- 数据 ----------
    def _debug_state(self):
        ex = _get_long(self.hwnd, GWL_EXSTYLE)
        wx, wy = self._window_xy()
        return {
            "pos": [self.pos_x, self.pos_y], "anchor": self.anchor,
            "window": [wx, wy], "card": list(self._card_rect()),
            "cursor": list(cursor_pos()), "inside": bool(self._inside),
            "shown": round(self.shown, 3), "shown_target": bool(self.shown_target),
            "expand": round(self.expand, 3), "menu": bool(self.menu_open),
            "dark": round(self.dark, 3), "dark_target": round(self.dark_target, 3),
            "through": bool(ex & WS_EX_TRANSPARENT), "scale": self.scale,
            "watch": bool(self._ct_off), "hide_since": round(self._hide_since, 2),
            "hide_fast": bool(self._hide_fast),
            "fg_win": int(self._fg_win or 0), "fg_hold": int(self._fg_hold or 0),
            "win_event": {"ready": bool(self.watcher.ready),
                          "events": int(self.watcher.events),
                          "hooks": len(self.watcher._hooks)},
            "peek": int(self._peek_fg or 0), "hint": bool(time.time() < self._hint_until),
            "render_ms": round(self._render_ms, 1), "paint_ms": round(self._paint_ms, 1),
            "paint_reason": list(self._paint_reason),
            "hover_close": bool(self.hover_close), "dragging": bool(self._dragging),
            "last_click": dict(self._last_click) if self._last_click else None,
            "hwnd": int(self.hwnd), "user_hidden": bool(self._user_hidden),
            "coupon_sync": self.coupon_sync.status() if self.coupon_sync else None,
        }

    def _update_data(self, force=False):
        try:
            snap = self.source.snapshot()
        except Exception as exc:
            log(f"snapshot failed: {exc!r}")
            return
        if not snap:
            return
        sig = self._data_digest(snap)
        if force or sig != self.data_sig:
            prev_seq = (self.snap or {}).get("seq")
            if prev_seq is not None and snap.get("seq", 0) > prev_seq:
                self.pulse_t0 = time.time()
                if time.time() - self.shine_t0 > 2.2:
                    self.shine_t0 = time.time()
            self.snap = snap
            self.data_sig = sig

    @staticmethod
    def _data_digest(s):
        today = s.get("today") or {}
        return (
            round(float(s.get("remain", 0)), 2),
            s.get("status"),
            s.get("balance_kind"), s.get("balance_ts"), bool(s.get("balance_sync_error")),
            s.get("balance_sync_issue"),
            round(float(s.get("face", 0)), 2),
            int(s.get("switch_in", 0) // 60),
            round(float(today.get("cost", 0)), 2),
            int(float(today.get("tokens", 0)) // 200000),
            round(float(s.get("hit_rate", 0))),
            int((s.get("totals") or {}).get("count", 0)),
            None if s.get("days_left") is None else round(float(s["days_left"]), 1),
            round(float(s.get("external_drop", 0)), 2),
            round(float(s.get("window_frac", 0)), 2),
            None if s.get("balance_age") is None else int(s["balance_age"] // 30),
            int(s.get("seq", 0)),
            int(s.get("pct_remain", 0)),
        )

    def _pulse(self):
        if not self.pulse_t0:
            return 0.0
        t = time.time() - self.pulse_t0
        return math.sin(math.pi * t / 0.9) if t < 0.9 else 0.0

    def _shine(self):
        if not self.shine_t0:
            return -1.0
        t = time.time() - self.shine_t0
        return t / 1.35 if t < 1.35 else -1.0

    # ---------- 画布 ----------
    @staticmethod
    def _bg_fingerprint(bd) -> bytes:
        try:
            small = bd.resize((4, 4), Image.Resampling.BILINEAR).convert("L")
        except Exception:
            return b""
        return bytes(v // 16 for v in small.tobytes())

    def _refresh_backdrop(self, force=False):
        now = time.perf_counter()
        # 展开/收起动画进行时冻结背景：backdrop_seq 一变，玻璃就得整块重砌，
        # 实测一帧从 5.3ms 涨到 25.8ms（scale1.5，480x372）。动画只有 0.2s，
        # 玻璃本来就是糊的，背景晚 0.2s 更新看不出来，但掉帧看得出。
        if not force and not self._hold_expand and abs(self.expand - self.expand_want) > 0.004:
            return
        # 抓屏要 1~3ms，显隐/拖动时会被高频调用，合并到 ~8Hz 就够用
        if not force and now - self._bg_ts < 0.12:
            return
        self._bg_ts = now
        wx, wy = self._window_xy()
        try:
            bd = self.grabber.grab(wx, wy, self.win_w, self.win_h)
        except Exception:
            # 锁屏、切到安全桌面、显卡驱动打嗝时 BitBlt 会失败：
            # 保留上一张背景就好，别让整个 tick 挂掉
            self._backdrop_error()
            return
        if bd is None:
            return
        # 桌面上什么都没变（用户没滚动、没切窗口）就别重砌玻璃：
        # 玻璃模糊一张满窗图要 7~13ms，静止时每秒白烧一次很浪费。
        # 用 4x4 粗亮度指纹判断"变了没有"——光标闪烁、页面微光都不算变；
        # 最多糊 10s 就强制换一次，免得慢慢漂移。
        fp = self._bg_fingerprint(bd)
        if (not force) and fp == self._bg_thumb and self._bg_pos == (wx, wy):
            self._bg_skip += 1
            if self._bg_skip < 8:
                return
        self._bg_skip = 0
        self._bg_thumb = fp
        moved = self._bg_pos is not None and self._bg_pos != (wx, wy)
        self.backdrop = bd
        self.backdrop_seq += 1
        self._bg_pos = (wx, wy)
        # 取样卡片四周的页面背景；两帧一致才切换，避免聊天内容/切窗白屏导致明暗交替
        l, t, r, b = self._card_rect()
        lum = ring_luminance(bd, (l - wx, t - wy, r - wx, b - wy),
                             (self.win_w, self.win_h))
        prev_lum = None if moved else self._lum_last
        if moved:
            self._theme_lums.clear()   # 位置变了，之前采的样不算数（背景不是同一块）
        self._lum_last = lum
        self._theme_lums.append(lum)
        # 亮度一次跳一大截 = 整个背景换了张脸（切窗口/切 app），这一帧就认，
        # 别再等第二帧——否则玻璃已经跟着新背景变了，文字主题却要晚 1.2~2.4 秒才切，
        # 看起来像"卡了一下才反应过来"。聊天内容、光标闪烁这类局部变化跳不了这么多。
        if prev_lum is not None and abs(lum - prev_lum) >= 55:
            self._theme_lums.clear()
            self._theme_lums.append(lum)
            if lum < 92:
                self._set_dark(True)
            elif lum > 124:
                self._set_dark(False)
            return
        if len(self._theme_lums) < 2:
            return
        a, b = self._theme_lums
        if a < 92 and b < 92:
            self._set_dark(True)
        elif a > 124 and b > 124:
            self._set_dark(False)

    def _set_dark(self, want):
        """切换明暗主题的动画目标。只有目标真的变了才重新起一段过渡，
        否则每 1.2 秒一次的背景刷新会不断把过渡"归零重来"（看起来像卡住）。"""
        want = 1.0 if want else 0.0
        if want == self.dark_target:
            return
        self.dark_target = want
        self._dark_from = self.dark
        self._dark_t0 = time.perf_counter()
        log(f"theme -> {'dark' if want else 'light'} lum={self._lum_last}")

    def _ui_dict(self):
        self.menu_items = self._menu_items()
        hint = 1.0 if time.time() < self._hint_until else 0.0
        menu_sig = tuple(tuple(sorted(item.items())) for item in self.menu_items) if self.menu_open else ()
        pulse, shine = self._pulse(), self._shine()
        # 详情淡入按 4 档参与缓存键：展开动画里只有这几档要重画整卡
        detail_a = max(0.0, min(1.0, (self.expand - 0.55) / 0.45))
        return {
            "content_h": self._content_units(),
            "card_y": self._card_y_units(),
            "dark": self.dark,
            "expand": self.expand,
            "detail_a": detail_a,
            "pulse": pulse,
            "shine": shine,
            "menu": self.menu_open,
            "menu_items": self.menu_items,
            "menu_index": self.menu_index,
            "hover_close": self.hover_close,
            "hint": hint,
            "opacity": float(self.prefs.get("glass", 1.0)),
            "backdrop_seq": self.backdrop_seq,
            "ink_sig": (self.data_sig, int(round(detail_a * 3)), self.menu_open,
                        self.menu_index, self.hover_close,
                        self.backdrop_seq, 1 if hint else 0, menu_sig,
                        int(round(pulse * 12)), int(round(shine * 20))),
        }

    _KEY_NAMES = ("ink_sig", "content_h", "card_y", "dark", "opacity",
                  "backdrop_seq", "win_w", "win_h", "anchor")

    def _pix_key_of(self, ui):
        """决定"像素长什么样"的全部输入；只有它变了才需要重新渲染。"""
        return (ui["ink_sig"], round(ui["content_h"], 2), round(ui["card_y"], 2),
                round(ui["dark"], 3), round(float(ui["opacity"]), 2),
                ui["backdrop_seq"], self.win_w, self.win_h, self.anchor)

    def _paint(self, force=False):
        if not self.snap:
            return
        # 已经收起且窗口已隐藏：先判断再渲染，别白花 13ms
        if self.shown <= 0.002 and not self.shown_target:
            return
        t0 = time.perf_counter()
        ui = self._ui_dict()
        alpha = int(255 * max(0.0, min(1.0, self.shown)))
        wx, wy = self._window_xy()
        key = self._pix_key_of(ui)
        # 淡入淡出期间像素不变、只有整体 alpha 在变：直接改 ULW 的常量 alpha，
        # 0.3ms 一帧（原来是 5ms），这才是"关闭要立刻有反馈"的关键。
        if not force and key == self._pix_key and self.painter.has_pixels():
            try:
                if self.painter.realpha(wx, wy, alpha):
                    self._paint_ms = (time.perf_counter() - t0) * 1000
                    return
            except Exception:
                pass     # 退回去走完整重绘
        try:
            img = self.renderer.render(self.snap, ui, self.backdrop, (W, MAX_H))
            if img.size != (self.win_w, self.win_h):
                img = img.resize((self.win_w, self.win_h), Image.Resampling.BILINEAR)
            self._render_ms = (time.perf_counter() - t0) * 1000
            ok = self.painter.paint(img, wx, wy, alpha=alpha)
        except Exception:
            self._paint_error("paint failed:\n" + traceback.format_exc())
            return
        self._pix_key = key
        self.last_img = img
        # 记下这次为什么重绘（调 /api/debug 能看到，排查"为啥一直在刷"很有用）
        if self._prev_key is not None:
            self._paint_reason = [n for n, a, b in zip(self._KEY_NAMES, key, self._prev_key) if a != b]
        self._prev_key = key
        self._paint_ms = (time.perf_counter() - t0) * 1000
        if not ok:
            self._paint_error(f"UpdateLayeredWindow 返回失败 hwnd={self.hwnd}")

    def _paint_error(self, msg):
        # 分层窗口偶发失败（锁屏、切换桌面、DPI 重建）不应该刷屏日志
        now = time.time()
        if now - self._paint_err_ts > 20.0:
            self._paint_err_ts = now
            log(msg)

    def _backdrop_error(self):
        now = time.time()
        if now - self._bg_err_ts > 60.0:
            self._bg_err_ts = now
            log("backdrop grab failed:\n" + traceback.format_exc())

    # ---------- 主循环 ----------
    def _tick(self):
        try:
            now = time.perf_counter()
            dt = min(0.12, max(0.001, now - self._last_tick))
            self._last_tick = now
            wall = time.time()

            # 一次性操作提示：卡片稳定显示 2 秒后才出现 7 秒；每次启动给一次，
            # 用户一旦真的悬停过（说明已经会用了）就写进 prefs，以后不再打扰
            if not self._hint_seen and not self._hint_done:
                if not self._hint_until and self.shown_target and wall - self._shown_since > 2.0:
                    self._hint_until = wall + 7.0
                elif self._hint_until and wall > self._hint_until:
                    self._hint_done = True
                    self._hint_until = 0.0

            if self.show_flag.get("req"):
                self.show_flag["req"] = False
                self.controls.put("show")
            while not self.controls.empty():
                self._control(self.controls.get_nowait())
                if self._closing:
                    return
            self._publish_hud_state()

            if wall - self._t_data > 0.6:
                self._t_data = wall
                self._update_data()
            # 前台切换/目标窗口销毁由钩子即时通知；轮询只是钩子失效时的安全网
            if self.watcher.take()[0]:
                self._vis_dirty = True
                # 事件来的这一拍要立刻核对"目标还有没有可见窗口"，
                # 别让 0.25s 的缓存把"关掉 agent"的收起拖慢
                self._t_vis_win = 0.0
            if self.watcher.dead:
                dead = self.watcher.dead
                self.watcher.dead = []
                self._on_windows_dead(dead)
            if self._vis_dirty or wall - self._t_vis > VIS_POLL:
                self._vis_dirty = False
                self._t_vis = wall
                self._check_visibility()

            cx, cy = cursor_pos()
            prev_hover = (self.menu_index, self.hover_close)
            self._hover_update(cx, cy, wall)
            self._poll_hotkey(wall)
            changed = self._animate(dt)
            # 鼠标在菜单/关闭按钮上移动时没有动画在跑，必须单独触发重绘，
            # 否则高亮会停在上一次动画结束时的行（表现为"选中区域不跟手"）。
            if (self.menu_index, self.hover_close) != prev_hover:
                changed = True
            if self._anchor_dirty:
                self._anchor_dirty = False
                changed = True

            wx, wy = self._window_xy()
            # 明暗过渡途中不许重抓背景：换了 backdrop_seq 就得把两个端点各重建
            # 一次（~40ms），正好落在最不该掉帧的地方。过渡只有 0.24s，等它走完。
            fading = abs(self.dark - self.dark_target) > 0.001
            if self.shown_target and not fading and (
                    wall - self._t_bg > 1.2 or self._bg_pos != (wx, wy)):
                self._t_bg = wall
                self._refresh_backdrop()
                changed = True

            # 收起途中也要继续画（否则就没有淡出，只剩硬切）
            if changed and (self.shown_target or self.shown > 0.004):
                self._paint()
        except Exception:
            # 同一个异常每个 tick 都会来，10s 认一次就行，别把日志刷爆
            now = time.time()
            if now - self._tick_err_ts > 10.0:
                self._tick_err_ts = now
                log("tick error:\n" + traceback.format_exc())
        # Windows 的 Tk 定时器精度是 15.6ms：请求 8ms 会并到 15.6ms（≈60fps），
        # 请求 16ms 会落到 31.2ms（≈32Hz）。动画期间要 60fps，空转 32Hz 足够
        # ——而且不用去改系统定时器精度（timeBeginPeriod 会影响整机耗电）。
        anim = (abs(self.shown - (1.0 if self.shown_target else 0.0)) > 0.004
                or (not self._hold_expand and abs(self.expand - self.expand_want) > 0.004)
                or abs(self.dark - self.dark_target) > 0.004
                or self._pulse() > 0 or self._shine() >= 0)
        delay = 8 if anim else IDLE_TICK
        # 淡出尾巴上 shown 已经小到看不见但还没到 0 时同样走 anim 的 15.6ms，
        # 不会掉到慢节拍，否则窗口会在"已经看不见了"之后还多活一拍才 SW_HIDE。
        self.after(delay, self._tick)

    def _animate(self, dt):
        now = time.perf_counter()
        changed = False
        # 悬停展开
        want = 1.0 if (self._inside and not self.menu_open
                       and self.expand_want > 0) else 0.0
        if not self._hold_expand and abs(self.expand - want) > 0.002:
            rate = 13.0 if want > self.expand else 16.0
            ne = self.expand + (want - self.expand) * min(1.0, dt * rate)
            if abs(ne - want) < 0.004:
                ne = want
            self.expand = max(0.0, min(1.0, ne))
            changed = True
        # 主题：按时间做 smoothstep，和帧率无关；端点各缓存一张卡，中间只混合。
        # （原来是每帧指数逼近 9.0，再按 21 个灰阶量化重绘 → 一格一格跳。）
        if self.dark != self.dark_target:
            dur = DARK_DUR
            p = (now - self._dark_t0) / dur if dur > 0 else 1.0
            p = max(0.0, min(1.0, p))
            eased = p * p * (3.0 - 2.0 * p)
            self.dark = self._dark_from + (self.dark_target - self._dark_from) * eased
            changed = True
            if p >= 1.0:
                self.dark = self.dark_target
        # 显隐淡入淡出：按经过时间求缓动，和帧率无关。
        # （原来是"每帧指数逼近"，帧间隔一过 33ms 就会一帧塌到 0，等于硬切，
        #   所以收起反而比最小化还生硬。）
        sh_target = 1.0 if self.shown_target else 0.0
        if self.shown != sh_target:
            dur = FADE_IN if sh_target > 0 else FADE_OUT
            p = (now - self._fade_t0) / dur if dur > 0 else 1.0
            p = max(0.0, min(1.0, p))
            eased = 1.0 - (1.0 - p) ** 2      # 快起慢收：一按下就有反馈
            self.shown = self._fade_from + (sh_target - self._fade_from) * eased
            changed = True
            if p >= 1.0:
                self.shown = sh_target
                if sh_target == 0.0:
                    try:
                        _user32.ShowWindow(self.hwnd, SW_HIDE)
                    except Exception:
                        pass
        if self._pulse() > 0 or self._shine() >= 0:
            changed = True
        return changed

    def _hover_update(self, cx, cy, wall):
        inside = self.shown_target and self.shown > 0.2 and self._hit_card(cx, cy)
        if inside and not self._inside:
            self._inside = True
            self._inside_since = wall
            self._outside_since = 0.0
            self.expand_want = 0.0
            # 注意：悬停期间**绝不改 anchor**。展开方向一变，卡片会整块滑动，
            # ✕ 就从鼠标底下挪走（想点 ✕ 的用户下一个动作必然落空）。
        elif not inside and self._inside:
            self._inside = False
            self._outside_since = wall

        # 悬停本身不抢点击：光标"扫过"卡片时保持穿透，只有停下来、正在拖动
        # 或开着菜单时才临时接管鼠标，这样就扫不飞背后窗口的点击了。
        through_pref = bool(self.prefs.get("through", True))
        dwell = (wall - self._inside_since) if self._inside_since else 0.0
        close_hit = inside and self._hit_close(cx, cy)
        # 底部锚定会让顶边在展开时上移；靠近关闭按钮时暂停动画，保持目标在鼠标下。
        self._hold_expand = bool(self.anchor == "bottom" and inside and not self.menu_open
                                 and self._near_close(cx, cy))
        # 光标已经停在 ✕（甚至只是逼近）上就必须马上接管鼠标：用户是看到 ✕ 才点的，
        # 再等 dwell 会让第一次点击直接穿到下层窗口（表现为"点了没反应"）。
        self._arm((not through_pref) or self.menu_open or self._dragging
                  or close_hit or (inside and (self._near_close(cx, cy) or dwell > 0.12)))

        # 会停下来悬停了，说明操作提示已经起作用，以后不必再出现
        if not self._hint_seen and inside and dwell > 0.5:
            self._hint_seen = True
            self._hint_until = 0.0
            self.prefs["hint_seen"] = True
            save_prefs(self.prefs)

        if self._inside and not self.menu_open:
            if wall - self._inside_since > 0.11:
                self.expand_want = 1.0
        elif not self._inside:
            if self._outside_since and wall - self._outside_since > 0.40:
                self.expand_want = 0.0

        self.hover_close = close_hit
        if self.menu_open:
            self.menu_index = self._hit_menu_row(cx, cy)
            if not inside and self._outside_since and wall - self._outside_since > 0.55:
                self._close_menu()
        else:
            self.menu_index = -1

    def _fg_desc(self, fg=None):
        """前台窗口的 (exe 名, 标题)。诊断日志用。"""
        try:
            if fg is None:
                fg = foreground_window()
            pid = wintypes.DWORD()
            _user32.GetWindowThreadProcessId(fg, ctypes.byref(pid))
            n = _user32.GetWindowTextLengthW(fg)
            title = ""
            if n > 0:
                buf = ctypes.create_unicode_buffer(n + 1)
                _user32.GetWindowTextW(fg, buf, n + 1)
                title = buf.value
            exe = process_exe(pid.value) or "?"
            return os.path.basename(exe), title
        except Exception:
            return "?", ""

    def _vis_trace(self, reason):
        """显隐切换时记一行现场：前台是谁、判定依据是什么。

        "点了关闭 agent 浮窗却不走"这类问题，光看代码猜不出来，得看这一行。
        """
        now = time.time()
        if now - self._vis_log_ts < 0.05:
            return
        self._vis_log_ts = now
        exe, title = self._fg_desc()
        log("vis %s: exe=%s title=%r matched=%s win=%s inside=%s menu=%s "
            "drag=%s user_hidden=%s fg=%s"
            % (reason, exe, title[:30], self._was_matched, self._vis_win,
               self._inside, self.menu_open, self._dragging, self._user_hidden,
               time.strftime("%H:%M:%S")))

    def _vis_live(self, fg_now, matched):
        """还在显示时每秒记一行：到底凭什么继续显示。"""
        exe, title = self._fg_desc(fg_now)
        log("vis live: exe=%s title=%r matched=%s win=%s fgwin=%s fg=%s"
            % (exe, title[:30], matched, self._vis_win, fg_now,
               time.strftime("%H:%M:%S")))

    def _on_windows_dead(self, dead):
        """有顶层窗口被销毁。

        如果被销毁的正是"我们正在跟随的那个前台窗口"，说明用户刚刚关掉了 agent：
        立刻收起（不等下一个 tick 的前台判定，也不等 HIDE_SETTLE）。
        Windows 会把前台交给"下面那个窗口"——同时开着两个 agent 时，那个窗口往往
        也是跟随目标，于是 matched 依旧为真、卡片又飘回来。所以这里钉一个 hold：
        只要前台还停在这个回退窗口上，就不再自动浮出来；用户切到别的窗口才解除。
        """
        if not self._fg_win or not self.shown_target:
            return
        fg = self._fg_win
        try:
            root = _user32.GetAncestor(fg, 2) or fg      # GA_ROOT
        except Exception:
            root = fg
        if fg not in dead and root not in dead:
            return
        try:
            now_fg = foreground_window()
        except Exception:
            now_fg = 0
        exe, title = self._fg_desc(now_fg)
        log("vis dead: watched=%s dead=%s now exe=%s title=%r"
            % (fg, dead[:4], exe, title[:30]))
        self._fg_hold = 0
        # 别立刻拿"现在的前台"当回退目标：这一瞬间 Windows 还在重排 z 序，
        # 先让它稳定 0.25s，那段窗口期由 _check_visibility 负责一直收着。
        self._fg_settle = time.time() + 0.25
        self._fg_win = 0
        self._hide_since = 0.0
        self._hide_fast = False
        self._set_shown(False)

    def _check_visibility(self):
        wall = time.time()
        # 窗口被拖到另一块 DPI 不同的显示器上时，分层窗口不会自己重排，
        # 会变得又糊又偏。这里低频轮询一下，变了就整体重建。
        if wall - self._t_dpi > 1.0:
            self._t_dpi = wall
            try:
                dpi_scale = get_dpi_for_window(self.hwnd) / 96.0
            except Exception:
                dpi_scale = self.scale
            if dpi_scale > 0.5 and abs(dpi_scale - self.scale) > 0.02:
                self._apply_scale(dpi_scale)
            # 拔掉显示器/改分辨率后，卡片可能整张留在屏幕外再也点不到：拉回来
            try:
                new_pos = self._clamp_pos(self.pos_x, self.pos_y)
                if new_pos != (self.pos_x, self.pos_y):
                    self.pos_x, self.pos_y = new_pos
                    self.anchor = self._anchor_for()
                    self._pix_key = None
                    self._bg_ts = 0.0
                    self._anchor_dirty = True
                    self._refresh_backdrop(force=True)
                    log(f"card off-screen, moved back to ({self.pos_x}, {self.pos_y})")
            except Exception:
                pass
        try:
            fg_now = foreground_window()
        except Exception:
            fg_now = 0
        if fg_now != self._last_fg:
            self._last_fg = fg_now
            self._theme_lums.clear()
            self._t_bg = 0.0
            if self._peek_fg and fg_now != self._peek_fg:
                self._peek_fg = 0  # 热键/接口唤出的浮窗，只要没切走就一直留着

        # 关掉 agent 的瞬间，Windows 会把前台"塌"给下面那个窗口，可能先落到本程序
        # 自己身上再换人（z 序在动）。这 0.25s 内一律当"还没定"，先收着别动，
        # 等前台稳定下来再决定要不要重新浮出。
        if self._fg_settle:
            if wall < self._fg_settle:
                if fg_now and self.hwnd and fg_now != int(self.hwnd):
                    self._fg_hold = fg_now
                self._set_shown(False)
                return
            self._fg_settle = 0.0
            if fg_now and self.hwnd and fg_now != int(self.hwnd):
                self._fg_hold = fg_now

        # 前台还停在"关窗后回退的那个窗口"上：保持收起，别飘回来
        if self._fg_hold:
            if fg_now == self._fg_hold:
                self._set_shown(False)
                return
            self._fg_hold = 0

        # 前台是我们自己（启动/贴边时的一瞬）：这时"有没有 agent 在前台"无从判断，
        # 保持现状最安全——既不会刚出来就收回去，也不会把自己当成目标永远赖着。
        own_fg = bool(fg_now and self.hwnd and fg_now == int(self.hwnd))

        mode = normalize_follow(self.prefs.get("follow", "both"))
        matchers = FOLLOW_MATCHERS.get(mode)
        matched = foreground_matches(matchers) if matchers else True
        prev_matched = self._was_matched
        self._was_matched = matched
        if matched:
            # 记住"正在跟随的那个前台窗口"：它一旦被销毁，就是用户关掉了 agent
            self._fg_win = fg_now

        # 刚关掉 agent：Windows 把前台塞给了下面那个窗口（可能也是 agent）。
        # 在用户主动切到别的窗口之前，保持收起——否则会"刚收起又浮出来"。
        if self._fg_hold:
            if fg_now == self._fg_hold:
                self._set_shown(False)
                return
            self._fg_hold = 0
        if VIS_LOG and self.shown_target and wall - self._vis_live_ts > 1.0:
            self._vis_live_ts = wall
            self._vis_live(fg_now, matched)

        if self._user_hidden:
            # X 明确表示收起；托盘、快捷方式、DSH 面板和热键均可恢复。
            self._set_shown(False)
            return

        # 正在悬停/拖拽/操作菜单时不要因为前台切走而突然消失
        if self._inside or self.menu_open or self._dragging:
            self._set_shown(True)
            return
        if self._peek_fg and fg_now == self._peek_fg:
            self._set_shown(True)
            return

        want = self.shown_target if own_fg else matched
        if want and matchers:
            # 前台还挂在目标进程上，不代表 agent 还开着：Electron 关掉主窗口后
            # 进程往往还活着，残留的窗口能继续"占着"前台，于是 matched 一直 True，
            # 卡片就赖着不走 —— 用户体感就是"点了 X 不消失，随便点一下别处才走"。
            # 这里定期核对"到底还有没有属于目标的可见窗口"，没有就立刻收。
            if wall - self._t_vis_win > 0.25:
                self._t_vis_win = wall
                try:
                    self._vis_win = app_visible(matchers)
                except Exception:
                    self._vis_win = True
            if self._vis_win is None:
                want = False
        if want and foreground_is_fullscreen(self.hwnd):
            want = False
        if want:
            self._hide_since = 0.0
            self._set_shown(True)
            return
        if not self.shown_target:
            return
        # 收起判定分两种，别一律等：
        #  · 目标窗口已经没了（关掉 agent）或全部最小化 —— 没有歧义，立刻收；
        #  · 目标还在、只是焦点被别的窗口抢走 —— 留一拍 HIDE_SETTLE 防闪跳
        #    （切输入法/点系统托盘时会有一瞬间的前台闪动）。
        # 关 agent 是最常见的收起场景，原来在这里无脑等 0.35s，最刺眼。
        if not self._hide_since:
            self._hide_since = wall
            if self._vis_win is None:
                # 刚才那轮核对已经确认"一个可见窗口都没有了"（关掉了）
                self._hide_fast = True
            else:
                try:
                    vis = app_visible(matchers) if matchers else True
                except Exception:
                    vis = True
                self._vis_win = vis
                self._hide_fast = vis is not True
        if wall - self._hide_since >= (0.0 if self._hide_fast else HIDE_SETTLE):
            self._hide_since = 0.0
            self._hide_fast = False
            self._set_shown(False)

    def _set_shown(self, want):
        if want == self.shown_target:
            return
        self._vis_trace("show" if want else "hide")
        self.shown_target = want
        # 记下淡入淡出的起点：过渡按时间走，不再依赖帧间隔
        self._fade_from = self.shown
        self._fade_t0 = time.perf_counter()
        if want:
            self._shown_since = time.time()
            # 从隐藏状态重新出现时卡片一定是折叠的：别让上一次收起残留的
            # 展开进度把卡片"弹"出来
            if not self._inside and self.expand < 0.35:
                self.expand = 0.0
                self.expand_want = 0.0
            try:
                _user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE)
                _user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                                     SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
            except Exception:
                pass
            self._t_bg = 0.0
            self._refresh_backdrop(force=True)
        self._paint()

    # ---------- 交互 ----------
    def _local_units(self, e):
        return e.x / self.scale, e.y / self.scale

    def _on_click(self, e):
        if self._quit_armed_until and time.time() > self._quit_armed_until:
            self._quit_armed_until = 0.0
        cx, cy = int(e.x_root), int(e.y_root)
        close = bool(self._hit_close(cx, cy))
        # 点一次记一行：以后"点了没反应"能直接看日志定位是没收到、还是判错了位置
        self._last_click = {"t": round(time.time() % 10000.0, 3), "x": cx, "y": cy,
                            "close": close, "menu": bool(self.menu_open),
                            "card": [round(v) for v in self._card_rect()],
                            "through": not self._ct_off}
        log(f"click ({cx},{cy}) close={close} card={self._last_click['card']} "
            f"through={self._last_click['through']} shown={self.shown:.2f}")
        if self.menu_open:
            row = self._hit_menu_row(cx, cy)
            if row >= 0:
                self._menu_action(row)
            else:
                self._close_menu()
            return
        if close:
            self._hide_by_user()
            return
        # 按下不等于拖动：先记起点，真的挪动超过 3px 才开始搬卡片。
        # 否则"想点 ✕ 却点空"会变成把卡片轻轻拖走，看起来就是"点了没反应"。
        self._dragging = True
        self._drag_moved = False
        self._press_x, self._press_y = cx, cy
        self._drag_dx = cx - self.pos_x
        self._drag_dy = cy - self.pos_y

    def _on_drag(self, e):
        if not self._dragging:
            return
        if not self._drag_moved:
            # 阈值跟 Windows 自己的拖动判定对齐（SM_CXDRAG/SM_CYDRAG = 4）：
            # 手抖 2~3 像素不该把卡片蹭走，超过 4px 才算真的要拖。
            if abs(e.x_root - self._press_x) <= 4 and abs(e.y_root - self._press_y) <= 4:
                return
            self._drag_moved = True
        x, y = self._clamp_pos(e.x_root - self._drag_dx, e.y_root - self._drag_dy)
        if (x, y) != (self.pos_x, self.pos_y):
            self.pos_x, self.pos_y = x, y
            new_anchor = self._anchor_for()
            if new_anchor != self.anchor:
                self.anchor = new_anchor
            now = time.perf_counter()
            if now - self._last_drag_paint > 0.02:
                self._last_drag_paint = now
                self._t_bg = 0.0
                self._refresh_backdrop()
                self._paint()

    def _on_release(self, e):
        if self._dragging:
            moved = self._drag_moved
            self._dragging = False
            self._drag_moved = False
            if moved:
                self.prefs["x"], self.prefs["y"] = self.pos_x, self.pos_y
                save_prefs(self.prefs)

    def _on_right(self, e):
        if self.menu_open:
            self._close_menu()
            return
        self._open_menu()

    def _on_double(self, e):
        if not self.menu_open:
            self._open_dashboard()

    # ---------- 菜单 ----------
    def _menu_items(self):
        follow = normalize_follow(self.prefs.get("follow", "both"))
        through = bool(self.prefs.get("through", True))
        glass = int(round(float(self.prefs.get("glass", 1.0)) * 100))
        armed = time.time() < self._quit_armed_until
        if self.glass_menu:
            rows = []
            for pct in (100, 90, 80, 70, 60, 50):
                rows.append({"key": f"glass_{pct}", "label": f"不透明度 {pct}%",
                             "value": "当前" if abs(glass - pct) <= 1 else ""})
            rows.append({"key": "glass_back", "label": "返回", "value": "", "sep": True})
            return rows
        return [
            {"key": "panel", "label": "打开监控面板", "arrow": True},
            {"key": "cal", "label": "余额校准 / 同步…", "arrow": True},
            {"key": "follow", "label": "跟随", "value": FOLLOW_LABEL[follow], "sep": True},
            {"key": "through", "label": "点击穿透", "value": "开" if through else "关"},
            {"key": "glass", "label": "不透明度", "value": f"{glass}%  >"},
            {"key": "hide", "label": "收起浮窗" if self.plugin_mode else "收起到托盘",
             "value": "" if self.plugin_mode else "Ctrl+Alt+B", "sep": True},
            {"key": "quit", "label": "确认退出并停止代理？" if armed else "退出监控",
             "danger": True, "sep": True},
        ]

    def _open_menu(self):
        self.menu_open = True
        self.glass_menu = False
        self.expand_want = 0.0
        self.menu_index = -1
        self.menu_items = self._menu_items()
        if not self._hint_seen:      # 都用上右键菜单了，操作提示可以退休了
            self._hint_seen = True
            self._hint_until = 0.0
            self.prefs["hint_seen"] = True
            save_prefs(self.prefs)
        self._arm(True)
        # 方向只在"当前这侧真装不下菜单"时才翻，而且优先翻到**装得下**的那一侧：
        # 悬停展开刚定好的方向不要被右键菜单再翻一次，否则卡片会在屏幕上跳。
        keep = self.anchor
        l, t, r, b = work_area_at(int(self.pos_x + W * self.scale / 2),
                                  int(self.pos_y + H * self.scale / 2))
        need = MAX_H * self.scale
        extra = (MAX_H - H) * self.scale      # 向上长时额外占用的高度
        if keep == "top":
            if self.pos_y + need > b and self.pos_y - extra >= t:
                keep = "bottom"
        elif self.pos_y - extra < t and self.pos_y + need <= b:
            keep = "top"
        self._set_anchor(keep)
        self._refresh_backdrop()
        self._paint()

    def _close_menu(self):
        if not self.menu_open:
            return
        self.menu_open = False
        self.glass_menu = False
        self.menu_index = -1
        self._quit_armed_until = 0.0
        if not self._inside and self.prefs.get("through", True):
            self._arm(False)
        self._refresh_backdrop()
        self._paint()

    def _menu_action(self, row):
        if row >= len(self.menu_items):
            return
        key = self.menu_items[row].get("key")
        if key == "panel":
            self._open_dashboard()
            self._close_menu()
        elif key == "cal":
            self._open_dashboard("#cal")
            self._close_menu()
        elif key == "follow":
            cur = normalize_follow(self.prefs.get("follow", "both"))
            nxt = FOLLOW_ORDER[(FOLLOW_ORDER.index(cur) + 1) % len(FOLLOW_ORDER)]
            self.prefs["follow"] = nxt
            save_prefs(self.prefs)
            self.menu_items = self._menu_items()
            self._paint()
            self._t_vis = 0.0  # 立刻按新规则显隐
        elif key == "through":
            self.prefs["through"] = not bool(self.prefs.get("through", True))
            save_prefs(self.prefs)
            # 关掉"点击穿透"就等于常驻接管鼠标；打开它则立刻恢复穿透
            self._arm(not self.prefs["through"])
            self.menu_items = self._menu_items()
            self._paint()
        elif key == "glass":
            self.glass_menu = True
            self.menu_items = self._menu_items()
            self._paint()
        elif key == "glass_back":
            self.glass_menu = False
            self.menu_items = self._menu_items()
            self._paint()
        elif key.startswith("glass_"):
            try:
                pct = int(key.split("_", 1)[1])
            except ValueError:
                return
            self.prefs["glass"] = max(0.3, min(1.0, pct / 100.0))
            save_prefs(self.prefs)
            self.menu_items = self._menu_items()
            self._paint()
        elif key == "hide":
            self._close_menu()
            self._hide_by_user()
        elif key == "quit":
            if time.time() < self._quit_armed_until:
                self._quit_now()
            else:
                self._quit_armed_until = time.time() + 4.0
                self.menu_items = self._menu_items()
                self._paint()

    def _open_dashboard(self, frag=""):
        try:
            webbrowser.open(f"http://{LISTEN_HOST}:{self.port}/{frag}")
        except Exception:
            pass

    def _hide_by_user(self):
        self._user_hidden = True
        self._hidden_armed = False
        self._hidden_since = time.time()
        self._peek_fg = 0
        log("hidden by user (%s)" % ("menu" if self.menu_open else "click"))
        self._set_shown(False)
        if self.tray:
            self.tray.notify_hidden()

    def _poll_hotkey(self, wall):
        """Ctrl+Alt+B 全局显隐：用 GetAsyncKeyState 轮询，不注册系统热键，不会抢别的程序。"""
        if self.plugin_mode:
            return
        if wall - self._t_hot < 0.09:
            return
        self._t_hot = wall
        try:
            down = bool((_user32.GetAsyncKeyState(0x11) & 0x8000)
                        and (_user32.GetAsyncKeyState(0x12) & 0x8000)
                        and (_user32.GetAsyncKeyState(0x42) & 0x8000))
        except Exception:
            down = False
        if down and not self._hot_down:
            self._control("toggle")
        self._hot_down = down

    def _quit_now(self):
        if self._closing:
            return
        self._closing = True
        log("service exit requested")
        if self.tray:
            self.tray.close()
        try:
            self.prefs["x"], self.prefs["y"] = self.pos_x, self.pos_y
            save_prefs(self.prefs)
        except Exception:
            pass
        try:
            if self.store:
                self.store.save()
        except Exception:
            pass
        try:
            if self.service:
                self.service.stop()
        except Exception:
            pass
        try:
            self.painter.close()
            self.grabber.close()
        except Exception:
            pass
        try:
            self.watcher.stop()
        except Exception:
            pass
        try:
            self.destroy()
        except Exception:
            pass



# ---------------------------------------------------------------- 网页面板
DASHBOARD = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="hud-token" content="__HUD_TOKEN__">
<title>百炼浮窗 · 监控面板</title>
<style>
  :root{--bg:#eef1f6;--card:rgba(255,255,255,.78);--line:rgba(18,22,30,.09);
        --txt:#171a20;--sub:#5c6472;--faint:#98a0ac;--accent:#215deb;--accent2:#5c9cff;
        --green:#0a9652;--warn:#c67c0a;--red:#cd2e2e}
  *{box-sizing:border-box}
  body{margin:0;min-height:100vh;background:
    radial-gradient(900px 500px at 8% -8%,#dfe8fb 0,transparent 60%),
    radial-gradient(800px 500px at 96% 4%,#f7e6f1 0,transparent 55%),
    radial-gradient(700px 600px at 40% 110%,#e2f5ec 0,transparent 60%),
    var(--bg);
    color:var(--txt);font:14px/1.55 "Microsoft YaHei UI","Segoe UI",system-ui,sans-serif;padding:30px 22px 60px}
  .wrap{max-width:880px;margin:0 auto}
  header{display:flex;align-items:center;gap:10px;margin-bottom:18px;font-size:15px;font-weight:600}
  header .sub{font-weight:400;color:var(--faint);font-size:12px}
  .pill{margin-left:auto;font-size:12px;font-weight:500;padding:4px 11px;border-radius:999px;
        background:rgba(10,150,82,.10);color:var(--green);display:flex;align-items:center;gap:6px}
  .pill.stale{background:rgba(198,124,10,.12);color:var(--warn)}
  .pill.est{background:rgba(198,124,10,.12);color:var(--warn)}
  .dot{width:7px;height:7px;border-radius:50%;background:currentColor}
  .live .dot{box-shadow:0 0 0 3px rgba(10,150,82,.18)}
  .card{background:var(--card);backdrop-filter:blur(18px);border:1px solid var(--line);
        border-radius:20px;box-shadow:0 12px 34px rgba(24,38,72,.07);padding:22px 24px}
  .hero{display:flex;align-items:flex-end;gap:14px;flex-wrap:wrap}
  .hero .bal{font-size:44px;font-weight:600;letter-spacing:-1.4px;font-variant-numeric:tabular-nums}
  .hero .unit{font-size:13px;color:var(--faint);margin-bottom:9px}
  .hero .right{margin-left:auto;text-align:right;font-size:12.5px;color:var(--sub)}
  .bar{height:7px;border-radius:4px;background:rgba(18,22,30,.07);margin:18px 0 0;overflow:hidden}
  .bar>i{display:block;height:100%;width:0;border-radius:4px;
         background:linear-gradient(90deg,var(--accent),var(--accent2));transition:width .5s cubic-bezier(.2,.8,.2,1)}
  .meta{display:flex;gap:8px;align-items:center;flex-wrap:wrap;font-size:12.5px;color:var(--sub);margin-top:12px}
  .tag{padding:2px 9px;border-radius:999px;border:1px solid var(--line);font-size:12px}
  .tag.peak{color:var(--warn);border-color:rgba(198,124,10,.35);background:rgba(198,124,10,.08)}
  .tag.off{color:var(--green);border-color:rgba(10,150,82,.3);background:rgba(10,150,82,.07)}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-top:14px}
  .mini{padding:16px 18px}
  .mini .k{font-size:11.5px;color:var(--faint);letter-spacing:.4px}
  .mini .v{font-size:23px;font-weight:600;margin-top:5px;font-variant-numeric:tabular-nums}
  .mini .s{font-size:11.5px;color:var(--sub);margin-top:3px}
  h2{font-size:12.5px;font-weight:600;color:var(--faint);letter-spacing:.6px;margin:26px 0 10px}
  .chart{display:flex;align-items:flex-end;gap:5px;height:96px;padding:6px 2px 0}
  .chart .col{flex:1;display:flex;flex-direction:column;justify-content:flex-end;align-items:center;gap:6px;height:100%}
  .chart .b{width:100%;border-radius:4px 4px 2px 2px;background:linear-gradient(180deg,var(--accent2),var(--accent));
            opacity:.85;min-height:2px;transition:height .4s}
  .chart .lb{font-size:9.5px;color:var(--faint);white-space:nowrap}
  table{width:100%;border-collapse:collapse;font-size:12.5px}
  th{text-align:left;font-weight:500;color:var(--faint);padding:6px 8px;border-bottom:1px solid var(--line);white-space:nowrap}
  td{padding:6px 8px;border-bottom:1px solid rgba(18,22,30,.05);white-space:nowrap}
  .mono{font-variant-numeric:tabular-nums}
  .muted{color:var(--faint)}
  .form{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
  input{font:inherit;padding:9px 12px;border-radius:10px;border:1px solid var(--line);
        background:rgba(255,255,255,.9);width:170px;outline:none}
  input:focus{border-color:var(--accent);box-shadow:0 0 0 3px rgba(33,93,235,.12)}
  button{font:inherit;padding:9px 16px;border-radius:10px;border:0;background:var(--accent);color:#fff;
         cursor:pointer;font-weight:500}
  button:hover{filter:brightness(1.06)}
  .hint{font-size:12px;color:var(--faint);margin-top:8px}
  .err{color:var(--red)}
  .ok{color:var(--green)}
  footer{margin-top:26px;text-align:center;font-size:11.5px;color:var(--faint)}
  @media (max-width:560px){.hero .bal{font-size:34px}}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <span>百炼浮窗</span><span class="sub" id="ver">v2.2</span>
    <span class="pill" id="status"><span class="dot"></span><span id="statusText">连接中…</span></span>
  </header>

  <div class="card">
    <div class="hero">
      <div class="bal" id="remain">--</div>
      <div class="unit" id="face">/ 300</div>
      <div class="right">
        <div id="clock">--:--:--</div>
        <div class="muted" id="source">余额来源：估算</div>
      </div>
    </div>
    <div class="bar"><i id="pbar"></i></div>
    <div class="meta">
      <span class="tag" id="mode">--</span>
      <span id="switch">--</span>
      <span class="muted">·</span>
      <span id="today">今日 --</span>
      <span class="muted" id="ext"></span>
    </div>
  </div>

  <div class="grid">
    <div class="card mini"><div class="k">今日代理消费</div><div class="v mono" id="todayCost">--</div><div class="s" id="todayTok">--</div></div>
    <div class="card mini"><div class="k">代理缓存命中率</div><div class="v mono" id="hit">--</div><div class="s" id="saved">--</div></div>
    <div class="card mini"><div class="k">代理累计请求</div><div class="v mono" id="count">--</div><div class="s" id="tok">--</div></div>
    <div class="card mini"><div class="k">按代理消费速度</div><div class="v mono" id="days">--</div><div class="s" id="burn">--</div></div>
  </div>

  <h2>近 12 小时代理消费</h2>
  <div class="card"><div class="chart" id="chart"></div></div>

  <h2 id="cal">余额校准 / 同步</h2>
  <div class="card">
    <div class="form">
      <button id="syncBtn">打开阿里云卡券页并同步</button>
      <span id="syncMsg" class="hint"></span>
    </div>
    <div class="form" style="margin-top:10px">
      <input id="calInput" type="number" step="0.01" placeholder="卡券页真实余额，如 231.57">
      <input id="faceInput" type="number" step="0.01" placeholder="券面值（可选，默认 300）">
      <button id="calBtn">手动写入</button>
      <span id="calMsg" class="hint"></span>
    </div>
    <div class="hint" id="syncAge">正在读取最近一次同步时间…</div>
    <div class="hint">浮窗余额 = 校准点余额 − 之后新增的估算消费。装了油猴脚本（balance-sync.user.js）后，打开卡券页会每 20 秒自动推送真实余额；点上面的按钮打开卡券页，几秒内即可同步。</div>
  </div>

  <h2>最近请求</h2>
  <div class="card" style="padding:10px 14px">
    <table><thead><tr><th>时间</th><th>模型</th><th>未命中</th><th>命中</th><th>输出</th><th>档位</th><th>费用</th></tr></thead>
    <tbody id="rows"><tr><td colspan="7" class="muted">暂无调用</td></tr></tbody></table>
  </div>

  <h2>按模型汇总</h2>
  <div class="card" style="padding:10px 14px">
    <table><thead><tr><th>模型</th><th>请求</th><th>输入</th><th>命中</th><th>输出</th><th>命中率</th><th>消费</th></tr></thead>
    <tbody id="models"><tr><td colspan="7" class="muted">暂无数据</td></tr></tbody></table>
  </div>

  <footer id="proxyAddress"></footer>
</div>

<script nonce="__CSP_NONCE__">
const $ = id => document.getElementById(id);
$('proxyAddress').textContent = '本地代理 ' + location.origin;
const HUD_TOKEN = (document.querySelector('meta[name="hud-token"]') || {}).content || '';
const hudFetch = (url, opts = {}) => {
  const headers = Object.assign({}, opts.headers || {});
  if (HUD_TOKEN) headers['X-HUD-Token'] = HUD_TOKEN;
  return fetch(url, Object.assign({}, opts, {headers}));
};
const esc = s => String(s).replace(/[&<>"']/g,
  ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
const money = v => '¥' + (Number(v) || 0).toFixed(2);
const money4 = v => '¥' + (Number(v) || 0).toFixed(4);
const tok = n => { n = Number(n) || 0;
  return n >= 1e9 ? (n/1e9).toFixed(2)+'B' : n >= 1e6 ? (n/1e6).toFixed(1)+'M'
       : n >= 1e3 ? Math.round(n/1e3)+'K' : String(Math.round(n)); };
const pad = n => String(n).padStart(2,'0');
const hms = s => { s = Math.max(0, Math.floor(s)); return pad(Math.floor(s/3600)) + ':' + pad(Math.floor(s%3600/60)) + ':' + pad(s%60); };
const ageText = s => s == null ? '' : s < 10 ? '刚刚' : s < 60 ? Math.round(s)+' 秒前'
  : s < 3600 ? Math.round(s/60)+' 分钟前' : s < 86400 ? Math.round(s/3600)+' 小时前' : Math.round(s/86400)+' 天前';

let lastStats = null;

async function tick(){
  let d;
  try { d = await (await fetch('/api/stats', {cache:'no-store'})).json(); }
  catch (e) { $('statusText').textContent = '连接中断'; $('status').className = 'pill est'; return; }
  if (!d || d.ok === false || !d.today || !d.totals) {
    $('statusText').textContent = '数据异常'; $('status').className = 'pill est'; return;
  }
  lastStats = d;
  const sa = $('syncAge'); if (sa) sa.textContent = syncAgeText(d);

  $('ver').textContent = 'v' + (d.version || '2.4');
  $('clock').textContent = d.now;
  $('remain').textContent = d.balance_available ? money(d.remain) : '--';
  $('remain').style.color = !d.balance_available ? 'var(--txt)' : d.remain < 30 ? 'var(--red)' : d.remain < 100 ? 'var(--warn)' : 'var(--txt)';
  $('face').textContent = '/ ' + Number(d.face).toFixed(0);
  $('pbar').style.width = Math.max(0, Math.min(100, d.pct_remain)).toFixed(1) + '%';

  const st = d.status;
  const kindText = d.balance_kind === 'bss' ? 'BSS 自动同步'
    : d.balance_kind === 'push' ? '卡券页推送'
    : d.balance_kind === 'manual' ? '手动校准'
    : d.balance_kind === 'seed' ? '首次启动锚点'
    : (st === 'estimate' ? '本地估算' : '上次真实同步');
  const unpriced = Number((d.totals && d.totals.unpriced_count) || 0);
  const age = ageText(d.balance_age);
  const synced = d.synced_remain != null
    ? `上次同步 ¥${Number(d.synced_remain).toFixed(2)}（${age}）` : '';
  const estSpend = Number(d.estimated_spend || 0);
  const estText = estSpend > 0.004 ? ` − 本地估算 ¥${estSpend.toFixed(2)}` : '';
  $('status').className = 'pill ' + (st === 'live' ? 'live' : st === 'stale' ? 'stale' : 'est');
  $('statusText').textContent = st === 'live'
    ? (d.balance_kind === 'bss' ? `BSS · ${age}`
       : d.balance_kind === 'manual' ? `已校准 · ${age}` : '余额实时')
    : st === 'stale' ? (d.balance_sync_issue === 'unavailable' ? '券不可用 · '
       : d.balance_sync_issue === 'missing' ? '未找到券 · '
       : d.balance_sync_error ? '同步失败 · ' : '真实 · ') + age : st === 'unknown' ? '待同步' : '估算中';
  $('source').textContent = (st === 'unknown' ? '余额来源：尚未同步或校准' : st === 'estimate'
    ? `余额来源：${kindText}` + (d.balance_note ? ` · ${d.balance_note}` : '')
    : `余额来源：${kindText} · ${synced}${estText}`)
    + (unpriced > 0 ? ` · ${unpriced} 次未计价` : '');

  $('mode').textContent = d.mode;
  $('mode').className = 'tag ' + (d.peak ? 'peak' : 'off');
  $('switch').textContent = `${d.switch_at} 转${d.next_mode} · 还剩 ${hms(d.switch_in)}`;
  $('today').textContent = `今日代理 ${money(d.today.cost)} · ${tok(d.today.tokens)} tok`;
  $('ext').textContent = d.external_drop > 0.005 ? `· 外部渠道 -${money(d.external_drop)}` : '';

  $('todayCost').textContent = money(d.today.cost);
  $('todayTok').textContent = `${d.today.count} 次 · ${tok(d.today.tokens)} token`;
  $('hit').textContent = (d.hit_rate || 0).toFixed(1) + '%';
  $('saved').textContent = `相比全未命中省下 ${money(d.cache_saved)}`;
  $('count').textContent = d.totals.count;
  $('tok').textContent = `↓ ${tok(d.input_tokens)} · ↑ ${tok(d.output_tokens)}`;
  $('days').textContent = d.days_left == null ? '—' : (d.days_left < 1 ? '< 1 天' : Math.round(d.days_left) + ' 天');
  $('burn').textContent = d.burn_per_hour > 0.004 ? `约 ¥${d.burn_per_hour.toFixed(2)} / 小时` : '近 3 小时无消费';

  const chart = $('chart');
  const hs = d.hourly || [];
  const mx = Math.max(1e-9, ...hs.map(x => x[1]));
  chart.innerHTML = hs.map(([h, c], i) => {
    const dt = new Date(h * 3600 * 1000);
    const label = i === hs.length - 1 ? '现在' : pad(dt.getHours());
    const hgt = c <= 0 ? 2 : Math.max(3, Math.round(c / mx * 74));
    return `<div class="col"><div class="b" style="height:${hgt}px;opacity:${c<=0?0.12:(i===hs.length-1?1:0.55+0.4*c/mx)}"></div><div class="lb">${label}</div></div>`;
  }).join('');

  const rows = d.recent || [];
  $('rows').innerHTML = rows.length ? rows.slice(0, 12).map(r => {
    const totalIn = (r.input || 0) + (r.cached || 0) + (r.cache_write || 0);
    const rate = totalIn ? (r.cached || 0) / totalIn * 100 : 0;
    return `<tr>
      <td class="mono muted">${esc(r.ts)}</td>
      <td>${esc(r.model || '—')}</td>
      <td class="mono">${tok(r.input)}</td>
      <td class="mono">${r.cached ? tok(r.cached) : '<span class="muted">0</span>'}</td>
      <td class="mono">${tok(r.output)}</td>
      <td><span class="tag ${r.peak ? 'peak' : 'off'}" style="font-size:11px">${r.peak ? '忙' : '闲'}</span></td>
      <td class="mono">${r.cost == null ? '<span class="muted">未知</span>' : money4(r.cost)}</td>
    </tr>`;
  }).join('') : '<tr><td colspan="7" class="muted">暂无调用</td></tr>';

  const models = Object.entries(d.per_model || {}).sort((a, b) => (b[1].cost || 0) - (a[1].cost || 0));
  $('models').innerHTML = models.length ? models.map(([name, m]) => {
    const tin = (m.input || 0) + (m.cached || 0) + (m.cache_write || 0);
    const rate = tin ? (m.cached || 0) / tin * 100 : 0;
    return `<tr><td>${esc(name)}</td><td class="mono">${m.count}</td>
      <td class="mono">${tok(tin)}</td><td class="mono">${tok(m.cached)}</td>
      <td class="mono">${tok(m.output)}</td><td class="mono">${rate.toFixed(0)}%</td>
      <td class="mono">${money4(m.cost)}</td></tr>`;
  }).join('') : '<tr><td colspan="7" class="muted">暂无数据</td></tr>';
}

$('calBtn').onclick = async () => {
  const v = parseFloat($('calInput').value);
  const faceText = $('faceInput').value.trim();
  const face = faceText ? parseFloat(faceText) : null;
  const msg = $('calMsg');
  if (!isFinite(v) || v < 0) { msg.className = 'hint err'; msg.textContent = '请输入有效的余额数字'; return; }
  if (faceText && (!isFinite(face) || face <= 0)) { msg.className = 'hint err'; msg.textContent = '请输入有效的券面值'; return; }
  try {
    const r = await hudFetch('/api/calibrate', {method:'POST', headers:{'content-type':'application/json'},
                                            body: JSON.stringify({remain: v, face})});
    const j = await r.json();
    if (j.ok) { msg.className = 'hint ok'; msg.textContent = `已校准为 ¥${v.toFixed(2)}，消耗系数 ${Number(j.cost_factor).toFixed(2)}`; }
    else { msg.className = 'hint err'; msg.textContent = j.error || '校准失败'; }
  } catch (e) { msg.className = 'hint err'; msg.textContent = '校准失败：' + e; }
};

// 一键同步：打开卡券页，油猴脚本会读 DescribeCoupon 并 POST /api/balance
let watchSync = 0;
$('syncBtn').onclick = () => {
  const msg = $('syncMsg');
  msg.className = 'hint';
  msg.textContent = '已打开卡券页，等待同步…';
  window.open('https://billing-cost.console.aliyun.com/coupons/coupon', '_blank', 'noopener');
  const before = lastStats && lastStats.balance_ts;
  const t0 = Date.now();
  clearInterval(watchSync);
  watchSync = setInterval(() => {
    if (lastStats && lastStats.balance_ts && lastStats.balance_ts !== before
        && lastStats.balance_kind === 'push' && lastStats.status !== 'estimate') {
      clearInterval(watchSync);
      msg.className = 'hint ok';
      msg.textContent = `同步成功：¥${Number(lastStats.remain).toFixed(2)}`;
      $('calInput').value = Number(lastStats.remain).toFixed(2);
    } else if (Date.now() - t0 > 25000) {
      clearInterval(watchSync);
      msg.className = 'hint err';
      msg.textContent = '没收到推送：确认 Tampermonkey 已启用 balance-sync.user.js，并在卡券页停留 20 秒';
    }
  }, 1200);
};

function syncAgeText(s) {
  if (s && s.status === 'estimate' && s.balance_note) return s.balance_note;
  if (!s || !s.balance_ts) return '还没有同步或校准余额';
  const mins = Math.max(0, (Date.now() / 1000 - s.balance_ts) / 60);
  const when = new Date(s.balance_ts * 1000).toLocaleString('zh-CN', {hour12: false});
  const ago = mins < 1 ? '刚刚' : mins < 60 ? `${Math.round(mins)} 分钟前` : `${(mins / 60).toFixed(1)} 小时前`;
  const kind = s.balance_kind === 'bss' ? 'BSS 自动同步'
    : s.balance_kind === 'push' ? '卡券页推送'
    : s.balance_kind === 'manual' ? '手动校准'
    : s.balance_kind === 'seed' ? '首次启动锚点'
    : (s.status === 'estimate' ? '本地估算' : '上次真实同步');
  return `最近同步：${when}（${ago}）· 数据源 ${kind}`;
}

tick();
setInterval(tick, 2000);
</script>
</body>
</html>
"""


# ---------------------------------------------------------------- 启动
def request_control(command="show", port=None) -> bool:
    try:
        instance = discover(BASE_DIR, port)
        if not instance:
            return False
        token = ensure_api_token(load_prefs())
        return bool(local_json(instance["port"], f"/api/hud/{command}",
                               token=token, post=True).get("ok"))
    except Exception:
        return False



def request_show(port=None) -> bool:
    return request_control("show", port)



def _msgbox(text: str, title: str = APP_NAME, flags: int = 0x10):
    try:
        ctypes.windll.user32.MessageBoxW(None, text, title, flags)
    except Exception:
        try:
            print(text, file=sys.stderr)
        except Exception:
            pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=f"{APP_NAME} · 百炼代金券悬浮仪表")
    ap.add_argument("--port", type=int, default=None, help="固定代理端口")
    ap.add_argument("--preview", metavar="DIR", help="只渲染预览图，不开窗")
    ap.add_argument("--scale", type=float, default=1.0, help="预览用缩放")
    ap.add_argument("--demo", action="store_true", help="演示数据，不启动代理")
    ap.add_argument("--attach", action="store_true", help="只做显示端，连已有实例")
    ap.add_argument("--no-proxy", action="store_true", help="不监听端口（调试）")
    ap.add_argument("--show", action="store_true", help="唤出已运行的实例后退出")
    ap.add_argument("--hide", action="store_true", help="收起浮窗，服务继续运行")
    ap.add_argument("--quit", action="store_true", help="退出正在运行的服务")
    ap.add_argument("--background", action="store_true", help="自动启动时遵循前台跟随规则")
    ap.add_argument("--plugin", action="store_true", help="由 DSH 插件管理，不创建独立托盘")
    ap.add_argument("--parent-pid", type=int, default=0, help="DSH 宿主进程")
    ap.add_argument("--reset-pos", action="store_true", help="重置窗口位置")
    ap.add_argument("--face", type=float, default=None, help="券面值")
    args = ap.parse_args(argv)
    if args.plugin and args.parent_pid <= 0:
        ap.error("--plugin requires --parent-pid")

    if args.preview:
        out = run_preview(Path(args.preview), args.scale)
        for p in out:
            print(p)
        return 0

    if Image is None:
        _msgbox("缺少 Pillow，无法绘制毛玻璃界面。\n\n请在命令行执行：\n"
                "  python -m pip install pillow\n\n然后重新启动。")
        return 2

    try:
        BASE_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _msgbox(f"Cannot create data directory: {BASE_DIR}\n{exc}")
        return 2

    set_dpi_awareness()

    if args.show:
        return 0 if request_show(args.port) else 1
    if args.hide or args.quit:
        return 0 if request_control("quit" if args.quit else "hide", args.port) else 1

    running = discover(BASE_DIR, args.port) if not args.demo else None
    if not args.demo and not args.attach and running:
        if not args.background:
            if not request_show(running["port"]):
                _msgbox("监控服务已运行，但暂时无法唤出浮窗。请通过托盘或 DSH 的百炼监控入口重试。")
                return 1
        return 0

    if args.attach and not running:
        _msgbox("没有找到正在运行的实例。\n请从 DSH 插件入口或运行 BailianHUD.exe 启动。")
        return 1

    instance_lock = InstanceLock(BASE_DIR)
    try:
        acquired = args.demo or args.attach or instance_lock.acquire()
    except OSError:
        _msgbox(f"无法写入数据目录：\n{BASE_DIR}\n\n"
                "请使用当前用户可写的目录，再重新启动。")
        return 2
    if not acquired:
        # 并发启动时，第一个进程可能还在载入历史记录。
        for _ in range(40):
            if discover(BASE_DIR, args.port):
                if not args.background:
                    request_show(args.port)
                return 0
            time.sleep(0.1)
        _msgbox("百炼监控正在启动或退出，请稍后重试。")
        return 1
    port = running["port"] if args.attach else (args.port if args.port is not None else
                                               (PLUGIN_PORT if args.plugin else LISTEN_PORT))
    instance_id = uuid.uuid4().hex

    first_prefs = not (BASE_DIR / ".hud_prefs.json").exists()
    prefs = load_prefs()
    if args.plugin and first_prefs:
        prefs["follow"] = "dsh"
    if args.reset_pos:
        prefs.pop("x", None)
        prefs.pop("y", None)
    api_token = ensure_api_token(prefs)
    if not UPSTREAM.lower().startswith("https://"):
        log(f"warning: BAILIAN_UPSTREAM={UPSTREAM!r} 不是 HTTPS，客户端 API Key 可能明文传输")

    show_flag: dict = {}
    store = None
    service = None
    feed = None
    source = None
    coupon_sync = None
    app = None
    parent_watch = None
    try:
        if args.plugin:
            parent_watch = ParentWatch(args.parent_pid)
        if args.demo:
            source = DemoSource()
        else:
            store = Store(BASE_DIR)
            if not args.attach:
                store.configure_coupon(prefs.get("coupon", ""))
            if args.face is not None:
                try:
                    face = finite_float(args.face, "face", 0.01, 1e7)
                except ValueError as exc:
                    _msgbox(f"--face 参数无效：{exc}")
                    return 2
                store.face = face
                store.save()
            if args.attach:
                feed = RemoteFeed(port)
                feed.start()
                source = feed
            elif args.no_proxy:
                source = store
            else:
                service = ProxyService(store, port,
                                       on_show=lambda: show_flag.__setitem__("req", True),
                                       api_token=api_token)
                try:
                    service.start()
                except OSError as exc:
                    if args.port is not None:
                        log(f"bind failed: {exc!r}")
                        _msgbox(f"指定端口 {port} 无法使用。\n请取消 --port 参数后重新启动，"
                                f"由程序选择可用端口。\n\n{exc}")
                        return 2
                    log(f"preferred port {port} unavailable, choosing a free local port: {exc!r}")
                    service.port = 0
                    service.start()
                port = service.port
                service.server.instance_id = instance_id
                service.server.data_id = directory_id(BASE_DIR)
                if source is None:
                    source = store

        if store is not None and not args.attach and not args.no_proxy:
            coupon_sync = CouponSync(store, prefs.get("coupon", ""))
            service.server.coupon_sync = coupon_sync
            coupon_sync.start()
        app = HudApp(source, store=store, service=service, port=port,
                     prefs=prefs, show_flag=show_flag, coupon_sync=coupon_sync,
                     plugin_mode=args.plugin, owner_pid=args.parent_pid)
        if service:
            service.server.hud_control = app.controls.put
            if not args.plugin:
                from hud_tray import TrayIcon
                app.tray = TrayIcon(app.controls.put, Path(__file__).parent / "assets" / "BailianHUD.ico", log)
                app.tray.start()
        app.start()
        if parent_watch:
            def check_owner():
                if app._closing:
                    return
                if not parent_watch.alive():
                    log("DSH host exited; stopping plugin worker")
                    app._quit_now()
                    return
                app.after(1000, check_owner)
            app.after(1000, check_owner)
        if service:
            app._publish_hud_state()
            publish_instance(BASE_DIR, port, instance_id)
            if not args.background:
                app.controls.put("show")
        app.mainloop()
        log("UI mainloop ended")
        return 0
    except Exception:
        detail = traceback.format_exc()
        log("fatal:\n" + detail)
        _msgbox("启动失败：\n\n" + detail[-1200:])
        return 2
    finally:
        if parent_watch:
            parent_watch.close()
        if app and app.tray:
            app.tray.close()
        try:
            if coupon_sync:
                coupon_sync.stop()
        except Exception:
            pass
        try:
            if feed:
                feed.stop()
        except Exception:
            pass
        try:
            if service:
                service.stop()
        except Exception:
            pass
        clear_instance(BASE_DIR, instance_id)
        instance_lock.close()
        log("service stopped")



if __name__ == "__main__":
    sys.exit(main())
