"""Language selection lives here and nowhere else.

v1 is English-only. Adding Mandarin later means adding one LanguageProfile below
and dropping prompts/system_prompt.zh.md next to the English one — no changes to
main.py, tools.py, or anything else.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

DEFAULT_LANGUAGE = "en"


@dataclass(frozen=True)
class LanguageProfile:
    """Everything that varies by language: the prompt, the STT locale, the voice."""

    code: str
    prompt_file: str
    stt_language: str
    tts_language: str
    voice_env_var: str

    def system_prompt(self) -> str:
        path = PROMPTS_DIR / self.prompt_file
        if not path.is_file():
            raise FileNotFoundError(f"system prompt not found: {path}")
        return path.read_text(encoding="utf-8").strip()

    def voice_id(self) -> str | None:
        return os.environ.get(self.voice_env_var) or None


LANGUAGES: dict[str, LanguageProfile] = {
    "en": LanguageProfile(
        code="en",
        prompt_file="system_prompt.md",
        stt_language="en-US",
        tts_language="en",
        voice_env_var="CARTESIA_VOICE_ID",
    ),
}


def get_language(code: str | None = None) -> LanguageProfile:
    code = (code or os.environ.get("AGENT_LANGUAGE") or DEFAULT_LANGUAGE).lower()
    if code not in LANGUAGES:
        raise ValueError(
            f"unsupported language {code!r}; known: {', '.join(sorted(LANGUAGES))}"
        )
    return LANGUAGES[code]


def load_system_prompt(code: str | None = None) -> str:
    return get_language(code).system_prompt()
