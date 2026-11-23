"""把 manual.md 构建成可打印的 HTML 和 PDF。

运行方式（仓库根目录）：
    py -3.13 docs/manual/build.py

依赖：markdown-it-py（仅构建文档使用，不加入应用运行依赖）；
PDF 由本机 Microsoft Edge 无头模式打印，不需要额外安装软件。
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from markdown_it import MarkdownIt

MANUAL_DIR = Path(__file__).resolve().parent
DOCS_DIR = MANUAL_DIR.parent
SOURCE = MANUAL_DIR / "manual.md"
STYLE = MANUAL_DIR / "style.css"
HTML_OUT = DOCS_DIR / "使用手册-运营版.html"
PDF_OUT = DOCS_DIR / "使用手册-运营版.pdf"

EDGE_CANDIDATES = (
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
)


def render_html() -> str:
    markdown = MarkdownIt("commonmark", {"html": True})
    markdown.enable("table")
    body = markdown.render(SOURCE.read_text(encoding="utf-8"))
    css = STYLE.read_text(encoding="utf-8")
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>亦欣 AI 生图工作台 · 运营使用手册</title>
<style>
{css}
</style>
</head>
<body>
{body}
</body>
</html>
"""


def find_edge() -> Path:
    for candidate in EDGE_CANDIDATES:
        if candidate.is_file():
            return candidate
    found = shutil.which("msedge")
    if found:
        return Path(found)
    raise RuntimeError("未找到 Microsoft Edge，无法生成 PDF")


def print_pdf(html_path: Path) -> None:
    edge = find_edge()
    profile = Path(tempfile.mkdtemp(prefix="yixin-manual-"))
    try:
        args = [
            str(edge),
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            f"--user-data-dir={profile}",
            "--no-pdf-header-footer",
            "--print-to-pdf-no-header",
            "--virtual-time-budget=8000",
            f"--print-to-pdf={PDF_OUT}",
            html_path.as_uri(),
        ]
        result = subprocess.run(
            args,
            capture_output=True,
            timeout=180,
            check=False,
        )
        if not PDF_OUT.is_file():
            message = (result.stderr or result.stdout).decode(
                "utf-8",
                errors="replace",
            )
            raise RuntimeError(f"Edge 未能生成 PDF：{message[-1500:]}")
    finally:
        shutil.rmtree(profile, ignore_errors=True)


def pdf_page_count() -> int:
    data = PDF_OUT.read_bytes()
    counts = [
        int(value)
        for value in re.findall(rb"/Count\s+(\d+)", data)
    ]
    return max(counts) if counts else 0


def main() -> int:
    missing = [
        path.name
        for path in [
            DOCS_DIR / "screenshots" / name
            for name in (
                "01-login.png",
                "02-task-submit.png",
                "02b-task-submit-table.png",
                "02c-task-submit-preview.png",
                "03-task-submit-fields.png",
                "04-submission-list.png",
                "04b-submission-table.png",
                "04c-submission-panel.png",
                "05-listing-dialog.png",
            )
        ]
        if not path.is_file()
    ]
    if missing:
        raise SystemExit(
            "缺少截图，请先运行 make_screenshots.py：" + "、".join(missing)
        )

    HTML_OUT.write_text(render_html(), encoding="utf-8")
    print(f"已生成 {HTML_OUT.relative_to(DOCS_DIR.parent)}")

    print_pdf(HTML_OUT)
    pages = pdf_page_count()
    size_mb = PDF_OUT.stat().st_size / 1024 / 1024
    print(
        f"已生成 {PDF_OUT.relative_to(DOCS_DIR.parent)}"
        f"（约 {pages} 页，{size_mb:.1f} MB）"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
