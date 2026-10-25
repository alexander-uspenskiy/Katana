"""Katana authentication: how the agents sign in to Claude, and the API keys kept in the macOS keychain.

Agents sign in with the Claude subscription (the default) or an Anthropic API key. The choice is shared by
every project and lives in ~/.katana/auth.json. API keys never touch a file: they are generic
passwords in the login keychain under the service "katana", one per provider. The `security` command
gets each secret on stdin, so it never shows in the process list, and agents may not run `security`.
"""
from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import projects

REPO = Path(__file__).resolve().parent.parent
AUTH_PATH = projects.USER_DIR / "auth.json"
KEYCHAIN_SERVICE = "katana"
METHODS = ("subscription", "api_key")

# Providers whose keys Katana stores. Only the Anthropic key is used today, and only in API key mode.
PROVIDERS = {
    "anthropic": {"title": "Anthropic (Claude API)", "env": "ANTHROPIC_API_KEY", "prefix": "sk-ant-",
                  "console": "https://console.anthropic.com/settings/keys"},
    "openai": {"title": "OpenAI", "env": "OPENAI_API_KEY", "prefix": "sk-",
               "console": "https://platform.openai.com/api-keys"},
    "gemini": {"title": "Google Gemini", "env": "GEMINI_API_KEY", "prefix": "",
               "console": "https://aistudio.google.com/apikey"},
}
KEY_PATTERN = re.compile(r"^[A-Za-z0-9_\-.:+/=]{16,512}$")

# Any of these would make the CLI bill an API account instead of the subscription.
API_ENV_VARS = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_SIMPLE",
)
_status_cache = {"at": 0.0, "value": None}


# ---------------------------------------------------------------- claude CLI

def find_claude() -> str:
    candidates = [os.environ.get("KATANA_CLAUDE_BIN"), shutil.which("claude"),
                  str(Path.home() / ".local/bin/claude")]
    # Fallback: the CLI bundled with the Claude desktop app, newest version first.
    bundled = glob.glob(str(Path.home() / "Library/Application Support/Claude/claude-code/*/*"
                                          "/claude.app/Contents/MacOS/claude"))
    version = lambda p: [int(x) for x in re.findall(r"\d+", Path(p).parents[4].name)]
    candidates += sorted(bundled, key=version, reverse=True)
    for c in candidates:
        if c and os.access(c, os.X_OK):
            return c
    raise SystemExit("Claude Code CLI not found. Install it or set KATANA_CLAUDE_BIN.")


def subscription_env() -> dict:
    """This process's environment without any API or provider variables. Gate commands get this."""
    return {k: v for k, v in os.environ.items() if k not in API_ENV_VARS}


def agent_env() -> dict:
    """The environment for agent CLI calls: the subscription env, plus the Anthropic key in API key mode."""
    env = subscription_env()
    if method() == "api_key":
        key = get_key("anthropic")
        if key:
            env["ANTHROPIC_API_KEY"] = key
    return env


def claude_login(claude: str) -> dict:
    """`claude auth status` for the subscription login, without API variables. Empty dict if unreadable."""
    try:
        out = subprocess.run([claude, "auth", "status", "--json"], capture_output=True, text=True,
                             env=subscription_env(), timeout=60)
        return json.loads(out.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return {}


def check(claude: str) -> dict:
    """Before a scan: makes sure the agents can sign in. Returns what the heartbeat records."""
    if method() == "api_key":
        key = get_key("anthropic")
        if not key:
            raise SystemExit("Agents are set to use an Anthropic API key, but none is saved. Add one in "
                             "Settings → Authentication, or switch back to the subscription.")
        return {"mode": "api_key", "method": "api_key", "subscription": None, "key": hint(key)}
    status = claude_login(claude)
    if not status:
        raise SystemExit("Could not read auth status from the Claude CLI.")
    if not status.get("loggedIn") or not status.get("subscriptionType"):
        raise SystemExit("Not logged in with a Claude subscription. Run `claude auth login` "
                         "(or `claude setup-token` and set CLAUDE_CODE_OAUTH_TOKEN), or add an Anthropic "
                         "API key in Settings → Authentication.")
    return {"mode": "subscription", "subscription": status.get("subscriptionType"),
            "method": status.get("authMethod")}


# ---------------------------------------------------------------- sign-in method

def method() -> str:
    try:
        value = json.loads(AUTH_PATH.read_text()).get("claude")
    except (OSError, json.JSONDecodeError, AttributeError):
        value = None
    return value if value in METHODS else "subscription"


def set_method(value: str) -> None:
    AUTH_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = AUTH_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps({"claude": value}, indent=2) + "\n")
    tmp.replace(AUTH_PATH)


# ---------------------------------------------------------------- keychain

def get_key(provider: str) -> str | None:
    try:
        out = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", provider, "-w"],
                             capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return (out.stdout.strip() or None) if out.returncode == 0 else None


def set_key(provider: str, key: str) -> str | None:
    """Saves or replaces a provider's key. Returns an error message, or None."""
    if provider not in PROVIDERS:
        return "Unknown provider."
    key = key.strip()
    if not KEY_PATTERN.match(key):
        return "That doesn't look like an API key: paste the whole key, with no spaces."
    # security -i reads the command from stdin; KEY_PATTERN keeps quotes and spaces out of it.
    title = PROVIDERS[provider]["title"]
    line = f'add-generic-password -U -s "{KEYCHAIN_SERVICE}" -a "{provider}" -l "Katana: {title}" -w "{key}"\n'
    try:
        out = subprocess.run(["security", "-i"], input=line, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        return f"Couldn't reach the keychain: {e}"
    _status_cache["at"] = 0
    if out.returncode != 0 or get_key(provider) != key:
        return (out.stderr.strip() or "The keychain didn't save the key.")[:300]
    return None


def delete_key(provider: str) -> None:
    subprocess.run(["security", "delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", provider],
                   capture_output=True, timeout=30)
    _status_cache["at"] = 0


def hint(key: str | None) -> str | None:
    return "…" + key[-4:] if key else None


# ---------------------------------------------------------------- status for the dashboard

def status(fresh: bool = False) -> dict:
    """The sign-in method, the subscription login, each provider's key (only its last 4 characters) and
    whether the agents can sign in. Cached for 30 seconds."""
    if not fresh and _status_cache["value"] and time.time() - _status_cache["at"] < 30:
        return _status_cache["value"]
    try:
        cli, cli_error = find_claude(), None
    except SystemExit as e:
        cli, cli_error = None, str(e)
    login = claude_login(cli) if cli else {}
    subscribed = bool(login.get("loggedIn") and login.get("subscriptionType"))
    providers = []
    for pid, p in PROVIDERS.items():
        key = get_key(pid)
        providers.append(dict(p, id=pid, saved=bool(key), hint=hint(key), env_set=bool(os.environ.get(p["env"]))))
    mode = method()
    has_api_key = providers[0]["saved"]
    if not cli:
        ready, message = False, cli_error
    elif mode == "api_key":
        ready = has_api_key
        message = "Agents use your Anthropic API key, billed per token." if ready \
            else "API key needed: agents are set to use an Anthropic API key, but none is saved."
    else:
        ready = subscribed
        message = f"Agents use your Claude {login['subscriptionType'].capitalize()} subscription." if ready \
            else "Not signed in to a Claude subscription. Run claude auth login, or add an Anthropic API key " \
                 "and switch agents to it."
    value = {"method": mode, "methods": list(METHODS), "ready": ready, "message": message, "cli": cli,
             "claude": {"logged_in": bool(login.get("loggedIn")), "subscription": login.get("subscriptionType"),
                        "auth_method": login.get("authMethod"), "email": login.get("email"),
                        "org": login.get("orgName")},
             "providers": providers, "keychain_service": KEYCHAIN_SERVICE,
             "env_vars": [v for v in API_ENV_VARS if os.environ.get(v)]}
    _status_cache.update(at=time.time(), value=value)
    return value
