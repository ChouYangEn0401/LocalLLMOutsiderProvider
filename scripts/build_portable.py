"""Build the Windows portable edition: a zip that contains its own Python.

Unzip, double-click Start.bat, done - no Python installation needed.

    python scripts/build_portable.py                     # uses dist/*.whl
    python scripts/build_portable.py --python-archive py.tar.gz

Works on Linux or Windows (it only downloads Windows wheels and unpacks them;
nothing is executed with the bundled Python). Output:
    dist/llmop-<version>-windows-x64-portable.zip

Layout inside the zip:
    llmop-<version>/
      python/                 standalone CPython (python-build-standalone, install_only)
      python/Lib/site-packages/  llmop + dependencies (Windows wheels)
      Start.bat, Start-With-Tunnel.bat, Add-User.bat, Check.bat, Command-Prompt.bat
      config.example.yaml, README.txt, docs/
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY_VERSION = "3.12"
# python-build-standalone release used by uv; pinned for reproducible builds.
PYTHON_URL = (
    "https://github.com/astral-sh/python-build-standalone/releases/download/20251014/"
    "cpython-3.12.12%2B20251014-x86_64-pc-windows-msvc-install_only_stripped.tar.gz"
)

README_TXT = """\
llmop {version} - Windows portable edition (Python included)
=============================================================

This folder is the SERVER side ("架設端"). People who use your models need
nothing from here: they use any OpenAI-compatible tool with your URL + API key.

1. Make sure Ollama is running and has a model:   ollama pull qwen3.5:9b
2. Double-click  Start.bat              (only this computer, http://127.0.0.1:8780)
   or            Start-With-Tunnel.bat  (public https://....trycloudflare.com address)
   The first start asks for the admin password and creates config.yaml.
3. Log in on the web page and create an API key.

Add a friend:       Add-User.bat friend1
Check backends:     Check.bat
Other commands:     Command-Prompt.bat, then e.g.  llmop usage / llmop keys
Quotas / accounts:  edit config.yaml with Notepad (applies without restart)
Stop:               close the black window

Update: unzip the new version into a NEW folder, then copy config.yaml and the
data folder from the old one.

Docs: docs\\HANDOFF.md, docs\\DEPLOYMENT_GUIDE.html (open in a browser)
"""


def log(msg: str) -> None:
    print(f"[portable] {msg}", flush=True)


def version() -> str:
    ns: dict = {}
    exec((ROOT / "src/llmop/__init__.py").read_text(encoding="utf-8"), ns)
    return ns["__version__"]


def fetch(url: str, dest: Path) -> None:
    log(f"downloading {url}")
    with urllib.request.urlopen(url) as resp, dest.open("wb") as fh:
        shutil.copyfileobj(resp, fh)


def crlf(text: str) -> bytes:
    return text.replace("\r\n", "\n").replace("\n", "\r\n").encode("ascii")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--wheel", help="llmop wheel (default: newest dist/*.whl)")
    ap.add_argument("--python-archive", help="local python-build-standalone .tar.gz (default: download)")
    ap.add_argument("--python-url", default=PYTHON_URL)
    ap.add_argument("--out", default=str(ROOT / "dist"))
    args = ap.parse_args(argv)

    ver = version()
    wheel = Path(args.wheel) if args.wheel else None
    if wheel is None:
        cands = sorted(glob.glob(str(ROOT / f"dist/*-{ver}-py3-none-any.whl")))
        if not cands:
            log("no wheel in dist/ - run: python -m build")
            return 1
        wheel = Path(cands[-1])

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    name = f"llmop-{ver}"
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        stage = tmp / name
        stage.mkdir()

        # 1. Python runtime
        archive = Path(args.python_archive) if args.python_archive else tmp / "python.tar.gz"
        if not args.python_archive:
            fetch(args.python_url, archive)
        log(f"python archive sha256 {hashlib.sha256(archive.read_bytes()).hexdigest()}")
        with tarfile.open(archive) as tf:
            tf.extractall(stage, filter="data") if sys.version_info >= (3, 12) else tf.extractall(stage)
        py_dir = stage / "python"
        if not (py_dir / "python.exe").exists():
            log("archive has no python/python.exe - wrong file?")
            return 1

        # 2. llmop + dependencies as Windows wheels, unpacked into site-packages
        site = py_dir / "Lib" / "site-packages"
        site.mkdir(parents=True, exist_ok=True)
        log("installing llmop + dependencies (win_amd64 wheels)")
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--quiet", "--no-compile",
             "--target", str(site), "--platform", "win_amd64",
             "--python-version", PY_VERSION, "--implementation", "cp",
             "--only-binary=:all:", "--upgrade", str(wheel)],
            check=True,
        )
        # pip --target writes Linux-style console scripts; the .bat files use "python -m llmop".
        shutil.rmtree(site / "bin", ignore_errors=True)

        # 3. launchers, docs, example config
        for bat in sorted((ROOT / "scripts/windows/portable").glob("*.bat")):
            (stage / bat.name).write_bytes(crlf(bat.read_text(encoding="ascii")))
        shutil.copy2(ROOT / "config.example.yaml", stage / "config.example.yaml")
        (stage / "README.txt").write_bytes(README_TXT.format(version=ver).replace("\n", "\r\n").encode("utf-8-sig"))
        docs = stage / "docs"
        docs.mkdir()
        for rel in ("README.md", "LICENSE", "docs/HANDOFF.md", "docs/DEPLOYMENT_GUIDE.html",
                    "docs/research/RESEARCH_REPORT.md", "docs/plan/DEVELOPMENT_PLAN.md"):
            src = ROOT / rel
            if src.exists():
                shutil.copy2(src, docs / src.name)

        # 4. zip
        target = out_dir / f"{name}-windows-x64-portable.zip"
        if target.exists():
            target.unlink()
        log(f"writing {target}")
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
            for path in sorted(stage.rglob("*")):
                if "__pycache__" in path.parts:
                    continue
                zf.write(path, path.relative_to(tmp))
    log(f"done: {target} ({target.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
