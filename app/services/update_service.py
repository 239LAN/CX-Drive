"""自动更新服务

每日 0 点请求 GitHub Releases API 比对版本：发现新版本时下载发布脚本
（RealFiles-release-<版本>-install.sh，与本仓库 Release 附件同名），校验 SHA-256 后
交由安装脚本写入的 sudoers 规则以 root 在独立 systemd 单元中执行覆盖更新
（脱离 realfiles.service 的 cgroup，避免更新脚本重启服务时自身被杀）。

检查结果（最新版本 / 是否有更新 / 检查时间 / 错误）落盘到
UPDATE_DIR/status.json，供页脚跨进程读取；是否真正安装由 config.yml 的
update.enabled 决定，关闭时仅检查并在页脚提示。

GitHub 直连不通时（例如国内服务器），会按 config.yml 的 update.proxy
回退到公共 GH 代理，规则是「先直连，失败才走代理」。
"""
import hashlib
import json
import os
import re
import subprocess
import threading
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from config import (Config, UPDATE_ENABLED, UPDATE_PROXY, UPDATE_REPO,
                    load_site_config, normalize_proxy, read_version)

_API_URL = "https://api.github.com/repos/{repo}/releases/latest"
_USER_AGENT = "RealFiles-Updater/1.0 (+https://github.com/{repo})"
# 发布脚本命名：RealFiles-release-1.1.1-install.sh；兼容改名前的 CXDrive-release-*.sh，
# 以便旧版本实例也能自动升级到 RealFiles 的首个发布
_ASSET_PATTERN = re.compile(r"^(?:RealFiles|CXDrive)-release-.*\.sh$", re.IGNORECASE)

CHECK_TIMEOUT = 10       # 请求 GitHub API 超时（秒）
DOWNLOAD_TIMEOUT = 60    # 下载发布脚本超时（秒）

_state_lock = threading.Lock()
_cache = {"key": None, "data": None}

# 落盘的状态字段（current / has_update 每次读取时按本地 VERSION 重新计算）
_FIELDS = ("latest", "checked_at", "error")


def _config():
    """应用上下文内取已加载的配置，否则回退读取 config.yml"""
    from flask import current_app, has_app_context
    cfg = current_app.config if has_app_context() else load_site_config()
    return {
        "VERSION": cfg.get("VERSION") or read_version(),
        "UPDATE_ENABLED": bool(cfg.get("UPDATE_ENABLED", UPDATE_ENABLED)),
        "UPDATE_REPO": cfg.get("UPDATE_REPO") or UPDATE_REPO,
        "UPDATE_PROXY": normalize_proxy(cfg.get("UPDATE_PROXY", UPDATE_PROXY)),
        "UPDATE_DIR": cfg.get("UPDATE_DIR") or Config.UPDATE_DIR,
        "UPDATE_TRIGGER": cfg.get("UPDATE_TRIGGER") or Config.UPDATE_TRIGGER,
    }


def parse_version(text):
    """从 v1.1.2 / 1.1.2 之类的文本中提取可比较的版本元组"""
    match = re.search(r"\d+(?:\.\d+)*", str(text or ""))
    if not match:
        return ()
    return tuple(int(part) for part in match.group(0).split("."))


def is_newer(latest, current):
    """latest 是否比 current 新（解析不出数字的版本视为最旧）"""
    return parse_version(latest) > parse_version(current)


def read_status():
    """读取最近一次检查结果（文件未变化时命中进程内缓存）

    返回字典始终包含 current / latest / has_update / checked_at / error。
    本地版本号以 VERSION 文件为准，避免升级后残留旧值。
    """
    cfg = _config()
    path = os.path.join(cfg["UPDATE_DIR"], "status.json")
    try:
        key = (path, os.path.getmtime(path))
    except OSError:
        return _status(cfg)
    with _state_lock:
        if _cache["key"] == key and _cache["data"] is not None:
            return _cache["data"]
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    status = _status(cfg, **{k: v for k, v in data.items() if k in _FIELDS})
    with _state_lock:
        _cache["key"] = key
        _cache["data"] = status
    return status


def _status(cfg, latest="", checked_at="", error=""):
    current = cfg["VERSION"]
    return {
        "current": current,
        "latest": str(latest or ""),
        "has_update": bool(latest) and is_newer(latest, current),
        "checked_at": str(checked_at or ""),
        "error": str(error or ""),
    }


def _write_status(cfg, latest="", checked_at="", error=""):
    status = _status(cfg, latest=latest, checked_at=checked_at, error=error)
    path = os.path.join(cfg["UPDATE_DIR"], "status.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(status, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    with _state_lock:
        _cache["key"] = None
        _cache["data"] = None
    return status


def _url_variants(url, proxy):
    """直连地址优先，其次（配置了代理时）是代理地址"""
    yield url
    if proxy and not url.startswith(proxy):
        yield proxy + url


def _fetch_release(repo, timeout=CHECK_TIMEOUT, proxy=""):
    """请求 GitHub API 获取最新 Release（独立函数便于测试替换）

    直连失败时回退到公共 GH 代理，两者都失败才抛出后一个异常。
    """
    last = None
    for url in _url_variants(_API_URL.format(repo=repo), proxy):
        req = Request(url, headers={
            "User-Agent": _USER_AGENT.format(repo=repo),
            "Accept": "application/vnd.github+json",
        })
        try:
            with urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (HTTPError, URLError, OSError, ValueError) as e:
            last = e
    raise last


def _pick_asset(release):
    """从 Release 中挑选发布脚本附件，返回 (名称, 下载地址, sha256)"""
    for asset in release.get("assets") or []:
        name = str(asset.get("name") or "")
        if not _ASSET_PATTERN.match(name):
            continue
        digest = str(asset.get("digest") or "")
        sha256 = digest.split(":", 1)[1] if digest.startswith("sha256:") else ""
        return name, str(asset.get("browser_download_url") or ""), sha256
    return "", "", ""


def _download(url, sha256, repo, dest, proxy=""):
    """下载发布脚本并校验摘要，返回脚本路径

    直连失败时回退到公共 GH 代理；重试会以 wb 重新覆盖，不会留下半截文件。
    """
    last = None
    for target in _url_variants(url, proxy):
        try:
            return _download_once(target, sha256, repo, dest)
        except (HTTPError, URLError, OSError, ValueError) as e:
            last = e
    raise last


def _download_once(url, sha256, repo, dest):
    """从单个地址下载发布脚本并校验 sha256"""
    req = Request(url, headers={"User-Agent": _USER_AGENT.format(repo=repo)})
    digest = hashlib.sha256()
    with urlopen(req, timeout=DOWNLOAD_TIMEOUT) as resp, open(dest, "wb") as fh:
        while True:
            chunk = resp.read(256 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            fh.write(chunk)
    if sha256 and digest.hexdigest().lower() != sha256.lower():
        os.remove(dest)
        raise ValueError("发布脚本 sha256 校验不通过")
    return dest


def trigger_update(trigger):
    """以 root 在独立 systemd 单元中执行覆盖更新

    实际执行的是安装脚本写入的 wrapper（sudoers 中放行的固定路径，无参数），
    它负责用 systemd-run 脱离当前服务 cgroup，再运行下载好的更新脚本。
    """
    subprocess.run(["sudo", "-n", trigger], check=True, timeout=30,
                   capture_output=True, text=True)


def check(apply_update=None):
    """检查（并在允许时自动安装）新版本，返回最新状态字典

    apply_update 为 None 时取 config.yml 的 update.enabled。
    """
    cfg = _config()
    if apply_update is None:
        apply_update = cfg["UPDATE_ENABLED"]
    checked_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    try:
        release = _fetch_release(cfg["UPDATE_REPO"], proxy=cfg["UPDATE_PROXY"])
    except (HTTPError, URLError, OSError, ValueError) as e:
        return _write_status(cfg, error=f"检查更新失败：{e}", checked_at=checked_at)

    latest = str(release.get("tag_name") or "").strip().lstrip("vV")
    if not latest:
        return _write_status(cfg, error="未获取到版本号", checked_at=checked_at)
    if not is_newer(latest, cfg["VERSION"]) or not apply_update:
        return _write_status(cfg, latest=latest, checked_at=checked_at)

    asset, url, sha256 = _pick_asset(release)
    if not url:
        return _write_status(cfg, latest=latest, checked_at=checked_at,
                             error=f"发布中未找到 {asset or 'RealFiles-release-*-install.sh'} 附件")
    dest = os.path.join(cfg["UPDATE_DIR"], "current.sh")
    try:
        os.makedirs(cfg["UPDATE_DIR"], exist_ok=True)
        _download(url, sha256, cfg["UPDATE_REPO"], dest, proxy=cfg["UPDATE_PROXY"])
        trigger_update(cfg["UPDATE_TRIGGER"])
    except (HTTPError, URLError, OSError, ValueError, subprocess.SubprocessError) as e:
        return _write_status(cfg, latest=latest, checked_at=checked_at,
                             error=f"自动更新失败：{e}")
    return _write_status(cfg, latest=latest, checked_at=checked_at)
