"""HTTP client helpers for end-to-end tests against the dev stack (dev/docker-compose.yml)."""

import os
import subprocess
import time
from pathlib import Path

import requests

BASE = os.environ.get("FIGHTLOG_E2E_BASE", "http://localhost:8000")
DEV_DIR = Path(__file__).resolve().parent.parent / "dev"


def session_key(username: str) -> str:
    out = subprocess.run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "-e",
            f"DEV_SESSION_USER={username}",
            "auth",
            "python",
            "manage.py",
            "shell",
            "-c",
            "exec(open('/home/allianceauth/session.py').read())",
        ],
        cwd=DEV_DIR,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    for line in out.splitlines():
        if line.startswith("SESSION "):
            return line.split()[1]
    raise RuntimeError(f"no session for {username}: {out[-500:]}")


def django_shell(code: str) -> str:
    """Run code in the auth container's Django shell; returns the last line printed."""
    out = subprocess.run(
        ["docker", "compose", "exec", "-T", "auth", "python", "manage.py", "shell", "--no-imports", "-c", code],
        cwd=DEV_DIR,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    lines = [line for line in out.splitlines() if line.strip()]
    return lines[-1] if lines else ""


class Client:
    def __init__(self, username: str):
        self.username = username
        self.http = requests.Session()
        self.http.cookies.set("sessionid", session_key(username))
        self.http.get(f"{BASE}/fightlog/upload/")
        self.csrf = self.http.cookies.get("csrftoken", "")
        self.http.headers.update({"X-CSRFToken": self.csrf, "Referer": BASE + "/fightlog/"})

    def get(self, path, **kwargs):
        return self.http.get(BASE + path, allow_redirects=False, **kwargs)

    def post(self, path, data=None, files=None):
        return self.http.post(
            BASE + path, data={**(data or {}), "csrfmiddlewaretoken": self.csrf}, files=files, allow_redirects=False
        )

    def upload(self, filename: str, data: bytes) -> dict:
        response = self.http.post(BASE + "/fightlog/api/logs/", files={"file": (filename, data, "text/plain")})
        return response.json()

    def wait_log(self, log_id: int, timeout: float = 180.0, built: bool = True) -> dict:
        deadline = time.time() + timeout
        status = {}
        while time.time() < deadline:
            status = self.get(f"/fightlog/api/logs/{log_id}/").json()
            if status["status"] in ("no_pvp", "failed", "rejected"):
                return status
            if status["status"] == "parsed" and (
                not built or (status["fights"] and all(f["built"] for f in status["fights"]))
            ):
                return status
            time.sleep(1.5)
        raise TimeoutError(f"log {log_id} still {status}")

    def wait_fight(self, fight_id: int, timeout: float = 180.0) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            status = self.get(f"/fightlog/api/fight/{fight_id}/status/").json()
            if status["built"]:
                return status
            time.sleep(1.5)
        raise TimeoutError(f"fight {fight_id} not built")

    def analysis(self, fight_id: int, **params) -> dict:
        return self.get(f"/fightlog/api/fight/{fight_id}/analysis/", params=params).json()
