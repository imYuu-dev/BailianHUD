"""Local AK accounts and atomic migration from single-account configuration."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile


def _read(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (OSError, ValueError):
        raise ValueError("账号配置文件无法读取，请检查文件是否完整") from None


def account_id(ak_id):
    return hashlib.sha256(ak_id.encode("utf-8")).hexdigest()[:24]


def active_account(data):
    return next((item for item in data["accounts"] if item["id"] == data.get("activeAccountId")), None)


def read_accounts(path, legacy_path):
    local = _read(path) if Path(path).exists() else None
    if local is not None and "accounts" in local:
        accounts = local["accounts"]
        if not isinstance(accounts, list) or any(
                not isinstance(item, dict) or not isinstance(item.get("aliyunAkId"), str)
                or not item["aliyunAkId"] or not isinstance(item.get("aliyunAkSecret"), str)
                or not item["aliyunAkSecret"] or item.get("id") != account_id(item["aliyunAkId"])
                or not isinstance(item.get("name", ""), str) or not isinstance(item.get("coupon", ""), str)
                for item in accounts):
            raise ValueError("账号配置文件格式有误")
        if len({item["id"] for item in accounts}) != len(accounts):
            raise ValueError("账号配置文件中存在重复账号")
        if accounts and active_account(local) is None:
            raise ValueError("当前账号不存在，请检查账号配置")
        local.setdefault("activeAccountId", "")
        return local
    conf = local
    if conf is None:
        conf = _read(legacy_path) if Path(legacy_path).exists() else {}
    ak_id = str(conf.get("aliyunAkId") or conf.get("accessKeyId") or "").strip()
    secret = str(conf.get("aliyunAkSecret") or conf.get("accessKeySecret") or "").strip()
    token = str(conf.get("aliyunStsToken") or conf.get("securityToken") or "").strip()
    source = "本地配置"
    for kid, ksecret, ktoken in (
            ("ALIYUN_AK_ID", "ALIYUN_AK_SECRET", "ALIYUN_STS_TOKEN"),
            ("ALIBABA_CLOUD_ACCESS_KEY_ID", "ALIBABA_CLOUD_ACCESS_KEY_SECRET", "ALIBABA_CLOUD_SECURITY_TOKEN"),
            ("ALIYUN_ACCESS_KEY_ID", "ALIYUN_ACCESS_KEY_SECRET", "ALIYUN_SECURITY_TOKEN")):
        if os.environ.get(kid, "").strip() and os.environ.get(ksecret, "").strip():
            ak_id, secret = os.environ[kid].strip(), os.environ[ksecret].strip()
            token, source = os.environ.get(ktoken, "").strip(), kid
            break
    accounts = []
    if ak_id and secret:
        account = {"id": account_id(ak_id), "name": "账号1", "aliyunAkId": ak_id,
                   "aliyunAkSecret": secret, "source": source}
        if token:
            account["aliyunStsToken"] = token
        if conf.get("bssEndpoint"):
            account["bssEndpoint"] = conf["bssEndpoint"]
        accounts.append(account)
    return {"version": 2, "accounts": accounts, "activeAccountId": accounts[0]["id"] if accounts else ""}


def save_accounts(path, data):
    path = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".ak-", suffix=".tmp", delete=False) as file:
            temporary = Path(file.name)
            # Explicit account selection takes precedence over the legacy environment fallback.
            for account in data["accounts"]:
                account.pop("source", None)
            json.dump(data, file, ensure_ascii=False)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def public_accounts(data):
    items = []
    for account in data["accounts"]:
        ak_id = account["aliyunAkId"]
        masked = ak_id[:4] + "…" + ak_id[-4:] if len(ak_id) > 12 else "…" + ak_id[-1:]
        items.append({"id": account["id"], "name": account.get("name") or "账号",
                      "maskedAkId": masked})
    return {"accounts": items, "count": len(items), "activeAccountId": data["activeAccountId"]}
