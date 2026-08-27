"""Tests for file_handler helper functions."""

import re
import unicodedata
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccgram.handlers.file_handler import (
    _AlbumItem,
    _build_album_prompt,
    _generate_photo_filename,
    _upload_and_notify,
    _sanitize_caption,
    _sanitize_filename,
    _unique_dest,
    _validate_dest_path,
)
from ccgram.task_scheduler import TaskAdmission


class TestSanitizeFilename:
    @pytest.mark.parametrize(
        ("input_name", "expected"),
        [
            ("document.pdf", "document.pdf"),
            ("file-name_123.txt", "file-name_123.txt"),
            ("/etc/passwd", "passwd"),
            ("../../../etc/passwd", "passwd"),
            ("../../etc/passwd", "passwd"),
            ("hello world!.txt", "hello_world_.txt"),
            ("file@#$.txt", "file___.txt"),
            ("请求书.xlsx", "请求书.xlsx"),
            ("Отчёт за 2025.pdf", "Отчёт_за_2025.pdf"),
            ("ใบเสร็จ.pdf", "ใบเสร็จ.pdf"),
            ("report📊.pdf", "report_.pdf"),
            ("safe‮gnp.exe", "safe_gnp.exe"),
            ("..", "unnamed"),
            (".", "unnamed"),
            ("...", "unnamed"),
            ("", "unnamed"),
        ],
    )
    def test_sanitize(self, input_name: str, expected: str) -> None:
        assert _sanitize_filename(input_name) == expected

    def test_truncates_long_names_preserving_extension(self) -> None:
        long = "a" * 250 + ".pdf"
        result = _sanitize_filename(long)
        assert len(result) <= 200
        assert result.endswith(".pdf")

    def test_composes_and_truncates_unicode_by_bytes(self) -> None:
        assert _sanitize_filename(unicodedata.normalize("NFD", "Отчёт.pdf")) == (
            "Отчёт.pdf"
        )
        result = _sanitize_filename("界" * 200 + ".pdf")
        assert len(result.encode()) <= 200
        assert result.endswith(".pdf")


class TestUniqueDest:
    def test_returns_original_if_not_exists(self, tmp_path: Path) -> None:
        assert _unique_dest(tmp_path / "file.txt") == tmp_path / "file.txt"

    @pytest.mark.parametrize(
        ("existing_files", "expected_name"),
        [
            (["file.txt"], "file_1.txt"),
            (["file.txt", "file_1.txt", "file_2.txt"], "file_3.txt"),
            (["file"], "file_1"),
        ],
    )
    def test_increments_suffix(
        self, tmp_path: Path, existing_files: list[str], expected_name: str
    ) -> None:
        for name in existing_files:
            (tmp_path / name).write_text("x")
        assert _unique_dest(tmp_path / existing_files[0]) == tmp_path / expected_name

    def test_fallback_to_timestamp_after_100(self, tmp_path: Path) -> None:
        dest = tmp_path / "file.txt"
        for i in range(100):
            name = "file.txt" if i == 0 else f"file_{i}.txt"
            (tmp_path / name).write_text(str(i))
        result = _unique_dest(dest)
        assert result.name.startswith("file_") and result.name.endswith(".txt")
        assert result != dest

    def test_broken_symlink_treated_as_existing(self, tmp_path: Path) -> None:
        dest = tmp_path / "file.txt"
        dest.symlink_to(tmp_path / "nonexistent_target")
        assert _unique_dest(dest) == tmp_path / "file_1.txt"


class TestValidateDestPath:
    @pytest.mark.parametrize(
        ("rel_dest", "expected"),
        [
            ("file.txt", True),
            ("subdir/file.txt", True),
            ("../outside.txt", False),
        ],
    )
    def test_path_validation(
        self, tmp_path: Path, rel_dest: str, expected: bool
    ) -> None:
        upload = tmp_path / "upload"
        upload.mkdir()
        if "/" in rel_dest and not rel_dest.startswith(".."):
            (upload / Path(rel_dest).parent).mkdir(parents=True, exist_ok=True)
        assert _validate_dest_path(upload / rel_dest, upload) is expected

    def test_rejects_absolute_path_outside(self, tmp_path: Path) -> None:
        upload = tmp_path / "upload"
        upload.mkdir()
        assert _validate_dest_path(tmp_path / "outside.txt", upload) is False


class TestSanitizeCaption:
    @pytest.mark.parametrize(
        ("input_text", "expected"),
        [
            ("", ""),
            ("hello\x00\x01\x02world", "helloworld"),
            ("hello\x07\x1bworld", "helloworld"),
            ("line1\nline2\r\nline3\ttab", "line1 line2  line3\ttab"),
        ],
    )
    def test_sanitize(self, input_text: str, expected: str) -> None:
        assert _sanitize_caption(input_text) == expected

    def test_limits_to_500_chars(self) -> None:
        assert len(_sanitize_caption("a" * 600)) == 500


class TestGeneratePhotoFilename:
    def test_format(self) -> None:
        result = _generate_photo_filename("ABCDEFGHIJKLMNOP")
        assert re.match(r"^photo_\d{8}_\d{6}_ABCDEFGH\.jpg$", result)


def test_album_prompt_merges_paths_and_deduplicates_caption() -> None:
    message = MagicMock()
    items = [
        _AlbumItem(message, "@7", 42, 17, ".ccgram-uploads/a.jpg", "检查"),
        _AlbumItem(message, "@7", 42, 17, ".ccgram-uploads/b.jpg", "检查"),
    ]

    prompt = _build_album_prompt(items)

    assert "with 2 files" in prompt
    assert "a.jpg" in prompt and "b.jpg" in prompt
    assert prompt.count("检查") == 1


async def test_media_dispatch_uses_shared_scheduler_and_request_correlation(
    tmp_path: Path,
) -> None:
    message = MagicMock()
    message.chat.id = -1001
    message.chat.send_action = AsyncMock()
    message.message_id = 55

    with (
        patch(
            "ccgram.handlers.file_handler._resolve_upload_dir",
            return_value=("@7", tmp_path, None),
        ),
        patch(
            "ccgram.handlers.file_handler._download_and_save",
            new_callable=AsyncMock,
            return_value="image.jpg",
        ),
        patch(
            "ccgram.handlers.file_handler.admit_request",
            new_callable=AsyncMock,
            return_value=TaskAdmission(False, False, task_id="T0001"),
        ) as admit,
        patch(
            "ccgram.handlers.file_handler.send_to_window",
            new_callable=AsyncMock,
            return_value=(True, ""),
        ) as send,
        patch("ccgram.handlers.file_handler.inbound_store") as inbound,
        patch("ccgram.handlers.file_handler.ack_reaction", new_callable=AsyncMock),
        patch("ccgram.handlers.file_handler.safe_reply", new_callable=AsyncMock),
    ):
        inbound.make_key.return_value = "request-key"
        await _upload_and_notify(
            message,
            user_id=42,
            thread_id=17,
            filename="image.jpg",
            file_id="telegram-file",
            file_size=100,
            size_label="Photo",
            agent_msg_tpl="image at {path}",
            success_emoji="📷",
            caption="检查告警",
        )

    expected = "image at .ccgram-uploads/image.jpg\n\nUser note: 检查告警"
    admit.assert_awaited_once()
    assert admit.await_args.kwargs["dispatch_text"] == expected
    send.assert_awaited_once_with("@7", expected)
    assert inbound.set_state.call_args_list[-1].args == ("request-key", "forwarded")
