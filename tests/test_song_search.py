from __future__ import annotations

import app


def test_search_routes_bilibili_to_music_search(monkeypatch):
    expected = [
        {
            "title": "七里香",
            "artist": "周杰伦",
            "cover": "",
            "url": "https://www.bilibili.com/video/BV1xx411c7mD",
        }
    ]
    monkeypatch.setattr(app, "search_bilibili_videos", lambda keyword: expected)

    assert app.search_song_catalog("B站", "七里香") == expected


def test_result_cards_escape_title_and_include_copy_button():
    cards = app._catalog_cards_html(
        [
            {
                "title": "<script>",
                "artist": "作者",
                "cover": "",
                "url": "https://example.com/a",
            }
        ]
    )

    assert "&lt;script&gt;" in cards
    assert 'data-copy-url="https://example.com/a"' in cards
    assert "navigator.clipboard.writeText" in cards


def test_search_ui_returns_first_result_as_copyable_link(monkeypatch):
    link = "https://music.163.com/#/song?id=1"
    monkeypatch.setattr(
        app,
        "search_song_catalog",
        lambda *_: [{"title": "歌", "artist": "人", "cover": "", "url": link}],
        raising=False,
    )

    _state, _cards, update, _cover, selected_link = app.search_song_catalog_ui("网易云", "歌")

    assert selected_link == link
    assert update["value"] == link
