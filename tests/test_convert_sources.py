from __future__ import annotations

import inspect
from pathlib import Path

import app


def test_convert_keeps_legacy_parameter_prefix_and_adds_optional_sources():
    parameters = list(inspect.signature(app.convert).parameters.values())
    assert [parameter.name for parameter in parameters[:11]] == [
        "song_name_src",
        "model_dropdown",
        "prompt_vocal_sep",
        "target_vocal_sep",
        "auto_shift",
        "auto_mix_acc",
        "pitch_shift",
        "n_step",
        "cfg",
        "seed",
        "random_seed",
    ]
    assert [parameter.name for parameter in parameters[11:14]] == [
        "target_upload",
        "reference_source",
        "reference_upload",
    ]
    assert [parameter.default for parameter in parameters[11:14]] == [None, "", None]


def test_uploaded_input_wins_over_text(monkeypatch, tmp_path):
    upload = tmp_path / "reference.wav"
    upload.write_bytes(b"RIFFxxxxWAVE")
    monkeypatch.setattr(app, "_validate_target_file", lambda path: Path(path))
    monkeypatch.setattr(
        app,
        "_resolve_audio_source",
        lambda *_: (_ for _ in ()).throw(AssertionError("text should not be read")),
        raising=False,
    )

    assert app._resolve_input_audio("BV1xx411c7mD", str(upload), "参考音色") == upload


def test_explicit_bilibili_source_uses_bilibili_downloader(monkeypatch, tmp_path):
    expected = tmp_path / "bilibili.wav"
    monkeypatch.setattr(app, "_resolve_bilibili_audio", lambda source, callback=None: expected, raising=False)

    assert app._resolve_audio_source("BV1xx411c7mD") == expected
