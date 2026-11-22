"""Build the Windows portable edition: a zip that contains its own Python.

Unzip, double-click Start.bat, done - no Python installation needed.

    python -m build                                      # makes dist/*.whl first
    python scripts/build_portable.py                     # uses dist/*.whl, downloads Python
    python scripts/build_portable.py --python-archive py.tar.gz

Works on Linux or Windows (it only downloads Windows wheels and unpacks them;
nothing is executed with the bundled Python). Output:
    dist/llmop-<version>-windows-x64-portable.zip

Supply chain: the Python archive (downloaded or given with --python-archive) must
match the pinned SHA-256 (PYTHON_SHA256); the build stops on any mismatch. Change
PYTHON_URL and PYTHON_SHA256 together, or pass --python-url and --python-sha256.

Layout inside the zip:
    llmop-<version>/
      python/                 standalone CPython (python-build-standalone, install_only)
      python/Lib/site-packages/  llmop[secure] + dependencies (Windows wheels)
      Start.bat, Start-With-Tunnel.bat, Start-Worker.bat, Start-Client-Proxy.bat,
      Rotate-API-Link.bat, Add-User.bat, Check.bat, Command-Prompt.bat
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
# sha256 of exactly that file. Never update it without updating PYTHON_URL.
PYTHON_SHA256 = "3c8b9b10a933909c98b9916297e2093b24a9c2abaa23df1c2622c2bfe052cb94"

# pip evaluates environment markers (sys_platform == "win32") for the machine that
# runs the build, not for the --platform target, so Windows-only dependencies are
# listed here explicitly: tzdata (zoneinfo has no system database on Windows; the
# weekly reset uses it) and colorama (click / uvicorn console colours).
WINDOWS_ONLY = ["tzdata", "colorama"]
# Must be importable from the zip, or the build fails (llmop[secure] = encrypted mode).
REQUIRED = ["llmop", "fastapi", "uvicorn", "httpx", "yaml", "cryptography", "aiohttp",
            "websockets", "tzdata"]

DOCS = ("README.md", "LICENSE", "docs/HANDOFF.md", "docs/DEPLOYMENT_GUIDE.html",
        "docs/secure/README.md", "docs/secure/DEPLOY.md", "docs/secure/PROTOCOL.md",
        "docs/secure/SECURITY.md")

README_TXT = """\
llmop {version} - Windows portable edition (Python included)
=============================================================

This folder is the SERVER side. People who use your model need nothing from
here for plain (direct) API keys: any OpenAI-compatible tool + the API link +
an sk-llop- key. Only encrypted keys need "llmop client proxy" on their side.

1. Start your LLM server (Ollama / LM Studio) and load the model.
2. Double-click  Start.bat              this computer only; opens the local
                                        console http://127.0.0.1:8781/
   or            Start-With-Tunnel.bat  also two public https addresses
                                        (account website + API link)
   The first start asks for the admin password and creates config.yaml.
3. In config.yaml set   model: <the model name>   (applies without restart).

Add an account:     Add-User.bat friend1   (tier: edit config.yaml)
Check backends:     Check.bat
New API link:       Rotate-API-Link.bat    (API keys stay valid)
Encrypted mode:     Start-Worker.bat       (worker on another computer)
                    Start-Client-Proxy.bat (the user's side, 127.0.0.1:8788)
Other commands:     Command-Prompt.bat, then e.g.  llmop usage / llmop keys
Stop:               close the black window

WARNING: the "data" folder holds the database AND private keys
(data\\llmop_home: worker and client keys). Do not share, upload or send it.

Update: unzip the new version into a NEW folder, then copy config.yaml and the
whole data folder from the old one.

Docs: docs\\README.md (start here), docs\\HANDOFF.md
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


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_archive(path: Path, expected: str) -> bool:
    actual = sha256_file(path)
    if actual != expected.lower():
        log(f"REFUSED: {path.name} has sha256 {actual}")
        log(f"         expected                {expected.lower()}")
        log("         wrong or tampered file; nothing was built.")
        return False
    log(f"python archive sha256 OK ({actual})")
    return True


def crlf(text: str) -> bytes:
    return text.replace("\r\n", "\n").replace("\n", "\r\n").encode("ascii")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--wheel", help="llmop wheel (default: newest dist/*.whl of this version)")
    ap.add_argument("--python-archive", help="local python-build-standalone .tar.gz (default: download)")
    ap.add_argument("--python-url", default=PYTHON_URL)
    ap.add_argument("--python-sha256", default=PYTHON_SHA256,
                    help="expected sha256 of the Python archive (default: the pinned one)")
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
    name = f"llmop-{ver}"
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)

        # 1. Python runtime (checked before anything is unpacked or installed)
        archive = Path(args.python_archive) if args.python_archive else tmp / "python.tar.gz"
        if not args.python_archive:
            fetch(args.python_url, archive)
        if not verify_archive(archive, args.python_sha256):
            return 1
        stage = tmp / name
        stage.mkdir()
        with tarfile.open(archive) as tf:
            tf.extractall(stage, filter="data") if sys.version_info >= (3, 12) else tf.extractall(stage)
        py_dir = stage / "python"
        if not (py_dir / "python.exe").exists():
            log("archive has no python/python.exe - wrong file?")
            return 1

        # 2. llmop[server,secure] + dependencies as Windows wheels, unpacked into site-packages
        site = py_dir / "Lib" / "site-packages"
        site.mkdir(parents=True, exist_ok=True)
        log("installing llmop[server,secure] + dependencies (win_amd64 wheels)")
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--quiet", "--no-compile",
             "--disable-pip-version-check", "--target", str(site), "--platform", "win_amd64",
             "--python-version", PY_VERSION, "--implementation", "cp",
             "--only-binary=:all:", "--upgrade", f"{wheel.resolve()}[server,secure]", *WINDOWS_ONLY],
            check=True,
        )
        # pip --target writes Linux-style console scripts; the .bat files use "python -m llmop".
        shutil.rmtree(site / "bin", ignore_errors=True)
        missing = [m for m in REQUIRED if not (site / m).is_dir()]
        if missing:
            log(f"missing in site-packages: {', '.join(missing)}")
            return 1

        # 3. launchers, docs, example config
        for bat in sorted((ROOT / "scripts/windows/portable").glob("*.bat")):
            (stage / bat.name).write_bytes(crlf(bat.read_text(encoding="ascii")))
        shutil.copy2(ROOT / "config.example.yaml", stage / "config.example.yaml")
        (stage / "README.txt").write_bytes(README_TXT.format(version=ver).replace("\n", "\r\n").encode("utf-8-sig"))
        docs = stage / "docs"
        docs.mkdir()
        for rel in DOCS:
            src = ROOT / rel
            if src.exists():
                dest = docs / ("SECURE-" + src.name if rel.startswith("docs/secure/") else src.name)
                shutil.copy2(src, dest)

        # 4. zip
        out_dir.mkdir(parents=True, exist_ok=True)
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
