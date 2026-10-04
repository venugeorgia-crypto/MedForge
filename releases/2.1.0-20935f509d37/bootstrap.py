#!/usr/bin/env python3
"""Installation support embedded in MEDFORGE-ONE-COMMAND.command."""
import fcntl, hashlib, importlib.util, json, os, platform, py_compile, shlex, shutil, subprocess, sys, tempfile, time, venv
from pathlib import Path
APP=Path(__file__).resolve().parent
BASE=Path(os.environ.get("MEDFORGE_HOME",str(Path.home()/"MedForge"))).expanduser().resolve()
VENV=BASE/".venv-v2.1"
BIN=Path.home()/"bin"
INSTALLER=Path(os.environ["MEDFORGE_INSTALLER_SOURCE"]).resolve()

def run(argv, **kwargs):
    return subprocess.run([str(x) for x in argv], check=True, **kwargs)

def atomic(path, text, mode=0o600):
    fd,tmp=tempfile.mkstemp(dir=path.parent,prefix=".medforge-")
    try:
        with os.fdopen(fd,"w",encoding="utf-8") as f: f.write(text)
        os.chmod(tmp,mode); os.replace(tmp,path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)

def install():
    if platform.system()!="Darwin": raise RuntimeError("This installer targets macOS. Use --verify to inspect it on another OS.")
    if not (3,10) <= sys.version_info[:2] <= (3,14): raise RuntimeError("Python 3.10-3.14 is required.")
    for p in [BASE,BASE/"docs",BASE/"products",BASE/"database",BASE/"logs",BASE/"backups",BIN]: p.mkdir(parents=True,exist_ok=True)
    with open(BASE/".install.lock","a+") as lock:
        try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: raise RuntimeError("A MedForge installation is already running.") from None
        # Do not modify package files while a product is being generated.
        with open(BASE/".job.lock","a+") as job:
            try: fcntl.flock(job,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError: raise RuntimeError("Let the current MedForge product finish before upgrading.") from None
            if shutil.disk_usage(BASE).free < 2*1024**3: raise RuntimeError("Installation needs at least 2 GB free; new model downloads need more.")
            print("[1/5] Checking the private Python environment...",flush=True)
            py=VENV/"bin/python"
            if not py.exists():
                if VENV.exists(): VENV.rename(BASE/"backups"/("venv-v2.1-"+str(time.time_ns())))
                venv.EnvBuilder(with_pip=True).create(VENV)
            run([py,"-c","import sqlite3; c=sqlite3.connect(':memory:'); c.execute('CREATE VIRTUAL TABLE t USING fts5(text)')"])
            print("[2/5] Installing required packages...",flush=True)
            requirements=APP/"requirements.txt"
            digest=hashlib.sha256(requirements.read_bytes()).hexdigest()
            marker=VENV/"medforge-requirements.sha256"
            modules="import requests,chromadb,pypdf,reportlab,PIL,streamlit,ddgs,trafilatura,genanki,psutil"
            installed=subprocess.run([py,"-c",modules],capture_output=True).returncode==0
            if not installed or not marker.exists() or marker.read_text()!=digest:
                run([py,"-m","pip","install","--disable-pip-version-check","--upgrade","pip"])
                run([py,"-m","pip","install","--disable-pip-version-check","-r",requirements])
                run([py,"-c",modules]); atomic(marker,digest)
            for p in APP.glob("*.py"): py_compile.compile(str(p),doraise=True)
            print("[3/5] Checking Ollama...",flush=True)
            if not shutil.which("ollama"):
                app_cli=Path("/Applications/Ollama.app/Contents/Resources/ollama")
                if app_cli.exists():
                    link=BIN/"ollama"
                    if not link.exists() and not link.is_symlink(): link.symlink_to(app_cli)
                    os.environ["PATH"]=str(BIN)+os.pathsep+os.environ.get("PATH","")
                elif shutil.which("brew"): run(["brew","install","ollama"])
                else: raise RuntimeError("Ollama was not found and Homebrew is unavailable. Install Ollama from https://ollama.com/download, then rerun this file.")
            print("[4/5] Activating the release and one-word commands...",flush=True)
            current=BASE/"current"
            if current.exists() and not current.is_symlink(): raise RuntimeError("~/MedForge/current already exists as a real folder; it was left untouched.")
            if current.is_symlink():
                atomic(BASE/"previous-release.txt",str(current.resolve()))
            database=BASE/"database"
            if any(database.iterdir()) and (not current.is_symlink() or current.resolve()!=APP):
                print("Saving the existing V2 evidence database before activation...",flush=True)
                backup_path=BASE/"backups"/("database-"+str(time.time_ns()))
                shutil.copytree(database,backup_path)
                # Snapshot SQLite pages consistently, including committed WAL data.
                import sqlite3
                for original in database.rglob("*.sqlite3"):
                    target=backup_path/original.relative_to(database)
                    with sqlite3.connect(original) as source, sqlite3.connect(target) as dest: source.backup(dest)
            nextlink=BASE/(".next-release-"+str(os.getpid()))
            nextlink.symlink_to(APP,target_is_directory=True)
            os.replace(nextlink,current)
            backup=BASE/"backups"/("commands-"+str(time.time_ns())); backup.mkdir()
            for name,mode in {"product":"product","medforge":"dashboard","medstudy":"study","medask":"ask"}.items():
                path=BIN/name
                if path.exists(): shutil.copy2(path,backup/name)
                text='#!/bin/bash\nset -euo pipefail\nexport PATH="$HOME/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"\nexport PYTHONUNBUFFERED=1\nBASE="${MEDFORGE_HOME:-$HOME/MedForge}"\n'
                text+='if [ ! -x "$BASE/.venv-v2.1/bin/python" ]; then\n  bash "$BASE/INSTALL-MEDFORGE.command" --no-open\nfi\n'
                text+='exec "$BASE/.venv-v2.1/bin/python" "$BASE/current/medforge_launch.py" '+mode+' "$@"\n'
                atomic(path,text,0o755)
            for rc_name in [".zshrc",".bash_profile"]:
                rc=Path.home()/rc_name
                previous=rc.read_text() if rc.exists() else ""
                entry='export PATH="$HOME/bin:$PATH"'
                if entry not in previous:
                    if rc.exists(): shutil.copy2(rc,backup/rc_name)
                    # Preserve permissions and symlinks in existing shell configuration.
                    with open(rc,"a",encoding="utf-8") as f: f.write('\n# MedForge commands\n'+entry+'\n')
            atomic(BASE/"MEDFORGE.command",'#!/bin/bash\nexec "$HOME/bin/medforge" "$@"\n',0o755)
            target=BASE/"INSTALL-MEDFORGE.command"
            if INSTALLER!=target: atomic(target,INSTALLER.read_text(),0o755)
        print("[5/5] Testing chat generation, embeddings and the evidence store...",flush=True)
        doctor=subprocess.run([py,"-u",APP/"medforge_core.py","doctor"])
        if doctor.returncode:
            print("MedForge is installed; AI readiness is incomplete. Saved data is retained. PRODUCT will retry the model check.",flush=True)
        else: print("MedForge ready. In a new Terminal: product \"cardiac cycle\"",flush=True)
    args=[a for a in sys.argv[1:] if a!="--no-open"]
    if "--no-open" not in sys.argv[1:]:
        mode="product" if args else "dashboard"
        run([py,APP/"medforge_launch.py",mode,*args])

if __name__=="__main__":
    try: install()
    except Exception as e:
        print("\nInstallation stopped:",e,file=sys.stderr)
        print("Fix the reported prerequisite and rerun the same command. Existing study files were retained.",file=sys.stderr)
        raise SystemExit(1)
