from __future__ import annotations

import inspect

import app


def test_convert_legacy_prefix_and_optional_extensions():
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


def test_convert_extension_components_are_optional_in_gradio_api():
    endpoint = app.APP.get_api_info()["named_endpoints"]["/convert"]
    extensions = endpoint["parameters"][11:14]

    assert [item["parameter_name"] for item in extensions] == [
        "target_upload",
        "reference_source",
        "reference_upload",
    ]
    assert [item["parameter_has_default"] for item in extensions] == [True, True, True]
    assert [item["parameter_default"] for item in extensions] == [None, "", None]
