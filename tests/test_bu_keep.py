"""BU_Keep_* files are frozen legacy backups: they must exist and never change (see docs/BU_Keep_README.md)."""
import hashlib
from pathlib import Path

DOCS = Path(__file__).resolve().parents[1] / "docs"


def test_frozen_files_unchanged():
    rows = [r.split(None, 1) for r in (DOCS / "BU_Keep.sha256").read_text().splitlines() if r.strip()]
    assert rows, "BU_Keep.sha256 must list at least one frozen file"
    for digest, name in rows:
        path = DOCS / name.strip().lstrip("*")
        assert path.exists(), f"frozen file deleted or renamed: {path.name}"
        # Hash the repository bytes (LF): a Windows checkout with core.autocrlf turns the
        # text files into CRLF without changing their content.
        data = path.read_bytes().replace(b"\r\n", b"\n")
        assert hashlib.sha256(data).hexdigest() == digest, f"frozen file modified: {path.name}"


def test_every_bu_keep_file_is_listed():
    listed = {r.split(None, 1)[1].strip().lstrip("*") for r in (DOCS / "BU_Keep.sha256").read_text().splitlines() if r.strip()}
    on_disk = {p.name for p in DOCS.glob("BU_Keep_*_legacy.*")}
    assert on_disk <= listed, f"unregistered frozen files: {on_disk - listed}"
