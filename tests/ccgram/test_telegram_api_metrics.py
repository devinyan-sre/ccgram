"""Telegram API metric labelling.

Label values end up in /metrics, which operators scrape and often ship
onwards. The bot token appears in every Bot API URL, so the method-name
extraction is security-relevant, not just cosmetic.
"""

from ccgram.telegram_request import _api_method_name


def test_extracts_method_from_a_bot_api_url():
    url = "https://api.telegram.org/bot123456:ABCDEF/getUpdates"
    assert _api_method_name((url,), {}) == "getUpdates"


def test_never_leaks_the_bot_token_into_the_label():
    token = "123456:ABC-DEF_secret"
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    label = _api_method_name((url,), {})
    assert label == "sendMessage"
    assert token not in label
    assert "123456" not in label


def test_prefers_the_url_keyword_argument():
    url = "https://api.telegram.org/bot1:x/answerCallbackQuery"
    assert _api_method_name((), {"url": url}) == "answerCallbackQuery"


def test_missing_url_degrades_to_unknown():
    assert _api_method_name((), {}) == "unknown"


def test_non_string_url_degrades_to_unknown():
    assert _api_method_name((object(),), {}) == "unknown"


def test_trailing_slash_degrades_to_unknown_rather_than_empty_label():
    assert _api_method_name(("https://api.telegram.org/bot1:x/",), {}) == "unknown"
