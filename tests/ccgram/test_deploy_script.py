"""Behavioural tests for scripts/deploy.sh.

The deploy script's whole value is the failure path — health-gate then roll
back — which is exactly what never gets exercised by a manual happy-path run.
These drive it against stubbed systemctl/uv/curl so the gate and the rollback
are actually verified.

Skips when bash is unavailable (non-Linux CI).
"""

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_DEPLOY = _REPO / "scripts" / "deploy.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("git") is None,
    reason="requires bash + git",
)


def _write(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _make_stub_bin(tmp_path: Path, *, active: str, healthz: str) -> Path:
    """Build a directory of fake CLIs on PATH.

    ``active`` / ``healthz`` are the outcomes systemctl is-active and curl
    /healthz should report, sequenced across calls via a counter file.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    state = tmp_path / "state"
    state.mkdir()

    _write(
        bindir / "systemctl",
        f"""#!/usr/bin/env bash
case "$*" in
  *is-active*) echo "{active}" ;;
  *NRestarts*) echo 0 ;;
  *restart*) : ;;
  *status*) echo "stub status" ;;
  *) : ;;
esac
exit 0
""",
    )
    _write(
        bindir / "uv",
        """#!/usr/bin/env bash
# record each install target so the test can assert what was installed
echo "$*" >>"$UV_CALLS"
exit 0
""",
    )
    _write(
        bindir / "curl",
        f"""#!/usr/bin/env bash
exit {0 if healthz == "ok" else 22}
""",
    )
    return bindir


def _run(tmp_path: Path, bindir: Path, *, extra_args=()):
    # A real git repo with two commits, so HEAD and HEAD~1 both resolve for
    # the rollback worktree.
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "HOME": str(tmp_path / "home"),
        "UV_CALLS": str(tmp_path / "uv_calls.txt"),
    }
    (tmp_path / "home").mkdir(exist_ok=True)
    scripts = repo / "scripts"
    scripts.mkdir()
    shutil.copy(_DEPLOY, scripts / "deploy.sh")

    def git(*args):
        subprocess.run(
            ["git", *args], cwd=repo, env=env, check=True, capture_output=True
        )

    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (repo / "marker").write_text("v1")
    git("add", "-A")
    git("commit", "-qm", "v1")
    (repo / "marker").write_text("v2")
    git("add", "-A")
    git("commit", "-qm", "v2")

    return subprocess.run(
        ["bash", str(scripts / "deploy.sh"), "--timeout", "2", *extra_args],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
    )


def test_healthy_deploy_succeeds(tmp_path):
    bindir = _make_stub_bin(tmp_path, active="active", healthz="ok")
    result = _run(tmp_path, bindir)
    assert result.returncode == 0, result.stderr
    assert "deploy" in result.stdout.lower()
    assert "complete" in result.stdout.lower()


def test_unhealthy_deploy_triggers_rollback(tmp_path):
    bindir = _make_stub_bin(tmp_path, active="failed", healthz="fail")
    result = _run(tmp_path, bindir)
    # Deploy failed → non-zero, but rollback ran.
    assert result.returncode != 0
    assert "rolling back" in result.stdout.lower()
    # Two installs: the new commit, then the rollback build.
    calls = (tmp_path / "uv_calls.txt").read_text().strip().splitlines()
    assert len(calls) == 2, calls


def test_no_rollback_flag_leaves_new_version(tmp_path):
    bindir = _make_stub_bin(tmp_path, active="failed", healthz="fail")
    result = _run(tmp_path, bindir, extra_args=["--no-rollback"])
    assert result.returncode != 0
    assert "no-rollback" in (result.stdout + result.stderr).lower()
    calls = (tmp_path / "uv_calls.txt").read_text().strip().splitlines()
    assert len(calls) == 1, "must not reinstall when rollback is disabled"


def test_bash_syntax_is_valid():
    result = subprocess.run(["bash", "-n", str(_DEPLOY)], capture_output=True)
    assert result.returncode == 0, result.stderr
