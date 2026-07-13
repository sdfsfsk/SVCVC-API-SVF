from __future__ import annotations

import sys
import types

from bilibili_source import (
    download_bilibili_audio,
    is_bilibili_source,
    resolve_bilibili_video_url,
    search_bilibili_videos,
)


def test_recognizes_only_explicit_bilibili_conversion_sources():
    assert is_bilibili_source("BV1xx411c7mD") is True
    assert is_bilibili_source("https://www.bilibili.com/video/BV1xx411c7mD") is True
    assert is_bilibili_source("https://b23.tv/abc123") is True
    assert is_bilibili_source("七里香 周杰伦") is False


def test_resolves_bvid_and_rejects_plain_keyword():
    assert resolve_bilibili_video_url("请播放 BV1xx411c7mD") == "https://www.bilibili.com/video/BV1xx411c7mD"
    assert resolve_bilibili_video_url("https://b23.tv/abc123") == "https://b23.tv/abc123"

    try:
        resolve_bilibili_video_url("七里香 周杰伦")
    except ValueError as exc:
        assert "BV" in str(exc)
    else:
        raise AssertionError("plain keyword must not become an implicit Bilibili search")


def test_search_prefers_music_partition_and_normalizes_cover():
    calls: list[tuple[str, object]] = []

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class Session:
        def get(self, url, **kwargs):
            calls.append((url, kwargs.get("params")))
            if url.endswith("/nav"):
                return Response(
                    {
                        "data": {
                            "wbi_img": {
                                "img_url": "https://i0.hdslb.com/0123456789abcdef0123456789abcdef.png",
                                "sub_url": "https://i0.hdslb.com/fedcba9876543210fedcba9876543210.png",
                            }
                        }
                    }
                )
            if url.endswith("/"):
                return Response({})
            return Response(
                {
                    "code": 0,
                    "data": {
                        "result": [
                            {
                                "bvid": "BV1xx411c7mD",
                                "title": "<em>七里香</em>",
                                "author": "周杰伦",
                                "pic": "//i0.hdslb.com/cover.jpg",
                            }
                        ]
                    },
                }
            )

    results = search_bilibili_videos("七里香", session=Session())

    assert calls[2][1]["tids"] == "3"
    assert results == [
        {
            "title": "七里香",
            "artist": "周杰伦",
            "cover": "https://i0.hdslb.com/cover.jpg",
            "url": "https://www.bilibili.com/video/BV1xx411c7mD",
        }
    ]


def test_download_passes_max_file_size_to_ytdlp(monkeypatch, tmp_path):
    destination = tmp_path / "audio.wav"
    captured: dict[str, object] = {}

    class Downloader:
        def __init__(self, options):
            captured.update(options)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def download(self, _urls):
            destination.write_bytes(b"RIFFxxxxWAVE")

    monkeypatch.setitem(sys.modules, "yt_dlp", types.SimpleNamespace(YoutubeDL=Downloader))

    result = download_bilibili_audio(
        "https://www.bilibili.com/video/BV1xx411c7mD",
        destination,
        tmp_path,
        max_bytes=12_345,
    )

    assert result == destination
    assert captured["max_filesize"] == 12_345
