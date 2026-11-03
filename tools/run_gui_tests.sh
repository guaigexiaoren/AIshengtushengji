#!/bin/sh

set -eu

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$project_dir"

if [ -n "${PYTHON_BIN:-}" ]; then
    python_bin=$PYTHON_BIN
elif [ -x "$project_dir/.venv/bin/python" ]; then
    python_bin=$project_dir/.venv/bin/python
else
    python_bin=/opt/homebrew/bin/python3.11
fi
if [ ! -x "$python_bin" ]; then
    printf '找不到可执行的 Python 解释器：%s\n' "$python_bin" >&2
    printf '可通过 PYTHON_BIN=/path/to/python3.11 指定。\n' >&2
    exit 2
fi

export PYTHONDONTWRITEBYTECODE=1
export QT_QPA_PLATFORM=offscreen

if ! "$python_bin" -c "import PySide6" >/dev/null 2>&1; then
    printf '当前 Python 未安装 PySide6：%s\n' "$python_bin" >&2
    printf '请先执行：%s -m pip install -r requirements.txt\n' "$python_bin" >&2
    exit 2
fi

if [ "$#" -eq 0 ]; then
    set -- \
        tests.test_async_thumbnails \
        tests.test_batch_dress \
        tests.test_collaboration_localization \
        tests.test_collaboration_pages \
        tests.test_gui \
        tests.test_icon_assets \
        tests.test_local_export
fi

exec "$python_bin" -m unittest "$@"
