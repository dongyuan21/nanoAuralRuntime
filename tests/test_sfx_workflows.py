# pyright: reportMissingImports=false
from __future__ import annotations

from nano_aural_runtime_workflows import DEFAULT_WORKFLOW_CATALOG as CATALOG
from nano_aural_runtime_workflows import (
    GENERATE_AND_MUX,
    TEXT_GENERATE,
    VIDEO_GENERATE,
    WorkflowError,
    assert_adapter_does_not_mux,
)


def test_catalog_splits_text_video_and_mux():
    text = CATALOG.get(TEXT_GENERATE)
    video = CATALOG.get(VIDEO_GENERATE)
    mux = CATALOG.get(GENERATE_AND_MUX)
    assert text.adapters == ("stable-audio-3-small-sfx",)
    assert text.operations == ("audio.text_to_sfx",)
    assert text.mux is False
    assert "woosh-v2a" in video.adapters
    assert "controlfoley" in video.adapters
    assert video.default_backend == "dvflow-8s"
    assert "vflow-8s" in video.backends
    assert "T2A" not in video.operations
    assert mux.mux is True
    assert mux.operations == (GENERATE_AND_MUX,)


def test_mux_is_not_an_adapter_operation():
    try:
        assert_adapter_does_not_mux("woosh-v2a", GENERATE_AND_MUX)
    except WorkflowError:
        pass
    else:
        raise AssertionError("mux must not be an adapter operation")
    assert_adapter_does_not_mux("woosh-v2a", "audio.video_to_sfx")
    try:
        assert_adapter_does_not_mux("woosh-v2a", "audio.text_to_sfx")
    except WorkflowError:
        return
    raise AssertionError("Woosh T2A must remain absent")


def test_optional_comfyui_mapping_has_no_woosh_t2a_nodes():
    from integrations.comfyui_compat.sfx_mapping import (
        FORBIDDEN_NODE_NAMES,
        SFX_COMFYUI_NODE_NAMES,
        display_name,
    )

    assert display_name(TEXT_GENERATE) == "NanoAuralStableAudio3TextToSfx"
    assert display_name(VIDEO_GENERATE) == "NanoAuralVideoToSfx"
    assert display_name(GENERATE_AND_MUX) == "NanoAuralGenerateAndMux"
    for name in FORBIDDEN_NODE_NAMES:
        assert name not in SFX_COMFYUI_NODE_NAMES.values()
