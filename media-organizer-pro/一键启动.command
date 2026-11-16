#!/bin/bash
set -e
cd "$(dirname "$0")"
PYTHON=""
for candidate in \
  /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 \
  /Library/Frameworks/Python.framework/Versions/3.12/bin/python3 \
  /Library/Frameworks/Python.framework/Versions/3.11/bin/python3 \
  /opt/homebrew/bin/python3 \
  /usr/local/bin/python3 \
  python3; do
  if [ -x "$candidate" ]; then
    resolved="$candidate"
  elif command -v "$candidate" >/dev/null 2>&1; then
    resolved="$(command -v "$candidate")"
  else
    continue
  fi
  if "$resolved" -c 'import sys, tkinter; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
    PYTHON="$resolved"
    break
  fi
done
if [ -z "$PYTHON" ]; then
  osascript -e 'display dialog "未检测到带 Tk 支持的 Python 3.11 或更高版本。请先安装后再试。" buttons {"好"} default button "好" with icon stop'
  exit 1
fi
exec "$PYTHON" media_organizer_pro.py
