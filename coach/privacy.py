"""Pattern scan for text that must not leave the machine.

A clean scan means none of these shapes matched. It does not prove a human
read the text, and it does not recognise an arbitrary nickname. Paid cloud
runs still require the human approval hash in coach.eval.
"""
import json
import re

# Identity keys, including riotId as a prefix so riotIdGameName matches.
IDENTITY = re.compile(
    r'(?i)(?:\b(?:puuid|summonername|summonerid|accountid|raw_?json|sk-proj-)\b|riotid)')
# Name#tag. A bare champion name has no hash.
RIOT_ID = re.compile(r'\S+#\w+')
URL_OR_DRIVE = re.compile(r'(?i)(?:https?://|file://|data:|[A-Za-z]:[\\/])')
# A leading slash plus a real directory. Fractions such as 1/2 do not match.
POSIX_PATH = re.compile(
    r'(?:^|[\s"\'=])/(?:Users|home|private|tmp|opt|var|workspace|mnt|Volumes|etc|usr)(?:/|$)')
UNC_PATH = re.compile(r'\\\\[A-Za-z0-9._$-]+\\')
# Riot PUUIDs are often UUIDs. Moment ids in this repo are not.
UUID = re.compile(
    r'\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b')
LONG_TOKEN = re.compile(r'\b[A-Za-z0-9_-]{60,}\b')
# A Riot match body, not a sentence that mentions participants.
RAW_MATCH = re.compile(r'(?i)"metadata"\s*:\s*\{[^{}]{0,500}"participants"\s*:')

_CHECKS = (
    ('identity', IDENTITY),
    ('riot_id', RIOT_ID),
    ('url_or_drive', URL_OR_DRIVE),
    ('absolute_path', POSIX_PATH),
    ('unc_path', UNC_PATH),
    ('uuid', UUID),
    ('long_token', LONG_TOKEN),
    ('raw_match', RAW_MATCH),
)


def findings(text):
    """Labels of patterns that matched. Empty is not a privacy approval.

    Escaped quotes are folded so a raw match blob still matches after JSON
    encoding. The labels name the pattern, not the matched text.
    """
    if not isinstance(text, str):
        raise ValueError('Privacy scan expects text')
    folded = text.replace('\\"', '"')
    found = []
    for name, pattern in _CHECKS:
        if pattern.search(text) or (folded != text and pattern.search(folded)):
            found.append(name)
    return found


def assert_clean(value, label='payload'):
    """Raise if JSON or text carries a scanned identifier, path, or raw match blob.

    The error names the payload, not the matched text.
    """
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    if findings(text):
        raise ValueError(f'{label} contains a possible identifier, secret, URL or local path')
