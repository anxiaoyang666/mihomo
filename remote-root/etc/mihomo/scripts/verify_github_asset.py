#!/usr/bin/env python3
"""Check a downloaded GitHub release asset against metadata from api.github.com.

The metadata file is the JSON for one release (or a list of releases). This script
never contacts the network. Callers must fetch that JSON from api.github.com
directly — a copy that arrived through GH_PROXY is not a trustworthy digest.

Exit 0 when the file is acceptable. A proxied download with no usable sha256 is
rejected. A direct download with no digest is allowed; the caller still has to
do its own gzip / execute checks.
"""
import hashlib
import hmac
import json
import os
import re
import sys


def asset_metadata(document, asset_name):
    """Return {"size": int|None, "digest": str} for asset_name, or empty metadata."""
    if isinstance(document, dict):
        releases = [document]
    elif isinstance(document, list):
        releases = document
    else:
        releases = []
    for release in releases:
        if not isinstance(release, dict):
            continue
        for asset in release.get("assets") or []:
            if isinstance(asset, dict) and asset.get("name") == asset_name:
                size = asset.get("size")
                usable_size = size if isinstance(size, int) and not isinstance(size, bool) else None
                return {"size": usable_size, "digest": str(asset.get("digest") or "")}
    return {"size": None, "digest": ""}


def verify_file(path, meta, via_proxy):
    """Return (ok, message). via_proxy means the bytes did not come from github.com."""
    meta = meta or {}
    size = meta.get("size")
    digest = str(meta.get("digest") or "").strip()
    actual_size = os.path.getsize(path)
    if isinstance(size, int) and not isinstance(size, bool) and actual_size != size:
        return False, f"文件大小不符：期望 {size} 字节，实际 {actual_size} 字节"
    algo, sep, hexpart = digest.partition(":")
    if sep and algo.lower() == "sha256" and re.fullmatch(r"[0-9a-fA-F]{64}", hexpart.strip()):
        with open(path, "rb") as handle:
            actual = hashlib.sha256(handle.read()).hexdigest()
        if not hmac.compare_digest(actual, hexpart.strip().lower()):
            return False, "sha256 与 GitHub 发布资产的 digest 不一致，已拒绝安装"
        return True, "sha256 校验通过"
    if via_proxy:
        if digest:
            return False, "通过代理下载的内核没有可用的 sha256 digest，已拒绝安装"
        return False, "通过代理下载内核必须校验 GitHub 资产 sha256，但 API 没有提供可用的 digest，已中止"
    if digest:
        return True, "GitHub digest 不是可用的 sha256，直连下载改为 gzip 完整性校验"
    return True, "GitHub 未提供 sha256，直连下载使用 gzip 完整性校验"


def load_document(meta_path):
    if not meta_path or meta_path == "-" or not os.path.isfile(meta_path):
        return None
    if os.path.getsize(meta_path) <= 0:
        return None
    with open(meta_path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 4:
        print("usage: verify_github_asset.py FILE META_JSON ASSET_NAME VIA_PROXY", file=sys.stderr)
        return 2
    path, meta_path, asset_name, via_flag = argv
    via_proxy = str(via_flag) == "1"
    try:
        document = load_document(meta_path)
    except (OSError, ValueError) as exc:
        if via_proxy:
            print(f"无法解析 GitHub 资产信息：{exc}", file=sys.stderr)
            return 1
        document = None
    meta = asset_metadata(document, asset_name) if document is not None else {"size": None, "digest": ""}
    try:
        ok, message = verify_file(path, meta, via_proxy)
    except OSError as exc:
        print(f"无法读取内核文件：{exc}", file=sys.stderr)
        return 1
    if not ok:
        print(message, file=sys.stderr)
        return 1
    print(message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
