"""Set volume permissions at startup, then permanently drop root privileges."""
import os
import pwd
import sys
from pathlib import Path

ROOT = Path("/home/appuser/.tradingagents")
mounted = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "")
if mounted and Path(mounted).resolve() != ROOT.resolve():
    raise SystemExit("The Railway volume mount path is not the expected application data directory.")
if os.geteuid() != 0:
    raise SystemExit("Volume bootstrap requires initial root permissions.")
user = pwd.getpwnam("appuser")
ROOT.mkdir(parents=True, exist_ok=True)

# The mounted volume belongs to root on its first startup. Own only the
# application data tree and run the HTTP server + agents as unprivileged user.
for directory, directories, files in os.walk(ROOT):
    os.chown(directory, user.pw_uid, user.pw_gid)
    for name in files:
        p = Path(directory) / name
        if not p.is_symlink():
            os.chown(p, user.pw_uid, user.pw_gid)
os.setgroups([])
os.setgid(user.pw_gid)
os.setuid(user.pw_uid)
os.chdir("/home/appuser/app")
os.execv(sys.executable, [sys.executable, "-u", "lab_server.py"])
