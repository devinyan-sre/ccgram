"""doctor's secrets-file permission check.

.env holds the bot token and LLM/Whisper/TTS API keys. A group/other-readable
.env exposes them to every account on the host.
"""

import stat

import pytest

from ccgram import doctor_cmd


@pytest.fixture
def _config_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(doctor_cmd, "ccgram_dir", lambda: tmp_path)
    # Neutralise the cwd-relative ".env" the check also looks at, so tests are
    # isolated from the repo's own working directory.
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_passes_when_no_secrets_file_exists(_config_dir):
    status, _ = doctor_cmd._check_secret_permissions()
    assert status == doctor_cmd._PASS


def test_passes_for_owner_only_env(_config_dir):
    env = _config_dir / ".env"
    env.write_text("TELEGRAM_BOT_TOKEN=x")
    env.chmod(0o600)
    status, _ = doctor_cmd._check_secret_permissions()
    assert status == doctor_cmd._PASS


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o666])
def test_warns_for_group_or_other_readable_env(_config_dir, mode):
    env = _config_dir / ".env"
    env.write_text("TELEGRAM_BOT_TOKEN=x")
    env.chmod(mode)
    status, msg = doctor_cmd._check_secret_permissions()
    assert status == doctor_cmd._WARN
    assert ".env" in msg


def test_fix_tightens_to_owner_only(_config_dir):
    env = _config_dir / ".env"
    env.write_text("TELEGRAM_BOT_TOKEN=x")
    env.chmod(0o644)

    doctor_cmd._fix_secret_permissions()

    mode = env.stat().st_mode
    assert not mode & (stat.S_IRWXG | stat.S_IRWXO)
    assert doctor_cmd._check_secret_permissions()[0] == doctor_cmd._PASS


def test_check_never_prints_secret_contents(_config_dir, capsys):
    env = _config_dir / ".env"
    env.write_text("TELEGRAM_BOT_TOKEN=super-secret-value")
    env.chmod(0o644)
    _status, msg = doctor_cmd._check_secret_permissions()
    assert "super-secret-value" not in msg
