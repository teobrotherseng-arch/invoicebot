"""
Speech-to-text. Uses OpenAI's Whisper API because it handles code-switched
(mixed Chinese/English in the same clip) audio much more reliably than most
alternatives, and returns the raw multilingual transcript rather than forcing
a single language. We do the *translation* step separately with Claude
(see extract.py) because that gives much better quality on mixed sentences
than Whisper's built-in translate endpoint, which assumes one dominant language.
"""
from openai import OpenAI
from config import OPENAI_API_KEY

client = OpenAI(api_key=OPENAI_API_KEY)


def transcribe_audio(file_path: str) -> str:
    """
    Returns the raw transcript, as spoken - may be a mix of Chinese and English.
    """
    with open(file_path, "rb") as f:
        result = client.audio.transcriptions.create(
            model="whisper-1",
            file=f,
            response_format="text",
            # language intentionally omitted -> auto-detect / mixed-language friendly
        )
    return result.strip() if isinstance(result, str) else result.text.strip()
