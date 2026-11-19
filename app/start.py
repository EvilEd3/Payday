"""Container entry point: give the data folder to the app's own user, drop root, then start the web server.

Running as an ordinary user means that if the app were ever tricked into running code, it couldn't touch the
rest of the container as root. If the data folder can't be handed over (some NAS permission setups refuse),
the app says so in its log and carries on as root, so it never fails to start because of this.
Set RUN_AS_ROOT=1 to skip this step entirely.
"""
import os
import sys

APP_UID = APP_GID = 10001
DATA = os.getenv("DATA_DIR", "/data")
SERVER = ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-server-header"]


def log(msg: str) -> None:
    print(f"[payday] {msg}", file=sys.stderr, flush=True)


def hand_over_data() -> bool:
    try:
        for root, dirs, files in os.walk(DATA):
            for name in [root] + [os.path.join(root, f) for f in files]:
                st = os.lstat(name)
                if (st.st_uid, st.st_gid) != (APP_UID, APP_GID):
                    os.lchown(name, APP_UID, APP_GID)
        key = os.path.join(DATA, "payday.key")
        if os.path.exists(key):
            os.chmod(key, 0o600)
        return True
    except OSError as e:
        log(f"Couldn't give {DATA} to the app's user ({e.strerror}); running as root instead.")
        return False


if os.getuid() == 0 and os.getenv("RUN_AS_ROOT") != "1":
    os.makedirs(DATA, exist_ok=True)
    if hand_over_data():
        os.setgroups([])
        os.setgid(APP_GID)
        os.setuid(APP_UID)
        os.environ["HOME"] = "/srv"
        log("Running as the app's own user, not root.")
os.umask(0o077)  # new files (database, key) are readable by the app only
os.execvp(SERVER[0], SERVER)
