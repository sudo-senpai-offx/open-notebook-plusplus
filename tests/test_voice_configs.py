"""The config the phone ships must be the config piper-plus can load.

The first device install of the guest TTS backend died at its final
step, after the 38 MB model had already downloaded:

    ValueError: 'text' is not a valid PhonemeType

piper-plus 1.13.0's enum is openjtalk/bilingual/multilingual - the
values piper 1.x used (text/espeak) are not in it at all, which is also
why stock piper cannot load this voice and piper-plus cannot load the
stock-piper configs. The PC hid the mismatch for weeks: the PC server
patches phoneme_type text -> multilingual at load time before handing
the file to piper-plus (piper-ja/server_ja.py), a patch the device path
never had. The repository copy therefore ships the value the runtime
accepts, and these tests hold that line without importing piper-plus:

* phoneme_type must be one of the enum's values,
* the fields piper's from_dict reads unconditionally must be present
  (a missing one is a KeyError at load, at first synthesis, on a phone),
* the installer must run piper's own parser BEFORE downloading the
  model, so the next mismatch fails in a second rather than after 38 MB.
"""

import json
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CONFIGS = REPO / "scripts" / "voice-configs"
INSTALLER = REPO / "scripts" / "termux-tts-ja-install.sh"

# piper-plus==1.13.0's PhonemeType (piper/config.py). The version is
# pinned as PIPER_VERSION in termux-tts-ja-install.sh: if that pin
# moves, re-read the enum there and update this set with it.
PIPER_PLUS_PHONEME_TYPES = {"openjtalk", "bilingual", "multilingual"}

# What PiperConfig.from_dict indexes without a default, plus the nested
# audio.sample_rate it reaches through config["audio"].
FROM_DICT_REQUIRED = ("num_symbols", "num_speakers", "phoneme_id_map")


def _configs() -> list[Path]:
    found = sorted(CONFIGS.glob("*.json"))
    assert found, "no shipped voice configs - the guard has nothing to guard"
    return found


def test_phoneme_type_is_what_piper_plus_accepts() -> None:
    for path in _configs():
        raw = json.loads(path.read_text(encoding="utf-8"))
        phoneme_type = raw.get("phoneme_type")
        assert phoneme_type in PIPER_PLUS_PHONEME_TYPES, (
            f"{path.name}: piper-plus 1.13.0 refuses phoneme_type "
            f"{phoneme_type!r} with ValueError at load - the device "
            "install proved exactly this, after the model had downloaded"
        )


def test_from_dict_required_fields_are_present() -> None:
    for path in _configs():
        raw = json.loads(path.read_text(encoding="utf-8"))
        for field in FROM_DICT_REQUIRED:
            assert field in raw, (
                f"{path.name}: {field} is indexed without a default by "
                "PiperConfig.from_dict - KeyError at load"
            )
        assert isinstance(raw.get("audio"), dict), f"{path.name}: missing audio"
        assert "sample_rate" in raw["audio"], (
            f"{path.name}: config['audio']['sample_rate'] is indexed without "
            "a default - KeyError at load"
        )


def test_the_installer_validates_with_piper_before_downloading() -> None:
    """The ordering is the fix, not merely the presence of a check.

    Validating after the download is a 38 MB mistake; validating before
    it turns the same failure into a one-second one. The check must be
    piper's own from_dict - reproducing the accepted values in shell
    would drift away from the runtime the moment the version pin moves.
    """
    text = INSTALLER.read_text(encoding="utf-8")
    assert "PiperConfig.from_dict" in text, (
        "the install must run piper-plus's own config parser against the "
        "shipped json before trusting it"
    )
    validation = text.index("PiperConfig.from_dict")
    download = text.index('fetch_exact "$MODEL_URL"')
    assert validation < download, (
        "config validation must run before the 38 MB model fetch, or the "
        "mismatch it catches gets paid for in bandwidth first"
    )
