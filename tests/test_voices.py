from youtube2podcast.voices import fallback_voice, voice_choices


def test_cosyvoice_presets_and_custom_voices():
    voices = voice_choices(
        "FunAudioLLM/CosyVoice2-0.5B",
        [{"uri": "speech:mine:abc", "customName": "我的声音"}],
    )
    assert voices[0]["id"].endswith(":alex")
    assert voices[-1] == {"id": "speech:mine:abc", "label": "我的声音"}
    assert voice_choices("other-model") == []
    assert fallback_voice("FunAudioLLM/CosyVoice2-0.5B", "FunAudioLLM/CosyVoice2-0.5B:alex").endswith(":diana")
    assert fallback_voice("other-model", "x") == ""
