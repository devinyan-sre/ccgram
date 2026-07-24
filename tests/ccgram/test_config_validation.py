"""Config validation + a documentation-drift gate for CCGRAM_* env vars.

The env surface is large (40+ vars). Two failure modes this guards:

1. A bad value silently degrading to a default, so the operator sees wrong
   behaviour with nothing to grep for.
2. A new env var landing in code but never reaching the guides, so the
   configuration reference quietly stops being the source of truth.
"""

import re
from pathlib import Path

import pytest

from ccgram.config import Config, _looks_like_time_spec, config

_REPO = Path(__file__).resolve().parents[2]
_CONFIG_SRC = _REPO / "src" / "ccgram" / "config.py"
_GUIDES = (
    _REPO / "docs" / "guides.md",
    _REPO / "docs" / "en" / "guides.md",
)


@pytest.fixture
def env(monkeypatch):
    """Build a Config with a controlled environment."""

    def _build(**overrides: str) -> Config:
        for key, value in overrides.items():
            monkeypatch.setenv(key, value)
        return Config()

    return _build


# --- documentation drift gate --------------------------------------------


def _documented_vars(path: Path) -> set[str]:
    return set(re.findall(r"CCGRAM_[A-Z_]+", path.read_text(encoding="utf-8")))


def _configured_vars() -> set[str]:
    return set(
        re.findall(r'"(CCGRAM_[A-Z_]+)"', _CONFIG_SRC.read_text(encoding="utf-8"))
    )


@pytest.mark.parametrize("guide", _GUIDES, ids=lambda p: p.parent.name)
def test_every_config_env_var_is_documented(guide):
    missing = sorted(_configured_vars() - _documented_vars(guide))
    assert not missing, (
        f"{guide.relative_to(_REPO)} is missing: {', '.join(missing)}. "
        "Add a row to the configuration table when introducing an env var."
    )


def test_the_gate_actually_sees_variables():
    """Guard against the regex silently matching nothing and passing vacuously."""
    assert len(_configured_vars()) > 20


# --- fatal problems ------------------------------------------------------


def test_clean_environment_has_no_problems():
    fatal, _ = config.validate()
    assert fatal == []


def test_unknown_multiplexer_is_fatal(env):
    cfg = env(CCGRAM_MULTIPLEXER="screen")
    fatal, _ = cfg.validate()
    assert any("CCGRAM_MULTIPLEXER" in p for p in fatal)


def test_known_multiplexers_are_accepted(env):
    for name in ("tmux", "herdr"):
        fatal, _ = env(CCGRAM_MULTIPLEXER=name).validate()
        assert fatal == []


def test_out_of_range_port_is_fatal(env):
    cfg = env(CCGRAM_METRICS_PORT="70000")
    fatal, _ = cfg.validate()
    assert any("CCGRAM_METRICS_PORT" in p for p in fatal)


def test_port_zero_is_valid_because_it_means_disabled(env):
    fatal, _ = env(CCGRAM_METRICS_PORT="0").validate()
    assert fatal == []


# --- warnings (silently-corrected values) --------------------------------


def test_typo_in_status_mode_warns_instead_of_failing(env):
    cfg = env(CCGRAM_STATUS_MODE="usre")
    fatal, warnings = cfg.validate()
    assert fatal == [], "a cosmetic typo must not stop the bot from booting"
    assert any("CCGRAM_STATUS_MODE" in w for w in warnings)


def test_valid_status_mode_is_silent(env):
    _, warnings = env(CCGRAM_STATUS_MODE="user").validate()
    assert not any("CCGRAM_STATUS_MODE" in w for w in warnings)


def test_unknown_language_warns(env):
    _, warnings = env(CCGRAM_LANG="fr").validate()
    assert any("CCGRAM_LANG" in w for w in warnings)


def test_regional_chinese_variant_is_accepted(env):
    _, warnings = env(CCGRAM_LANG="zh-CN").validate()
    assert not any("CCGRAM_LANG" in w for w in warnings)


def test_malformed_quiet_hours_warns(env):
    _, warnings = env(CCGRAM_QUIET_HOURS="22-7").validate()
    assert any("CCGRAM_QUIET_HOURS" in w for w in warnings)


def test_valid_quiet_hours_is_silent(env):
    _, warnings = env(CCGRAM_QUIET_HOURS="22:00-07:30").validate()
    assert not any("CCGRAM_QUIET_HOURS" in w for w in warnings)


def test_malformed_digest_time_warns(env):
    _, warnings = env(CCGRAM_DAILY_DIGEST="9am").validate()
    assert any("CCGRAM_DAILY_DIGEST" in w for w in warnings)


# --- time-spec shape check ------------------------------------------------


@pytest.mark.parametrize("value", ["00:00", "23:59", "09:05"])
def test_accepts_valid_hhmm(value):
    assert _looks_like_time_spec(value, "HH:MM") is True


@pytest.mark.parametrize("value", ["24:00", "12:60", "9:5:0", "noon", "", "12"])
def test_rejects_invalid_hhmm(value):
    assert _looks_like_time_spec(value, "HH:MM") is False


def test_accepts_valid_range():
    assert _looks_like_time_spec("22:00-07:00", "HH:MM-HH:MM") is True


def test_rejects_range_missing_a_side():
    assert _looks_like_time_spec("22:00", "HH:MM-HH:MM") is False
