"""Tests for the i18n passthrough/translation layer."""

from ccgram import i18n
from ccgram.i18n import _reset_language_for_testing, t


class TestPassthrough:
    def test_default_language_is_passthrough(self, monkeypatch) -> None:
        monkeypatch.delenv("CCGRAM_LANG", raising=False)
        _reset_language_for_testing()
        assert t("❌ Use this command inside a topic.") == (
            "❌ Use this command inside a topic."
        )
        _reset_language_for_testing()

    def test_unknown_string_passes_through_in_zh(self, monkeypatch) -> None:
        monkeypatch.setenv("CCGRAM_LANG", "zh")
        _reset_language_for_testing()
        assert t("no such catalog entry xyz") == "no such catalog entry xyz"
        _reset_language_for_testing()

    def test_zh_translates_known_string(self, monkeypatch) -> None:
        monkeypatch.setenv("CCGRAM_LANG", "zh")
        _reset_language_for_testing()
        key = next(iter(i18n._ZH))
        assert t(key) == i18n._ZH[key]
        _reset_language_for_testing()


class TestCatalogHygiene:
    def test_placeholders_preserved_in_translations(self) -> None:
        """Every {placeholder} in an English key must appear in its translation."""
        import re

        for key, value in i18n._ZH.items():
            for ph in re.findall(r"\{[a-z_]+\}", key):
                assert ph in value, f"placeholder {ph} missing in translation of {key!r}"
