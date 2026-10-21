"""Open a validated local PDF in Google Chrome on macOS."""
from pathlib import Path
import subprocess
import sys


def open_pdf_in_chrome(path):
    path = Path(path).resolve(strict=True)
    if path.suffix.lower() != '.pdf' or not path.is_file():
        raise ValueError('这个文件尚无可查看的 PDF 版本')
    if sys.platform != 'darwin':
        raise ValueError('当前系统暂不支持通过此入口打开 Chrome')
    try:
        subprocess.run(['/usr/bin/open', '-a', 'Google Chrome', str(path)], check=True, capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError('无法打开 Google Chrome，请检查 Chrome 是否已安装') from exc
    return {'opened': True, 'browser': 'chrome', 'name': path.name}
