# -*- coding: utf-8 -*-
"""生成单文件安装包 install.sh

将项目源码打包为 gzip tar，base64 编码后内嵌至 install-template.sh 的负载区，
输出在 Linux（systemd 发行版）上可直接执行的单文件安装脚本。

用法:
    python build_install.py               # 输出 install.sh
    python build_install.py -o app.sh     # 指定输出文件名
"""
import argparse
import base64
import fnmatch
import gzip
import io
import os
import tarfile

BASE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(BASE, "install-template.sh")
DEFAULT_OUT = os.path.join(BASE, "install.sh")

BEGIN = "# CLOUDPAN_PAYLOAD_BEGIN"
END = "# CLOUDPAN_PAYLOAD_END"

# 排除项与 install-template.sh 的 rsync 规则一致：数据/密钥/环境/开发残留不打包
EXCLUDE_DIRS = {".git", "__pycache__", "venv", ".venv", "instance", "storage",
                "uploads", ".pytest_cache", "_smoke_tmp", "_chk_tmp", "_rt_tmp"}
EXCLUDE_FILES = {".gitignore", ".env", "install.sh", "install-template.sh",
                 "cloudpan-install.sh", "build_install.py",
                 "_smoke_run.py", "_chk_new.py", "_rt_check.py", "_pyc_probe.py"}
EXCLUDE_PATTERNS = ("*.pyc", "*.db", "*.log")


def should_exclude(rel: str) -> bool:
    name = os.path.basename(rel)
    if name in EXCLUDE_FILES:
        return True
    return any(fnmatch.fnmatch(name, p) for p in EXCLUDE_PATTERNS)


def build_payload() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.GNU_FORMAT) as tf:
        for root, dirs, files in os.walk(BASE):
            dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
            for f in files:
                fp = os.path.join(root, f)
                rel = os.path.relpath(fp, BASE)
                if should_exclude(rel):
                    continue
                tf.add(fp, arcname=rel.replace(os.sep, "/"))
    data = buf.getvalue()
    # base64 每 76 字符换行
    encoded = base64.encodebytes(data).decode("ascii").rstrip("\n")
    return encoded.encode("utf-8")


def inject_payload(template: str, payload: bytes) -> str:
    """在 BEGIN 标记行后注入 base64 负载（仅匹配整行恰为 BEGIN 的行）"""
    lines = template.splitlines(keepends=True)
    out = []
    done = False
    for ln in lines:
        out.append(ln)
        if not done and ln.rstrip("\r\n") == BEGIN:
            out.append(payload.decode("ascii") + "\n")
            done = True
    if not done:
        raise SystemExit("错误：install.sh 中缺少内嵌负载标记")
    return "".join(out)


def main():
    ap = argparse.ArgumentParser(description="生成创想云盘单文件安装包")
    ap.add_argument("-o", "--output", default=DEFAULT_OUT, help="输出文件名")
    args = ap.parse_args()

    with open(TEMPLATE, encoding="utf-8") as fh:
        template = fh.read()
    if END not in template:
        raise SystemExit("错误：install-template.sh 中缺少内嵌负载尾标记")

    payload = build_payload()
    out = inject_payload(template, payload)

    with open(args.output, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(out)

    size_mb = os.path.getsize(args.output) / 1024 / 1024
    print(f"已生成: {args.output} ({size_mb:.2f} MB, 负载 {len(payload) / 1024:.0f} KB)")
    print("Linux 上执行:  sudo bash %s" % os.path.basename(args.output))


if __name__ == "__main__":
    main()
