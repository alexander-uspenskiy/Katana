"""Platform support: the few places where Windows, macOS and Linux differ.

File locks, the secret store for API keys, desktop notifications, the native folder dialog, process
checks and detached background workers. Every other module calls these instead of fcntl, `security`,
osascript or os.kill, so the rest of Katana stays the same on every platform.

macOS uses the login keychain, osascript and launchd. Windows uses Credential Manager, PowerShell toasts and
Task Scheduler (see service.py). Linux uses secret-tool, notify-send and zenity when they are installed.
"""
from __future__ import annotations

import base64
import os
import subprocess
import sys
import time
from pathlib import Path

IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"

if IS_WIN:
    import msvcrt
else:
    import fcntl

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # keeps PowerShell and schtasks from flashing a console


def ensure_utf8() -> None:
    """Windows opens text files in the system code page unless UTF-8 mode is on, which breaks the arrows and
    dashes in agent output. Restarts this script once in UTF-8 mode; children inherit PYTHONUTF8."""
    if IS_WIN and not sys.flags.utf8_mode:
        env = dict(os.environ, PYTHONUTF8="1")
        raise SystemExit(subprocess.call([sys.executable, *sys.argv], env=env))
    os.environ.setdefault("PYTHONUTF8", "1")


def python_command() -> str:
    """The command that runs Katana's scripts: `python3` on macOS and Linux, `python` on Windows."""
    return "python" if IS_WIN else "python3"


def background_python() -> str:
    """The interpreter for background jobs: pythonw.exe on Windows, so no console window opens."""
    if IS_WIN:
        pyw = Path(sys.executable).with_name("pythonw.exe")
        if pyw.exists():
            return str(pyw)
    return sys.executable


def redirect_std_streams(log_path: Path) -> None:
    """pythonw has no stdout or stderr; sends them to `log_path` so print and tracebacks are kept."""
    if sys.stdout is None or sys.stderr is None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log = open(log_path, "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stdout or log
        sys.stderr = sys.stderr or log


# ---------------------------------------------------------------- file locks

def lock(f, blocking: bool = False) -> None:
    """Takes an exclusive lock on the open file `f`, held until unlock() or until the file is closed.
    Raises BlockingIOError when `blocking` is False and another process holds it."""
    if IS_WIN:
        while True:
            f.seek(0)
            try:
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError as e:
                if not blocking:
                    raise BlockingIOError(str(e)) from e
                time.sleep(0.1)
    else:
        fcntl.flock(f, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))


def unlock(f) -> None:
    if IS_WIN:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(f, fcntl.LOCK_UN)


# ---------------------------------------------------------------- processes

def pid_alive(pid) -> bool:
    """True while a process with this id exists. Never signals it: on Windows os.kill(pid, 0) would end it."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if IS_WIN:
        import ctypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # access denied: it exists but belongs to someone else
        code = ctypes.c_ulong()
        ok = kernel.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code))
        kernel.CloseHandle(ctypes.c_void_p(handle))
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def spawn_detached(cmd: list, cwd, stderr) -> None:
    """Starts `cmd` in the background so it outlives this process and whatever started it."""
    kwargs = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": stderr, "cwd": str(cwd)}
    if not IS_WIN:
        subprocess.Popen(cmd, start_new_session=True, **kwargs)
        return
    # A hidden console (not DETACHED_PROCESS) so git, claude and the other tools it starts don't each open a window.
    flags = subprocess.CREATE_NEW_PROCESS_GROUP | NO_WINDOW
    try:  # leave Task Scheduler's job, or the task would stay "running" for as long as the worker does
        subprocess.Popen(cmd, creationflags=flags | 0x01000000, **kwargs)  # CREATE_BREAKAWAY_FROM_JOB
    except OSError:
        subprocess.Popen(cmd, creationflags=flags, **kwargs)  # the job doesn't allow breakaway


# ---------------------------------------------------------------- secrets (API keys)

def secret_store_name() -> str:
    return "macOS login keychain" if IS_MAC else "Windows Credential Manager" if IS_WIN else "system keyring (secret-tool)"


def secret_get(service: str, account: str) -> str | None:
    try:
        if IS_WIN:
            return _cred_read(f"{service}/{account}")
        if IS_MAC:
            out = subprocess.run(["security", "find-generic-password", "-s", service, "-a", account, "-w"],
                                 capture_output=True, text=True, timeout=30)
        else:
            out = subprocess.run(["secret-tool", "lookup", "service", service, "account", account],
                                 capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return (out.stdout.strip() or None) if out.returncode == 0 else None


def secret_set(service: str, account: str, label: str, value: str) -> str | None:
    """Saves or replaces a secret. Returns an error message, or None. `value` must not contain quotes or spaces."""
    try:
        if IS_WIN:
            return _cred_write(f"{service}/{account}", label, value)
        if IS_MAC:  # security -i reads the command from stdin, so the key never shows in the process list
            line = f'add-generic-password -U -s "{service}" -a "{account}" -l "{label}" -w "{value}"\n'
            out = subprocess.run(["security", "-i"], input=line, capture_output=True, text=True, timeout=30)
        else:
            out = subprocess.run(["secret-tool", "store", f"--label={label}", "service", service, "account", account],
                                 input=value, capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        return "secret-tool isn't installed. Install libsecret-tools to store keys." if not IS_MAC else \
            "The security command wasn't found."
    except (OSError, subprocess.SubprocessError) as e:
        return f"Couldn't reach the {secret_store_name()}: {e}"
    if out.returncode != 0 or secret_get(service, account) != value:
        return (out.stderr.strip() or f"The {secret_store_name()} didn't save the key.")[:300]
    return None


def secret_delete(service: str, account: str) -> None:
    try:
        if IS_WIN:
            _cred_delete(f"{service}/{account}")
        elif IS_MAC:
            subprocess.run(["security", "delete-generic-password", "-s", service, "-a", account],
                           capture_output=True, timeout=30)
        else:
            subprocess.run(["secret-tool", "clear", "service", service, "account", account],
                           capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        pass


def _cred_api():
    """advapi32's credential functions, with their argument types, plus the CREDENTIAL structure."""
    import ctypes
    from ctypes import wintypes

    class CREDENTIAL(ctypes.Structure):
        _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD), ("TargetName", wintypes.LPWSTR),
                    ("Comment", wintypes.LPWSTR), ("LastWritten", wintypes.FILETIME),
                    ("CredentialBlobSize", wintypes.DWORD), ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                    ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
                    ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR)]

    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    adv.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                              ctypes.POINTER(ctypes.POINTER(CREDENTIAL))]
    adv.CredReadW.restype = wintypes.BOOL
    adv.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIAL), wintypes.DWORD]
    adv.CredWriteW.restype = wintypes.BOOL
    adv.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    adv.CredDeleteW.restype = wintypes.BOOL
    adv.CredFree.argtypes = [ctypes.c_void_p]
    adv.CredFree.restype = None
    return ctypes, adv, CREDENTIAL


CRED_TYPE_GENERIC, CRED_PERSIST_LOCAL_MACHINE = 1, 2


def _cred_read(target: str) -> str | None:
    ctypes, adv, CREDENTIAL = _cred_api()
    ptr = ctypes.POINTER(CREDENTIAL)()
    if not adv.CredReadW(target, CRED_TYPE_GENERIC, 0, ctypes.byref(ptr)):
        return None
    try:
        cred = ptr.contents
        raw = bytes(ctypes.cast(cred.CredentialBlob, ctypes.POINTER(ctypes.c_ubyte * cred.CredentialBlobSize)).contents)
        return raw.decode("utf-16-le").strip() or None
    finally:
        adv.CredFree(ptr)


def _cred_write(target: str, label: str, value: str) -> str | None:
    ctypes, adv, CREDENTIAL = _cred_api()
    blob = value.encode("utf-16-le")
    buf = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
    cred = CREDENTIAL()
    cred.Type, cred.TargetName, cred.Comment = CRED_TYPE_GENERIC, target, label
    cred.CredentialBlobSize, cred.CredentialBlob = len(blob), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))
    cred.Persist, cred.UserName = CRED_PERSIST_LOCAL_MACHINE, "katana"
    if not adv.CredWriteW(ctypes.byref(cred), 0):
        return f"Credential Manager refused the key (error {ctypes.get_last_error()})."
    return None if _cred_read(target) == value else "Credential Manager didn't save the key."


def _cred_delete(target: str) -> None:
    _, adv, _ = _cred_api()
    adv.CredDeleteW(target, CRED_TYPE_GENERIC, 0)


# ---------------------------------------------------------------- PowerShell

def powershell(script: str, env: dict | None = None, sta: bool = False, timeout: int = 60):
    """Runs a PowerShell script (Windows PowerShell 5.1, which ships with Windows). Values go in `env`
    and are read as $env:NAME in the script, so they never become part of the script's text."""
    encoded = base64.b64encode(script.encode("utf-16-le")).decode()
    cmd = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass"]
    if sta:
        cmd.append("-STA")
    cmd += ["-EncodedCommand", encoded]
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                          env=dict(os.environ, **(env or {})), creationflags=NO_WINDOW)


# ---------------------------------------------------------------- desktop notifications

# The message travels as an argument, never as AppleScript source, so its text can't inject commands.
APPLESCRIPT = ["-e", "on run argv", "-e",
               "display notification (item 1 of argv) with title (item 2 of argv) subtitle (item 3 of argv)",
               "-e", "end run"]

TOAST_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[void][Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
[void][Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime]
$lines = @($env:KATANA_N_TITLE, $env:KATANA_N_SUBTITLE, $env:KATANA_N_MESSAGE) | Where-Object { $_ }
$text = ($lines | ForEach-Object { '<text>' + [System.Security.SecurityElement]::Escape($_) + '</text>' }) -join ''
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml("<toast><visual><binding template='ToastGeneric'>$text</binding></visual></toast>")
$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show([Windows.UI.Notifications.ToastNotification]::new($xml))
"""


def notification_name() -> str:
    return "macOS Notification Center" if IS_MAC else "Windows notifications" if IS_WIN else "desktop notifications"


def desktop_notify(title: str, subtitle: str, message: str) -> None:
    """Shows one notification. Raises OSError or SubprocessError when the platform can't."""
    if IS_MAC:
        osascript = _which("osascript") or "/usr/bin/osascript"
        subprocess.run([osascript, *APPLESCRIPT, message[:300], title[:80], subtitle[:120]],
                       capture_output=True, timeout=15, check=True)
    elif IS_WIN:
        out = powershell(TOAST_SCRIPT, {"KATANA_N_TITLE": title[:80], "KATANA_N_SUBTITLE": subtitle[:120],
                                        "KATANA_N_MESSAGE": message[:300]}, timeout=30)
        if out.returncode != 0:
            raise OSError((out.stderr or out.stdout).strip()[:300] or "PowerShell couldn't show the toast")
    else:
        notify_send = _which("notify-send")
        if not notify_send:
            raise OSError("notify-send isn't installed")
        subprocess.run([notify_send, title[:80], "\n".join(x for x in (subtitle[:120], message[:300]) if x)],
                       capture_output=True, timeout=15, check=True)


def _which(name: str) -> str | None:
    import shutil
    return shutil.which(name)


# ---------------------------------------------------------------- folder dialog

FOLDER_SCRIPT = r"""
Add-Type -AssemblyName System.Windows.Forms
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.Description = 'Choose a Katana project folder'
$dialog.ShowNewFolderButton = $true
$owner = New-Object System.Windows.Forms.Form
$owner.TopMost = $true
if ($dialog.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) { [Console]::Out.Write($dialog.SelectedPath) } else { exit 3 }
"""


def folder_dialog_name() -> str:
    return "macOS folder dialog on this Mac" if IS_MAC else "Windows folder dialog on this PC" if IS_WIN \
        else "folder dialog on this computer"


def choose_folder() -> dict:
    """Shows the native folder dialog and waits for the user. {"ok", "path"}, {"cancelled"} or {"errors"}."""
    try:
        if IS_MAC:
            script = 'POSIX path of (choose folder with prompt "Choose a Katana project folder")'
            out = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=600)
            cancelled = "-128" in out.stderr  # User canceled.
        elif IS_WIN:
            out = powershell(FOLDER_SCRIPT, sta=True, timeout=600)
            cancelled = out.returncode == 3
        else:
            zenity = _which("zenity")
            if not zenity:
                return {"ok": False, "errors": ["Install zenity for a folder dialog, or type the folder's path."]}
            out = subprocess.run([zenity, "--file-selection", "--directory", "--title=Choose a Katana project folder"],
                                 capture_output=True, text=True, timeout=600)
            cancelled = out.returncode == 1
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"ok": False, "errors": [f"Couldn't open the folder dialog: {e}"]}
    if out.returncode != 0:
        if cancelled:
            return {"ok": False, "cancelled": True}
        return {"ok": False, "errors": [out.stderr.strip() or "Couldn't open the folder dialog."]}
    path = out.stdout.strip()
    return {"ok": True, "path": str(Path(path)) if IS_WIN else path.rstrip("/") or "/"}


def example_path() -> str:
    return r"C:\Users\you\Projects\my-app" if IS_WIN else "/Users/you/Projects/my-app" if IS_MAC \
        else "/home/you/projects/my-app"
