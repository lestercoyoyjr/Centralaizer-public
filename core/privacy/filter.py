"""
Privacy filter — gap #3 from MemPrivacy (arXiv:2605.09530).

Before any text is injected into a prompt sent to the local LLM,
PII spans are detected with spaCy and replaced with typed placeholders.
The placeholder map is kept in-memory only — never persisted.

Example
-------
Input:  "Alice's email is alice@acme.com and her SSN is 123-45-6789"
Output: "PERSON_1's email is EMAIL_1 and her SSN is PHONE_1"
Map:    {"PERSON_1": "Alice", "EMAIL_1": "alice@acme.com", "PHONE_1": "123-45-6789"}
"""
from __future__ import annotations
import re
import threading

try:
    import spacy
    _nlp = spacy.load("en_core_web_sm")
    _SPACY_OK = True
except (ImportError, OSError):
    _SPACY_OK = False
    _nlp = None

from config.settings import settings

# Simple regex fallbacks when spaCy isn't installed
_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
_PHONE_RE = re.compile(r"\b(\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b")
_CRED_RE  = re.compile(r"\b(sk-[a-zA-Z0-9]{20,}|ghp_[a-zA-Z0-9]{30,}|Bearer\s+\S{10,})\b")

_lock = threading.Lock()


class MaskedText:
    """Holds masked text + placeholder → original mapping."""
    def __init__(self, text: str, mapping: dict[str, str]):
        self.text = text
        self.mapping = mapping  # {"PERSON_1": "Alice", ...}

    def restore(self, text: str) -> str:
        """Substitute placeholders back with originals."""
        for placeholder, original in self.mapping.items():
            text = text.replace(placeholder, original)
        return text


def mask(text: str) -> MaskedText:
    """
    Detect PII in *text* and replace with typed placeholders.
    Returns a MaskedText whose .mapping lets you restore originals.
    """
    mapping: dict[str, str] = {}
    counters: dict[str, int] = {}

    def _replace(label: str, span: str) -> str:
        counters[label] = counters.get(label, 0) + 1
        placeholder = f"{label}_{counters[label]}"
        mapping[placeholder] = span
        return placeholder

    # 1. Regex-based (API keys, emails, phones) — run first so spaCy doesn't see them
    def _sub(pattern: re.Pattern, label: str, t: str) -> str:
        return pattern.sub(lambda m: _replace(label, m.group()), t)

    text = _sub(_CRED_RE,  "CREDENTIAL", text)
    text = _sub(_EMAIL_RE, "EMAIL",      text)
    text = _sub(_PHONE_RE, "PHONE",      text)

    # 2. spaCy NER for named entities (persons, orgs, locations)
    if _SPACY_OK and _nlp is not None:
        doc = _nlp(text)
        # Process in reverse so character offsets stay valid
        for ent in reversed(doc.ents):
            label = ent.label_
            if label not in settings.pii_entity_types:
                continue
            placeholder = _replace(label, ent.text)
            text = text[: ent.start_char] + placeholder + text[ent.end_char :]

    return MaskedText(text, mapping)


def mask_batch(texts: list[str]) -> list[MaskedText]:
    return [mask(t) for t in texts]
