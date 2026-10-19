#!/usr/bin/env python3
"""Add the two speech residency labels and three hold reasons to every locale.

Written as a script rather than fifteen hand-edits because a key added to one
locale and not the others fails the locale test in a way that reads like a
translation bug rather than a forgotten file - and because the previous
`cpu-budget` reason was missing from all of them, which the test also did not
catch. Adding the keys through the same code path the test reads is the only
way to be sure.

Run from the repo root:  .venv/bin/python scripts/add-speech-locale-keys.py
"""

import json
import re
import sys
from pathlib import Path

LOCALES = Path("frontend/src/lib/locales")

# role label -> {locale: text}
ROLES = {
    "residency_stt": {
        "en-US": "speech-to-text model",
        "bn-IN": "স্পিচ-টু-টেক্সট মডেল",
        "ca-ES": "model de veu a text",
        "de-DE": "Sprach-zu-Text-Modell",
        "es-ES": "modelo de voz a texto",
        "fr-FR": "modèle de transcription vocale",
        "it-IT": "modello da voce a testo",
        "ja-JP": "音声認識モデル",
        "pl-PL": "model zamiany mowy na tekst",
        "pt-BR": "modelo de fala para texto",
        "ru-RU": "модель распознавания речи",
        "tr-TR": "konuşmadan metne modeli",
        "zh-CN": "语音转文字模型",
        "zh-TW": "語音轉文字模型",
    },
    "residency_tts": {
        "en-US": "text-to-speech model",
        "bn-IN": "টেক্সট-টু-স্পিচ মডেল",
        "ca-ES": "model de text a veu",
        "de-DE": "Text-zu-Sprache-Modell",
        "es-ES": "modelo de texto a voz",
        "fr-FR": "modèle de synthèse vocale",
        "it-IT": "modello da testo a voce",
        "ja-JP": "音声合成モデル",
        "pl-PL": "model zamiany tekstu na mowę",
        "pt-BR": "modelo de texto para fala",
        "ru-RU": "модель синтеза речи",
        "tr-TR": "metinden sese modeli",
        "zh-CN": "文字转语音模型",
        "zh-TW": "文字轉語音模型",
    },
}

REASONS = {
    "reason_cpu-budget": {
        "en-US": "{{count}} waiting: CPU is fully committed",
        "bn-IN": "{{count}} অপেক্ষমাণ: CPU সম্পূর্ণভাবে ব্যবহৃত",
        "ca-ES": "{{count}} en espera: la CPU està commitmentada",
        "de-DE": "{{count}} wartend: CPU ist vollständig belegt",
        "es-ES": "{{count}} en espera: CPU está comprometida",
        "fr-FR": "{{count}} en attente : CPU entièrement engagée",
        "it-IT": "{{count}} in attesa: CPU completamente occupata",
        "ja-JP": "{{count}}件待機中: CPUが完全に使用中",
        "pl-PL": "{{count}} czeka: procesor jest w pełni zajęty",
        "pt-BR": "{{count}} aguardando: CPU totalmente ocupada",
        "ru-RU": "{{count}} ожидают: ЦП полностью загружен",
        "tr-TR": "{{count}} bekliyor: CPU tamamen kullanımda",
        "zh-CN": "{{count}} 个等待：CPU 已完全占用",
        "zh-TW": "{{count}} 個等待：CPU 已完全佔用",
    },
    "reason_class_limit_stt": {
        "en-US": "{{count}} waiting: transcription is already running",
        "bn-IN": "{{count}} অপেক্ষমাণ: ট্রান্সক্রিপশন ইতিমধ্যে চলছে",
        "ca-ES": "{{count}} en espera: la transcripció ja està en marxa",
        "de-DE": "{{count}} wartend: Transkription läuft bereits",
        "es-ES": "{{count}} en espera: la transcripción ya está en marcha",
        "fr-FR": "{{count}} en attente : transcription déjà en cours",
        "it-IT": "{{count}} in attesa: la trascrizione è già in corso",
        "ja-JP": "{{count}}件待機中: 文字起こしはすでに実行中",
        "pl-PL": "{{count}} czeka: transkrypcja już trwa",
        "pt-BR": "{{count}} aguardando: a transcrição já está em execução",
        "ru-RU": "{{count}} ожидают: расшифровка уже выполняется",
        "tr-TR": "{{count}} bekliyor: yazıya dökme zaten çalışıyor",
        "zh-CN": "{{count}} 个等待：转录已在进行中",
        "zh-TW": "{{count}} 個等待：轉錄已在進行中",
    },
    "reason_class_limit_tts": {
        "en-US": "{{count}} waiting: synthesis is already running",
        "bn-IN": "{{count}} অপেক্ষমাণ: সিনথেসিস ইতিমধ্যে চলছে",
        "ca-ES": "{{count}} en espera: la síntesis ja està en marxa",
        "de-DE": "{{count}} wartend: Sprachausgabe läuft bereits",
        "es-ES": "{{count}} en espera: la síntesis ya está en marcha",
        "fr-FR": "{{count}} en attente : synthèse déjà en cours",
        "it-IT": "{{count}} in attesa: la sintesi è già in corso",
        "ja-JP": "{{count}}件待機中: 音声合成はすでに実行中",
        "pl-PL": "{{count}} czeka: synteza już trwa",
        "pt-BR": "{{count}} aguardando: a síntese já está em execução",
        "ru-RU": "{{count}} ожидают: синтез уже выполняется",
        "tr-TR": "{{count}} bekliyor: sentez zaten çalışıyor",
        "zh-CN": "{{count}} 个等待：语音合成已在进行中",
        "zh-TW": "{{count}} 個等待：語音合成已在進行中",
    },
}

#: The locale files are TypeScript modules (`export default { ... }`), not JSON,
#: so a json.load would fail. This anchors on the last two residency role keys
#: and inserts after the one that is last, which keeps the block contiguous
#: instead of scattering speech labels between unrelated sections.
ANCHORS = [
    "    residency_chat: ",
    "    residency_embed: ",
    "    'reason_class_limit_embed': ",
    "    'reason_memory': ",
]


def insert_after(text: str, anchor_re: str, block: str) -> str:
    # MULTILINE matters: without it `^` anchors to the start of the whole file
    # rather than the start of a line, so every anchor in a 3000-line locale
    # module fails to match and the script reports "anchor not found" for keys
    # that are plainly there.
    matches = list(re.finditer(anchor_re, text, re.MULTILINE))
    if not matches:
        raise SystemExit(f"anchor not found: {anchor_re}")
    last = matches[-1]
    # Find the end of that line, then insert on the next one with the same
    # indentation. The trailing newline is part of the insertion, not optional:
    # without it the next key lands on the same line as this one and the file
    # still parses, which is why the damage is invisible until someone reads it.
    line_end = text.index("\n", last.start())
    indent_match = re.match(r"[ \t]*", text[last.start() :])
    indent = indent_match.group(0) if indent_match else "    "
    return text[: line_end + 1] + indent + block.strip() + "\n" + text[line_end + 1 :]


def main() -> int:
    failures = []
    for locale_dir in sorted(LOCALES.iterdir()):
        if not locale_dir.is_dir():
            continue
        locale = locale_dir.name
        path = locale_dir / "index.ts"
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        original = text

        for key, translations in {**ROLES, **REASONS}.items():
            if locale not in translations:
                raise SystemExit(f"no translation for {key} in {locale}")
            value = translations[locale]
            quoted = json.dumps(value, ensure_ascii=False)
            # Match the file's own style: bare keys when the name is a valid
            # identifier, single quotes when it is not.
            if re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", key):
                literal = f"{key}: {quoted},"
            else:
                literal = f"'{key}': {quoted},"
            if literal in text:
                continue
            if key.startswith("residency_"):
                anchor = r"^    residency_embed: "
            elif key == "reason_cpu-budget":
                anchor = r"^    reason_memory: "
            else:
                anchor = r"^    reason_class_limit_embed: "
            try:
                text = insert_after(text, anchor, literal)
            except SystemExit as exc:
                failures.append(f"{locale}: {exc} ({key})")

        if text != original:
            path.write_text(text, encoding="utf-8")
            print(f"  updated {locale}")
    if failures:
        print("FAILURES:", file=sys.stderr)
        for line in failures:
            print(f"  {line}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
