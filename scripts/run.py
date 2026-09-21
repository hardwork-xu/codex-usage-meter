#!/usr/bin/env python3
"""Portable entry for marketplace-installed skills and hooks. No model calls."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

if sys.version_info < (3, 10):
    raise SystemExit("用量计需要 Python 3.10+。macOS 请更新 python3；Windows 请更新 py -3。")

from platform_support import default_data_dir


def runtime_folder(value=None):
    # Hooks and skill shell commands do not share PLUGIN_DATA. Use a stable,
    # user-owned location across marketplaces, cache versions, and entry points.
    value = value or os.environ.get("METER_DATA_DIR")
    return Path(value).expanduser().resolve() if value else default_data_dir()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", help="Explicit isolated data directory")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("open", "summary", "hook", "stop", "browser-status", "browser-uninstall"):
        sub.add_parser(command)
    browser = sub.add_parser("browser-install")
    browser.add_argument("--proxy-http", help="Previously verified local HTTP proxy, optional")
    args = parser.parse_args(argv)
    folder = runtime_folder(args.data_dir)

    if args.command.startswith("browser-"):
        import browser_access
        action = args.command.removeprefix("browser-")
        if action == "install":
            result = browser_access.install(folder, proxy_http=args.proxy_http)
        else:
            result = getattr(browser_access, action)(folder)
        print(json.dumps(result, ensure_ascii=False))
        return

    import meter
    previous = sys.argv
    try:
        sys.argv = [str(Path(__file__).with_name("meter.py")), "--data-dir", str(folder), args.command]
        meter.main()
    finally:
        sys.argv = previous


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError) as exc:
        raise SystemExit(str(exc)) from None
