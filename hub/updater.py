"""Update the hub from Admin: see which branch and commit it runs, fetch, switch branch, pull, install new
requirements, and restart.

Only fast-forward pulls: if the Mac's copy has commits or edits of its own, the update stops and says so
instead of merging or discarding anything. Restarting relies on the launchd service (scripts/install-service.sh),
which starts the hub again whenever it stops; without it, the hub replaces its own process.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import signal
import sys
import time
from pathlib import Path

from .models import HubError

log = logging.getLogger(__name__)

REPO = Path(__file__).resolve().parent.parent
SERVICE_LABEL = "local.smarthome.hub"
BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,120}$")
GIT_TIMEOUT = 90
PIP_TIMEOUT = 900


class Updater:
    def __init__(self, repo: Path = REPO, python: str = sys.executable) -> None:
        self.repo = repo
        self.python = python
        self.job: dict | None = None      # the running or last update: {"state", "log", "started", ...}
        self._task: asyncio.Task | None = None

    async def _run(self, *args: str, timeout: float = GIT_TIMEOUT, check: bool = True) -> str:
        proc = await asyncio.create_subprocess_exec(
            *args, cwd=self.repo, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})   # never wait for a password prompt
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError as exc:
            proc.kill()
            raise HubError(504, f"{' '.join(args[:2])} took too long.") from exc
        text = out.decode(errors="replace").strip()
        if check and proc.returncode != 0:
            raise HubError(502, f"{' '.join(args[:3])} failed: {text[-400:]}")
        return text

    async def git(self, *args: str, check: bool = True) -> str:
        return await self._run("git", *args, check=check)

    async def status(self, fetch: bool = True) -> dict:
        if not (self.repo / ".git").exists():
            raise HubError(409, "The hub isn't running from a git checkout, so it can't update itself.")
        fetch_error = None
        if fetch:
            try:
                await self.git("fetch", "--prune", "origin")
            except HubError as exc:
                fetch_error = exc.message
        branch = await self.git("rev-parse", "--abbrev-ref", "HEAD")
        head = await self.git("log", "-1", "--format=%h\x1f%s\x1f%cr")
        short, subject, when = (head.split("\x1f") + ["", "", ""])[:3]
        dirty = [line[3:] for line in (await self.git("status", "--porcelain", "--untracked-files=no")).splitlines() if line.strip()]
        refs = await self.git("for-each-ref", "--sort=-committerdate", "--format=%(refname:short)\x1f%(committerdate:relative)\x1f%(subject)",
                              "refs/remotes/origin")
        branches = []
        for line in refs.splitlines():
            name, rel, subj = (line.split("\x1f") + ["", "", ""])[:3]
            name = name.removeprefix("origin/")
            if name in ("HEAD", "origin") or not BRANCH_RE.match(name):
                continue
            branches.append({"name": name, "updated": rel, "subject": subj[:100]})
        behind = ahead = 0
        incoming: list[str] = []
        if any(b["name"] == branch for b in branches):
            counts = await self.git("rev-list", "--left-right", "--count", f"HEAD...origin/{branch}")
            ahead, behind = (int(x) for x in counts.split())
            if behind:
                incoming = (await self.git("log", "--format=%h %s", "-n", "15", f"HEAD..origin/{branch}")).splitlines()
        return {"branch": branch, "commit": short, "subject": subject, "when": when, "dirty": dirty,
                "ahead": ahead, "behind": behind, "incoming": incoming, "branches": branches[:40],
                "fetch_error": fetch_error, "service": self.under_launchd(), "job": self.job}

    def start(self, branch: str, restart: bool) -> dict:
        if not BRANCH_RE.match(branch) or branch.startswith("-") or ".." in branch:
            raise HubError(400, "That isn't a branch name.")
        if self._task and not self._task.done():
            raise HubError(409, "An update is already running.")
        self.job = {"state": "running", "branch": branch, "log": [], "started": time.time(), "restart": restart}
        self._task = asyncio.create_task(self._update(branch, restart))
        return self.job

    def _say(self, line: str) -> None:
        self.job["log"].append(line)
        log.info("update: %s", line)

    async def _update(self, branch: str, restart: bool) -> None:
        job = self.job
        try:
            await self.git("fetch", "--prune", "origin")
            remote = f"origin/{branch}"
            if not await self.git("rev-parse", "--verify", "--quiet", remote, check=False):
                raise HubError(404, f"There's no branch {branch} on GitHub.")
            dirty = (await self.git("status", "--porcelain", "--untracked-files=no")).strip()
            if dirty:
                raise HubError(409, "The Mac's copy has changes of its own, so nothing was pulled. Commit or discard them first:\n" + dirty[:400])
            before = await self.git("rev-parse", "HEAD")
            current = await self.git("rev-parse", "--abbrev-ref", "HEAD")
            if current != branch:
                exists = await self.git("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False)
                if exists:
                    await self.git("checkout", branch)
                else:
                    await self.git("checkout", "-b", branch, "--track", remote)
                self._say(f"Switched from {current} to {branch}.")
            try:
                out = await self.git("merge", "--ff-only", remote)
            except HubError as exc:
                raise HubError(409, f"{branch} on this Mac has commits that aren't on GitHub, so it can't simply be "
                                    "updated. Nothing was changed beyond switching branch.") from exc
            after = await self.git("rev-parse", "HEAD")
            if before == after:
                self._say("Already up to date.")
            else:
                self._say(out.splitlines()[0] if out else "Pulled.")
                self._say(await self.git("log", "-1", "--format=Now at %h: %s"))
                changed = (await self.git("diff", "--name-only", before, after)).splitlines()
                if "requirements.txt" in changed:
                    self._say("requirements.txt changed: installing (this can take a few minutes)…")
                    out = await self._run(self.python, "-m", "pip", "install", "-q", "-r", "requirements.txt", timeout=PIP_TIMEOUT)
                    self._say("Packages installed." + (f"\n{out[-600:]}" if out else ""))
            job["state"] = "done"
            job["changed"] = before != after
            if restart and before != after:
                self._say("Restarting the hub…")
                job["state"] = "restarting"
                asyncio.get_running_loop().call_later(1.0, self.restart_now)
        except HubError as exc:
            job["state"], job["error"] = "failed", exc.message
            self._say(exc.message)
        except Exception as exc:  # noqa: BLE001
            job["state"], job["error"] = "failed", str(exc)
            self._say(f"{type(exc).__name__}: {exc}")
        finally:
            job["finished"] = time.time()

    @staticmethod
    def under_launchd() -> bool:
        return os.environ.get("XPC_SERVICE_NAME") == SERVICE_LABEL

    def restart_now(self) -> None:
        """Under launchd, stop cleanly and let launchd start the hub again (about 10 s). Otherwise replace this
        process with a fresh one."""
        if self.under_launchd():
            os.kill(os.getpid(), signal.SIGTERM)
        else:
            os.execv(self.python, [self.python, "-m", "hub"])
