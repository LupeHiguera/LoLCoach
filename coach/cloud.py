"""Explicit text-only OpenAI benchmark client; never accepts media or match dumps."""
import copy
import hashlib
import json
import os
import re
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from coach import harness

MODEL = 'gpt-6.1-sol'
ENDPOINT = 'https://api.openai.com/v1/responses'
PRICING = dict(as_of='2026-09-29', input_per_million=2.0, cached_per_million=0.10,
               output_per_million=10.0, cache_write_per_million=2.50,
               source='https://developers.openai.com/api/docs/models/gpt-6.1-sol')


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()


def checked_text(packet, observations):
    """Whitelist structured fields and reject obvious identifiers; human checking is still required."""
    request = harness.build_request('review', packet, MODEL, observations)
    context = json.loads(request['messages'][1]['content'][0]['text'])
    # Frames are only evidence links in the observations, not coach input. No file names or images.
    context['packet'].pop('frames')
    serialized = json.dumps(context, ensure_ascii=False)
    forbidden = (r'(?i)\b(?:puuid|summonerName|riotId|raw_json|rawJson|sk-proj-)\b',
                 r'(?i)(?:https?://|file://|data:|[A-Z]:[\\/]|/(?:Users|home|private|tmp)/)',
                 r'\S+#\w+', r'\b[A-Za-z0-9_-]{60,}\b')
    if any(re.search(pattern, serialized) for pattern in forbidden):
        raise ValueError('Cloud text contains a possible identifier, secret, URL or local path')
    return context


def make_request(context, max_output_tokens=4096):
    """One fresh bounded request, with no tools, previous response, conversation or media."""
    if type(max_output_tokens) is not int or not 512 <= max_output_tokens <= 8192:
        raise ValueError('Cloud output token limit must be 512–8192')
    schema = copy.deepcopy(harness.SCHEMAS['review'])

    def enum_types(node):
        if isinstance(node, dict):
            if 'enum' in node and 'type' not in node:
                kinds = list(dict.fromkeys('null' if v is None else 'integer' if type(v) is int
                                          else 'string' for v in node['enum']))
                node['type'] = kinds[0] if len(kinds) == 1 else kinds
            for value in node.values():
                enum_types(value)
        elif isinstance(node, list):
            for value in node:
                enum_types(value)
    enum_types(schema)
    request = dict(model=MODEL, store=False, service_tier='default', reasoning=dict(effort='low'),
                   max_output_tokens=max_output_tokens,
                   input=[dict(role='system', content=harness.REVIEW_PROMPT),
                          dict(role='user', content=json.dumps(context, ensure_ascii=False))],
                   text=dict(format=dict(type='json_schema', name='review', strict=True, schema=schema)))
    if len(json.dumps(request, ensure_ascii=False)) > harness.MAX_TEXT_CHARS:
        raise ValueError('Cloud request exceeds the moment text budget')
    return request


def reserve_usd(request):
    """Conservative allocation, not an invoice: UTF-8 bytes plus overhead, max billed output."""
    input_bound = len(json.dumps(request, ensure_ascii=False).encode('utf-8')) + 1024
    # Reserve cache-write pricing and a regional premium too; no assumed cache savings.
    return (input_bound * PRICING['cache_write_per_million'] +
            request['max_output_tokens'] * PRICING['output_per_million']) * 1.1 / 1_000_000


def cost_usd(usage):
    """Standard-tier estimate from usage; output_tokens already includes reasoning tokens."""
    if not isinstance(usage, dict):
        return None
    incoming, outgoing = usage.get('input_tokens'), usage.get('output_tokens')
    details = usage.get('input_tokens_details')
    if details is None:
        details = {}
    if not isinstance(details, dict):
        return None
    cached = details.get('cached_tokens', 0)
    if any(type(v) is not int or v < 0 for v in (incoming, outgoing, cached)) or cached > incoming:
        return None
    return ((incoming - cached) * PRICING['input_per_million'] +
            cached * PRICING['cached_per_million'] + outgoing * PRICING['output_per_million']) / 1_000_000


def load_key(env_file=Path('.env.local')):
    key = os.environ.get('OPENAI_API_KEY', '').strip()
    if not key and env_file.is_file():
        if env_file.stat().st_size > 65536:
            raise ValueError('Credential file exceeds the size limit')
        for line in env_file.read_text(encoding='utf-8').splitlines():
            name, separator, value = line.strip().removeprefix('export ').partition('=')
            if separator and name.strip() == 'OPENAI_API_KEY':
                key = value.strip().strip('\"\'')
    if not key or any(c.isspace() for c in key):
        raise ValueError('No usable OpenAI key; finish secure key setup before a paid run')
    return key


def complete(request, key):
    """Single fixed HTTPS request. Errors never include headers, server bodies or input text."""
    req = Request(ENDPOINT, json.dumps(request, ensure_ascii=False).encode('utf-8'),
                  headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + key}, method='POST')
    opener = build_opener(ProxyHandler({}), harness.NoRedirect())
    try:
        with opener.open(req, timeout=120) as response:
            body = response.read(harness.MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        raise ValueError(f'OpenAI returned HTTP {exc.code}; no automatic retry') from None
    except (URLError, TimeoutError, OSError):
        raise ValueError('OpenAI connection failed; billing may be unknown; no automatic retry') from None
    if len(body) > harness.MAX_RESPONSE_BYTES:
        raise ValueError('OpenAI response exceeds the byte limit; billing may be unknown')
    try:
        response = json.loads(body)
        if not isinstance(response, dict):
            raise ValueError('OpenAI returned an invalid response envelope')
        return response
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError('OpenAI returned malformed JSON; billing may be unknown') from None


def parse_result(response):
    if not isinstance(response, dict) or response.get('status') != 'completed':
        raise ValueError('OpenAI answer was incomplete or failed')
    output = response.get('output')
    if not isinstance(output, list):
        raise ValueError('OpenAI returned an invalid output envelope')
    chunks = []
    for item in output:
        if not isinstance(item, dict):
            raise ValueError('OpenAI returned an invalid output item')
        if item.get('type') == 'message':
            contents = item.get('content')
            if not isinstance(contents, list):
                raise ValueError('OpenAI returned invalid message content')
            for content in contents:
                if not isinstance(content, dict):
                    raise ValueError('OpenAI returned an invalid content item')
                if content.get('type') == 'refusal':
                    raise ValueError('OpenAI refused this benchmark case')
                if content.get('type') == 'output_text':
                    if not isinstance(content.get('text'), str):
                        raise ValueError('OpenAI returned invalid output text')
                    chunks.append(content['text'])
    try:
        return json.loads(''.join(chunks))
    except (TypeError, json.JSONDecodeError):
        raise ValueError('OpenAI returned no valid JSON review') from None
