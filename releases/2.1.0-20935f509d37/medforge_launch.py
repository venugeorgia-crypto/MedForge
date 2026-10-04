#!/usr/bin/env python3
"""Local launcher. Never binds the dashboard outside loopback."""
import importlib.util, json, os, re, secrets, socket, subprocess, sys, time, urllib.request, webbrowser
from pathlib import Path
APP=Path(__file__).resolve().parent
BASE=Path(os.environ.get("MEDFORGE_HOME",str(Path.home()/"MedForge"))).expanduser().resolve()
LOGS=BASE/"logs"
MODULES=["requests","chromadb","pypdf","reportlab","PIL","streamlit","ddgs","trafilatura","genanki","psutil"]
def dependencies():
    if any(importlib.util.find_spec(m) is None for m in MODULES):
        print("Repairing missing MedForge packages...",flush=True)
        subprocess.run([sys.executable,"-m","pip","install","--disable-pip-version-check","-r",str(APP/"requirements.txt")],check=True)
def healthy(port, instance):
    try:
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        if not re.fullmatch(r"medforge-[a-f0-9]{24}", instance): return False
        with opener.open(f"http://127.0.0.1:{port}/{instance}/_stcore/health",timeout=2) as r:
            return r.status==200 and r.read().strip()==b"ok"
    except Exception: return False
def dashboard():
    import medforge_core as mf
    with open(BASE/".dashboard.lock","a+") as lock:
        import fcntl
        fcntl.flock(lock,fcntl.LOCK_EX)
        statefile=BASE/"dashboard-runtime.json"
        try:
            state=json.loads(statefile.read_text())
            if state.get("app")==str(APP) and healthy(int(state["port"]),state.get("instance", "")):
                url=f"http://127.0.0.1:{int(state['port'])}/{state['instance']}"
                webbrowser.open(url); print("MedForge:",url); return
        except (OSError,ValueError,KeyError): pass
        port=None
        for candidate in range(8501,8521):
            with socket.socket() as sock:
                try: sock.bind(("127.0.0.1",candidate)); port=candidate; break
                except OSError: pass
        if port is None: raise RuntimeError("No free local dashboard port in 8501-8520.")
        instance="medforge-"+secrets.token_hex(12)
        cmd=[sys.executable,"-m","streamlit","run",str(APP/"dashboard.py"),
             "--server.address=127.0.0.1",f"--server.port={port}","--server.headless=true",
             "--server.enableCORS=true","--server.enableXsrfProtection=true",
             "--server.maxUploadSize=50","--browser.gatherUsageStats=false",f"--server.baseUrlPath={instance}"]
        with open(LOGS/"dashboard.log","ab") as log:
            process=subprocess.Popen(cmd,cwd=APP,stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True)
        for _ in range(90):
            if process.poll() is not None: raise RuntimeError(f"Dashboard exited. Details: {LOGS/'dashboard.log'}")
            if healthy(port,instance):
                mf.atomic_text(statefile,json.dumps({"pid":process.pid,"port":port,"app":str(APP),"instance":instance}))
                url=f"http://127.0.0.1:{port}/{instance}"
                webbrowser.open(url); print("MedForge:",url); return
            time.sleep(0.5)
        raise RuntimeError(f"Dashboard is taking longer than expected. Details: {LOGS/'dashboard.log'}")
def main():
    LOGS.mkdir(parents=True,exist_ok=True)
    dependencies()
    mode=sys.argv[1] if len(sys.argv)>1 else "dashboard"
    args=sys.argv[2:]
    if mode=="dashboard" and args and args[0] in ("status","doctor"): mode,args=args[0],args[1:]
    if mode=="dashboard": dashboard(); return
    raise SystemExit(subprocess.call([sys.executable,"-u",str(APP/"medforge_core.py"),mode,*args]))
if __name__=="__main__":
    try: main()
    except KeyboardInterrupt: raise SystemExit(130)
    except Exception as e:
        print("MedForge:",e,file=sys.stderr); raise SystemExit(1)
