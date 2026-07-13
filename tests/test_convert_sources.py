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


def test_convert_uses_external_reference_without_reading_saved_profile(monkeypatch, tmp_path):
    target = tmp_path / "target.wav"
    prompt = tmp_path / "prompt.wav"
    result = tmp_path / "result.wav"
    target.write_bytes(b"RIFFxxxxWAVE")
    prompt.write_bytes(b"RIFFxxxxWAVE")
    result.write_bytes(b"RIFFxxxxWAVE")
    resolved_paths = iter([target, prompt])
    called: list[tuple[object, ...]] = []

    monkeypatch.setattr(app, "_resolve_input_audio", lambda *_args: next(resolved_paths))
    monkeypatch.setattr(app, "_validate_parameters", lambda *_args: (0, 1, 1.0, 42))
    monkeypatch.setattr(app, "_soulx_asset_fingerprint", lambda: {})
    monkeypatch.setitem(app.CONFIG["cache"], "enabled", False)
    monkeypatch.setattr(app, "_find_profile", lambda *_args: (_ for _ in ()).throw(AssertionError("profile should not be read")))

    def fake_call(*args):
        called.append(args)
        return result, "/soulx_svc_convert_path", tmp_path

    def fake_export(_source, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"mp3")
        return destination

    monkeypatch.setattr(app, "_call_soulx", fake_call)
    monkeypatch.setattr(app, "_export_mp3", fake_export)
    monkeypatch.setattr(app, "_cleanup_soulx_download_dir", lambda _path: None)

    output, cache_hit = app.convert("target", "missing", reference_source="reference")

    assert Path(output).is_file()
    assert cache_hit == "false"
    assert called[0][0] == prompt
    assert called[0][1] == target
