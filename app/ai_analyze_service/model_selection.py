"""Pure, run-local Gemini selection; never changes environment or default chains."""
import re


def validate_model(value):
    # Accept model IDs, never URLs, credentials, whitespace, or endpoint overrides.
    if not isinstance(value,str) or not re.fullmatch(r'gemini-[A-Za-z0-9][A-Za-z0-9._-]{0,126}',value):
        raise ValueError('Gemini model override must be a Gemini model ID.')
    return value


def select_chain(default_chain, override=None, *, parent_id=None):
    if override is None:
        return default_chain()
    if type(parent_id) is not int or parent_id <= 0:
        raise ValueError('Gemini model override requires one explicit parent ID.')
    return [validate_model(override)]


def diagnostic_model(value):
    try:return validate_model(value)
    except ValueError:return None
