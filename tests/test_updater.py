"""Updating the hub from Admin: real git repositories in a temp folder, a fake pip, no real restart."""

import asyncio
import subprocess
import sys

import pytest

from hub.models import HubError
from hub.updater import Updater


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repos(tmp_path):
    origin, work, clone = tmp_path / "origin.git", tmp_path / "work", tmp_path / "clone"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    git(tmp_path, "clone", "-q", str(origin), str(work))
    for repo in (work,):
        git(repo, "config", "user.email", "t@example.com")
        git(repo, "config", "user.name", "T")
    (work / "requirements.txt").write_text("fastapi\n")
    (work / "app.txt").write_text("v1\n")
    git(work, "add", ".")
    git(work, "commit", "-qm", "first")
    git(work, "push", "-q", "origin", "main")
    git(work, "checkout", "-qb", "feature")
    (work / "app.txt").write_text("feature\n")
    git(work, "commit", "-qam", "feature work")
    git(work, "push", "-q", "origin", "feature")
    git(work, "checkout", "-q", "main")
    git(tmp_path, "clone", "-q", str(origin), str(clone))
    git(clone, "config", "user.email", "t@example.com")
    git(clone, "config", "user.name", "T")
    return work, clone


def push_change(work, msg, requirements=False):
    (work / "app.txt").write_text(msg + "\n")
    if requirements:
        (work / "requirements.txt").write_text("fastapi\nnewpkg\n")
    git(work, "commit", "-qam", msg)
    git(work, "push", "-q", "origin", "main")


class FakePython(Updater):
    """pip is replaced by a harmless command; restarts are recorded, not done."""

    def __init__(self, repo):
        super().__init__(repo=repo, python=sys.executable)
        self.pip_calls, self.restarts = [], 0

    async def _run(self, *args, **kw):
        if args[:3] == (sys.executable, "-m", "pip"):
            self.pip_calls.append(args)
            return ""
        return await super()._run(*args, **kw)

    def restart_now(self):
        self.restarts += 1


def run_update(u, branch, restart=True):
    async def go():
        u.start(branch, restart)
        await u._task
        await asyncio.sleep(1.1)      # let a scheduled restart fire
        return u.job
    return asyncio.run(go())


def test_status_shows_branch_incoming_changes_and_branches(repos):
    work, clone = repos
    push_change(work, "second")
    st = asyncio.run(FakePython(clone).status())
    assert st["branch"] == "main" and st["behind"] == 1 and st["ahead"] == 0 and st["incoming"][0].endswith("second")
    assert {b["name"] for b in st["branches"]} == {"main", "feature"} and st["dirty"] == []


def test_pull_installs_new_requirements_and_restarts(repos):
    work, clone = repos
    push_change(work, "needs a package", requirements=True)
    u = FakePython(clone)
    job = run_update(u, "main")
    assert job["state"] == "restarting" and job["changed"] and u.restarts == 1
    assert (clone / "app.txt").read_text() == "needs a package\n"
    assert len(u.pip_calls) == 1 and "requirements.txt" in u.pip_calls[0]
    assert any("Now at" in line for line in job["log"])


def test_nothing_new_means_no_restart(repos):
    _, clone = repos
    u = FakePython(clone)
    job = run_update(u, "main")
    assert job["state"] == "done" and not job["changed"] and u.restarts == 0 and u.pip_calls == []
    assert "Already up to date." in job["log"]


def test_switching_branch(repos):
    _, clone = repos
    u = FakePython(clone)
    job = run_update(u, "feature", restart=False)
    assert job["state"] == "done" and git(clone, "rev-parse", "--abbrev-ref", "HEAD") == "feature"
    assert (clone / "app.txt").read_text() == "feature\n" and u.restarts == 0
    assert any("Switched from main to feature" in line for line in job["log"])


def test_local_edits_stop_the_update(repos):
    work, clone = repos
    push_change(work, "second")
    (clone / "app.txt").write_text("edited on the mac\n")
    u = FakePython(clone)
    job = run_update(u, "main")
    assert job["state"] == "failed" and "changes of its own" in job["error"] and u.restarts == 0
    assert (clone / "app.txt").read_text() == "edited on the mac\n"      # nothing was discarded


def test_diverged_branch_is_not_merged(repos):
    work, clone = repos
    push_change(work, "on github")
    (clone / "local.txt").write_text("x\n")
    git(clone, "add", "local.txt")
    git(clone, "commit", "-qm", "local only")
    job = run_update(FakePython(clone), "main")
    assert job["state"] == "failed" and "commits that aren't on GitHub" in job["error"]
    assert git(clone, "log", "-1", "--format=%s") == "local only"


def test_bad_branch_names_and_unknown_branches(repos):
    _, clone = repos
    u = FakePython(clone)
    for bad in ("--upload-pack=x", "a..b", "x y"):
        with pytest.raises(HubError):
            u.start(bad, True)
    job = run_update(u, "nope")
    assert job["state"] == "failed" and "no branch nope" in job["error"]


def test_routes_need_a_signed_in_person(tmp_path, monkeypatch):
    monkeypatch.setenv("SMARTHOME_HOME", str(tmp_path))
    from fastapi.testclient import TestClient
    from hub import app as app_module
    with TestClient(app_module.app) as client:
        hub = app_module.app.state.hub
        token = {"Authorization": f"Bearer {hub.secrets.access_token()}"}
        restarts = []
        monkeypatch.setattr(hub.updater, "restart_now", lambda: restarts.append(1))
        assert client.get("/api/admin/update", headers=token).status_code == 403
        assert client.post("/api/admin/update", json={"branch": "main"}, headers=token).status_code == 403
        assert client.post("/api/admin/restart", headers=token).status_code == 403
        assert restarts == []


def test_log_tail_follow_and_masking(tmp_path, monkeypatch):
    monkeypatch.setenv("SMARTHOME_HOME", str(tmp_path))
    from fastapi.testclient import TestClient
    from hub import app as app_module
    with TestClient(app_module.app) as client:
        hub = app_module.app.state.hub
        token = {"Authorization": f"Bearer {hub.secrets.access_token()}"}
        assert client.get("/api/admin/logfile", headers=token).status_code == 403      # scripts can't read it
        hub.secrets.set("auth", {**hub.auth.config, "allowed_emails": ["me@example.com"]})
        client.cookies.set("hub_session", hub.auth.create_session("me@example.com", "google"))
        log = tmp_path / "logs" / "hub.log"
        log.parent.mkdir(exist_ok=True)
        log.write_text("".join(f"line {i}\n" for i in range(1500)) + "login password=hunter2 ok\nAuthorization: Bearer abc.def\n")
        r = client.get("/api/admin/logfile?lines=1000").json()
        lines = r["text"].splitlines()
        assert len(lines) == 1000 and lines[0] == "line 502" and r["reset"]
        assert "hunter2" not in r["text"] and "abc.def" not in r["text"] and "password=•••" in r["text"]
        with log.open("a") as f:
            f.write("new 1\nnew 2\n")
        more = client.get(f"/api/admin/logfile?after={r['end']}").json()
        assert more["text"] == "new 1\nnew 2\n" and not more["reset"] and more["end"] > r["end"]
        log.write_text("rotated\n")                                   # the file got smaller: start over
        again = client.get(f"/api/admin/logfile?after={more['end']}").json()
        assert again["reset"] and again["text"] == "rotated\n"


def test_energy_watts_route(tmp_path, monkeypatch):
    monkeypatch.setenv("SMARTHOME_HOME", str(tmp_path))
    from fastapi.testclient import TestClient
    from hub import app as app_module
    with TestClient(app_module.app) as client:
        hub = app_module.app.state.hub
        token = {"Authorization": f"Bearer {hub.secrets.access_token()}"}
        hub.last_devices = {}
        assert client.put("/api/energy/watts/leviton:chand", json={"watts": 960}, headers=token).json() == {"ok": True}
        assert client.get("/api/energy/watts", headers=token).json()["watts"] == {"leviton:chand": 960}
        assert client.put("/api/energy/watts/leviton:chand", json={"watts": 50000}, headers=token).status_code == 422
        client.put("/api/energy/watts/leviton:chand", json={"watts": None}, headers=token)
        assert client.get("/api/energy/watts", headers=token).json()["watts"] == {}
