#!/usr/bin/env python3
"""
CI enforcement for locale completeness.

Checks that every locale file:
1. Contains all keys from the en-US reference (no missing keys)
2. Has no values that match the en-US English value (no untranslated keys)

Exclusions:
- Keys under 'attachments.*' (being removed per D11 migration)
- Language name keys (common.english, common.chinese, etc.) - may remain in native script
- Proper noun keys (common.appName, common.apiUrl, etc.)
- Template-only values ({{count}}, {{name}}, etc.)
- URLs

Exit code 1 if any violations found.
"""
import json
import re
import sys
from pathlib import Path

LOCALE_DIR = Path(__file__).resolve().parent.parent / "frontend/src/lib/locales"
LOCALES = [
    "en-US", "tr-TR", "ca-ES", "zh-CN", "zh-TW", "pt-BR", "ja-JP",
    "it-IT", "fr-FR", "ru-RU", "bn-IN", "es-ES", "de-DE", "pl-PL",
]

# Keys that are exempt from translation (proper nouns, language names, etc.)
TRANSLATION_EXEMPT_KEYS = {
    # Language names - often kept in native script or English
    'common.english', 'common.chinese', 'common.japanese', 'common.french',
    'common.russian', 'common.bengali', 'common.catalan', 'common.spanish',
    'common.german', 'common.polish', 'common.turkish',
    'common.traditionalChinese', 'common.portuguese',
    # Proper nouns / brand names
    'common.appName', 'common.apiUrl', 'common.frontendUrl',
    # Generic terms that are the same across languages
    'common.url', 'common.model', 'common.name', 'common.type',
    'auth.loginTitle',
    'sites.urlPlaceholder',
    # Chat keys that are generic
    'chat.message', 'chat.tokenCountMessage', 'chat.tokenCountReasoning',
    'chat.tokenCountResponse',
    # Navigation items being removed (attachments migration D11)
    'navigation.attachments',
}

# Prefixes of keys exempt from translation
TRANSLATION_EXEMPT_PREFIXES = (
    'attachments.',  # Being removed per D11 attachment->source migration
)

# Prefixes of keys exempt from the MISSING check during the i18n stabilization
# window (Week 1 Q8). New poly-pipeline keys stabilize in English first;
# non-English locales will be filled in during the translation pass.
I18N_TODO_PREFIXES = (
    'queueBacklog.',      # Week 6 Day 2-3 queue backlog panel
    'capabilities.',      # Week 6 Day 2-3 capability status card
    'sources.statusExtractingPartial',
    'sources.statusExtractingFailed',
    'sources.statusEmbeddingPending',
    'sources.retryStarted',
    'sources.retryFailed',
    'sources.retrying',
    'sources.reprocessWithLanguage',
    'sources.selectLanguage',
)

# Values that are template-only or non-translatable
NON_TRANSLATABLE_VALUES = {
    '{{count}}', '{{name}}', '{{time}}', '{{date}}', '{{query}}',
    '{{seconds}}s', '{{value}} tokens', '{{value}} chars',
    '{{done}} of {{total}}', '{{hours}} hrs high-intensity today',
    '{{hours}} hrs/day', '{{count}} URL(s)', '{{count}} file(s)',
    '{{count}} sources selected', '{{count}} items selected',
    '{{queued}}', '{{skipped}}', '{{processed}}/{{total}}',
    '{{percent}}%', '{{failed}}', '{{success}}', '{{graded}}',
    '{{total}}', '{{kept}}', '{{minutes}}', '{{platform}}',
    '{platform}', '{time}', '{count}', '{id}', '{title}', '{minutes}',
}

# Keys exempt from MISSING check (new keys not yet added to some locales)
MISSING_EXEMPT_KEYS = {
    'duckAiTitle',  # New key, locales will be updated
}


def strip_comments(text: str) -> str:
    """Remove // line comments only when at start of line (not in URLs)."""
    text = re.sub(r'(?m)^\s*//[^\n]*', '', text)
    text = re.sub(r'/\*.*?\*/', '', text, flags=re.DOTALL)
    return text


def remove_string_contents(text: str) -> str:
    """Replace string contents with placeholders to avoid brace counting issues."""
    text = re.sub(r'"(?:[^"\\]|\\.)*"', '""', text)
    text = re.sub(r"'(?:[^'\\]|\\.)*'", "''", text)
    return text


def extract_key_values(text: str) -> dict:
    """Extract all key-value pairs from a TypeScript locale object.
    Returns a flat dict with dot-notation keys."""
    result = {}

    match = re.search(r'export\s+const\s+\w+\s*=\s*\{', text)
    if not match:
        return result

    start = match.end() - 1  # position of opening brace

    text_no_strings = remove_string_contents(text[start:])
    depth = 0
    end = None
    for i, ch in enumerate(text_no_strings):
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                end = start + i + 1
                break

    if end is None:
        return result

    obj_text = text[start:end]
    lines = obj_text.split('\n')
    key_stack = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        line_no_strings = remove_string_contents(stripped)
        opens = line_no_strings.count('{')
        closes = line_no_strings.count('}')

        m = re.match(r'^(\s*)([a-zA-Z_][a-zA-Z0-9_]*)\s*:\s*(.+?)\s*,?\s*$', stripped)
        if m:
            indent, key, value = m.groups()

            for _ in range(closes):
                if key_stack:
                    key_stack.pop()

            value_stripped = value.strip().rstrip(',').strip()
            value_no_strings = remove_string_contents(value_stripped)

            if value_no_strings.endswith('{'):
                key_stack.append(key)
            else:
                str_value = extract_string_value(value)
                if str_value is not None:
                    full_key = '.'.join(key_stack + [key])
                    result[full_key] = str_value

        else:
            for _ in range(closes):
                if key_stack:
                    key_stack.pop()

    return result


def extract_string_value(value: str) -> str | None:
    """Extract the string value from a value expression."""
    value = value.strip().rstrip(',').strip()

    m = re.match(r'^"(.*)"$', value, re.DOTALL)
    if m:
        return m.group(1)

    m = re.match(r"^'(.*)'$", value, re.DOTALL)
    if m:
        return m.group(1)

    return None


def extract_i18n_todo_keys(text: str) -> set:
    """Extract keys marked with ``// i18n-todo`` comments from en-US.

    A key is considered i18n-todo marked if:
    - The line containing the key definition has a trailing ``// i18n-todo``
      comment, OR
    - The line immediately preceding the key definition is a
      ``// i18n-todo`` comment (covers parent object keys whose children
      inherit the exemption).

    Returns a set of flat dot-notation key prefixes (e.g. ``queueBacklog``,
    ``capabilities``, ``sources.statusExtractingPartial``). Callers should
    treat any key starting with one of these prefixes as exempt from the
    MISSING check during the stabilization window.
    """
    todo_keys = set()
    lines = text.split('\n')

    for i, line in enumerate(lines):
        stripped = line.strip()
        if '// i18n-todo' not in stripped:
            continue

        # Case 1: key is on the same line as the comment
        #   key: value, // i18n-todo
        m = re.match(r'^(\s*)([a-zA-Z_][a-zA-Z0-9_]*)\s*:', stripped)
        if m:
            todo_keys.add(m.group(2))
            continue

        # Case 2: key is on the next non-empty, non-comment line
        for j in range(i + 1, min(i + 4, len(lines))):
            next_stripped = lines[j].strip()
            if not next_stripped or next_stripped.startswith('//'):
                continue
            m = re.match(r'^(\s*)([a-zA-Z_][a-zA-Z0-9_]*)\s*:', next_stripped)
            if m:
                todo_keys.add(m.group(2))
            break

    return todo_keys


def is_exempt_from_translation(key: str, value: str) -> bool:
    """Check if a key is exempt from translation requirements."""
    if key in TRANSLATION_EXEMPT_KEYS:
        return True
    if any(key.startswith(p) for p in TRANSLATION_EXEMPT_PREFIXES):
        return True
    if value in NON_TRANSLATABLE_VALUES:
        return True
    return False


def is_exempt_from_missing(key: str, i18n_todo_keys: set | None = None) -> bool:
    """Check if a key is exempt from the missing-key check."""
    # Extract the leaf key (last segment)
    leaf = key.split('.')[-1]
    if leaf in MISSING_EXEMPT_KEYS:
        return True
    # Keys marked with // i18n-todo in en-US are exempt during stabilization
    if i18n_todo_keys and any(key == k or key.startswith(k + '.') for k in i18n_todo_keys):
        return True
    # Keys matching explicit I18N_TODO_PREFIXES are exempt
    if any(key.startswith(p) for p in I18N_TODO_PREFIXES):
        return True
    return False


def check_locales():
    """Check all locales for missing and untranslated keys."""
    en_us_text = (LOCALE_DIR / "en-US" / "index.ts").read_text(encoding="utf-8")
    reference = extract_key_values(strip_comments(en_us_text))
    # Keys marked with // i18n-todo in en-US are exempt from MISSING during
    # the stabilization window (Week 1 Q8).
    i18n_todo_keys = extract_i18n_todo_keys(en_us_text)

    errors = []

    for locale in LOCALES:
        if locale == "en-US":
            continue

        path = LOCALE_DIR / locale / "index.ts"
        if not path.exists():
            errors.append(f"{locale}: FILE NOT FOUND")
            continue

        data = extract_key_values(strip_comments(
            path.read_text(encoding="utf-8")
        ))

        # Check for missing keys
        for key, ref_value in reference.items():
            if key not in data:
                if not is_exempt_from_missing(key, i18n_todo_keys):
                    errors.append(f"{locale}: MISSING key '{key}' (en: '{ref_value}')")

        # Check for untranslated keys (value matches en-US)
        for key, value in data.items():
            if key in reference and value == reference[key]:
                if not is_exempt_from_translation(key, value):
                    errors.append(f"{locale}: UNTRANSLATED key '{key}' (value: '{value}')")

    if errors:
        print(f"LOCALE CHECK FAILED: {len(errors)} error(s)\n")
        for err in errors:
            print(f"  {err}")
        sys.exit(1)
    else:
        print("LOCALE CHECK PASSED: all locales are complete and translated")
        sys.exit(0)


if __name__ == "__main__":
    check_locales()
