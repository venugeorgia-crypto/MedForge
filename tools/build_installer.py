#!/usr/bin/env python3
"""Rebuild INSTALL-MEDFORGE.command from the active release plus core modules.

The installer is a self-extracting macOS script:

    bash preamble  ->  finds Python 3.10-3.14
    Python payload ->  embeds {app, base} files as base64(gzip(JSON)), verifies
                       the payload SHA-256, writes the app files into a
                       content-addressed release directory, writes the core
                       V3 database modules into MEDFORGE_HOME/core, then runs
                       bootstrap.py from that release.

This builder reads the payload files from the checkout (release files from the
`current` symlink target, core modules from `core/`), keeps the existing bash
preamble verbatim, and rewrites INSTALL-MEDFORGE.command deterministically so
the same sources always produce the same release tag (2.1.0-<sha256[:12]>).

Usage:
    .venv-v2.1/bin/python tools/build_installer.py
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import os
import re
import sys
import tempfile
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "INSTALL-MEDFORGE.command"
RELEASE_SRC = (ROOT / "current").resolve()
CORE_SRC = ROOT / "core"
HEREDOC_MARKER = "MEDFORGE_PAYLOAD"

# Flat files beside the Python modules that belong to the release payload.
NAMED_EXTRAS = ("requirements.txt", "README.md", "MEDFORGE-ASTRA-MASTER-PROMPT.md")

PAYLOAD_CODE = r'''import base64, gzip, hashlib, json, os, platform, runpy, sys, tempfile
from pathlib import Path
PAYLOAD="""@@PAYLOAD@@"""
EXPECTED="@@EXPECTED@@"
PREFIX="@@PREFIX@@"
raw=base64.b64decode(PAYLOAD)
if hashlib.sha256(raw).hexdigest()!=EXPECTED:
    raise SystemExit("Installer payload is damaged; download the file again.")
tree=json.loads(gzip.decompress(raw).decode())
app_files=tree.get("app"); base_files=tree.get("base")
if not isinstance(app_files,dict) or not isinstance(base_files,dict) or not app_files:
    raise SystemExit("Installer payload structure is invalid.")
for name,text in [*app_files.items(),*base_files.items()]:
    rel=Path(name)
    if rel.is_absolute() or ".." in rel.parts or not isinstance(text,str):
        raise SystemExit("Unsafe embedded file: "+str(name))
    if name.endswith(".py"): compile(text,name,"exec")
installer=Path(sys.argv[1]).expanduser().resolve()
args=sys.argv[2:]
if args==["--verify"]:
    print("Verified payload SHA-256:",EXPECTED)
    print("Verified embedded files:",len(app_files),"app +",len(base_files),"core")
    print("  app:",", ".join(sorted(app_files)))
    print("  core:",", ".join(sorted(base_files)))
    raise SystemExit(0)
if platform.system()!="Darwin": raise SystemExit("This installer targets macOS. Use --verify for read-only payload checks.")
base=Path(os.environ.get("MEDFORGE_HOME",str(Path.home()/"MedForge"))).expanduser().resolve()
release=base/"releases"/(PREFIX+"-"+EXPECTED[:12])
release.mkdir(parents=True,exist_ok=True)
def write_file(path,text):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(dir=path.parent,prefix=".install-")
    with os.fdopen(fd,"w",encoding="utf-8") as stream: stream.write(text)
    os.chmod(tmp,0o600); os.replace(tmp,path)
for name,text in sorted(app_files.items()):
    path=release/name
    if path.exists():
        if path.read_text(encoding="utf-8")!=text: raise SystemExit("Existing release was modified: "+str(path))
        continue
    write_file(path,text)
# Core V3 database modules live at MEDFORGE_HOME/core: medforge.storage and
# medforge.learner import them after adding T.BASE to sys.path at call time.
for name,text in sorted(base_files.items()):
    path=base/name
    if path.exists() and path.read_text(encoding="utf-8")==text: continue
    write_file(path,text)
os.environ["MEDFORGE_INSTALLER_SOURCE"]=str(installer)
sys.path.insert(0,str(release))
sys.argv=[str(release/"bootstrap.py"),*args]
runpy.run_path(str(release/"bootstrap.py"),run_name="__main__")
'''


def read_version_prefix() -> str:
    """Release directory prefix, e.g. '2.1.0' taken from medforge.types.VERSION."""
    text = (RELEASE_SRC / "medforge" / "types.py").read_text(encoding="utf-8")
    match = re.search(r'VERSION\s*:\s*Final\[str\]\s*=\s*"([0-9.]+)"', text)
    return match.group(1) if match else "2.1.0"


def collect_app_files() -> dict[str, str]:
    if not RELEASE_SRC.is_dir() or RELEASE_SRC.parent != ROOT / "releases":
        sys.exit(f"Refusing to read release files from {RELEASE_SRC} (expected under {ROOT / 'releases'}).")
    files: dict[str, str] = {}
    for path in sorted(RELEASE_SRC.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(RELEASE_SRC)
        if any(part == "__pycache__" or part.startswith(".") for part in rel.parts):
            continue
        if rel.suffix != ".py" and rel.as_posix() not in NAMED_EXTRAS:
            continue
        files[rel.as_posix()] = path.read_text(encoding="utf-8")
    if "medforge_core.py" not in files or "bootstrap.py" not in files:
        sys.exit("Active release is missing medforge_core.py or bootstrap.py; nothing to package.")
    return files


def collect_core_files() -> dict[str, str]:
    files: dict[str, str] = {}
    for path in sorted(CORE_SRC.rglob("*.py")):
        rel = path.relative_to(ROOT)
        if any(part == "__pycache__" or part.startswith(".") for part in rel.parts):
            continue
        files[rel.as_posix()] = path.read_text(encoding="utf-8")
    if "core/database/schema.py" not in files:
        sys.exit("core/database/schema.py is missing; the V3 DDL is required for installs.")
    return files


def split_preamble(text: str) -> str:
    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if line.strip().endswith(f"<<'{HEREDOC_MARKER}'"):
            return "".join(lines[: index + 1])
    sys.exit("Could not find the MEDFORGE_PAYLOAD heredoc marker in the existing installer.")


def build() -> tuple[str, str, int, int]:
    preamble = split_preamble(INSTALLER.read_text(encoding="utf-8"))
    app_files = collect_app_files()
    base_files = collect_core_files()

    blob = json.dumps(
        {"app": app_files, "base": base_files},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    compressed = gzip.compress(blob, compresslevel=9, mtime=0)
    digest = hashlib.sha256(compressed).hexdigest()
    wrapped = "\n".join(textwrap.wrap(base64.b64encode(compressed).decode("ascii"), 110))

    payload = (
        PAYLOAD_CODE
        .replace("@@PAYLOAD@@", wrapped)
        .replace("@@EXPECTED@@", digest)
        .replace("@@PREFIX@@", read_version_prefix())
    )
    script = preamble + payload + HEREDOC_MARKER + "\n"

    fd, tmp = tempfile.mkstemp(dir=INSTALLER.parent, prefix=".installer-")
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(script)
    os.chmod(tmp, 0o755)
    os.replace(tmp, INSTALLER)
    return digest, read_version_prefix(), len(app_files), len(base_files)


if __name__ == "__main__":
    digest, prefix, app_count, base_count = build()
    print(f"Rebuilt {INSTALLER.name}")
    print(f"  Release tag:    {prefix}-{digest[:12]}")
    print(f"  Payload SHA-256: {digest}")
    print(f"  Embedded:       {app_count} app files + {base_count} core files")
    print(f"  Size:           {INSTALLER.stat().st_size:,} bytes")
