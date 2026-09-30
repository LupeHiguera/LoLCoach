"""Versioned moment JSON and evidence checks; valid structure does not prove a claim."""
import json
import re

MAX_WINDOW_MS = 60_000
MAX_FRAMES = 8
MAX_EVENTS = 12
MAX_TEXT_CHARS = 24_000


def text(limit=600, nullable=False, pattern=None):
    schema = dict(type=['string', 'null'] if nullable else 'string', maxLength=limit, minLength=1)
    if pattern:
        schema['pattern'] = pattern
    return schema


def obj(**properties):
    return dict(type='object', properties=properties, required=list(properties), additionalProperties=False)


def array(items, maximum, minimum=0):
    return dict(type='array', items=items, maxItems=maximum, minItems=minimum)


def choice(*values):
    return dict(enum=list(values))


ID = text(128, pattern=r'^[A-Za-z0-9_-]+$')
MS = dict(type='integer', minimum=0)
REFS = array(ID, 8, 1)
FRAME = obj(id=ID, game_ms=MS, image_file=text(160, pattern=r'^frame-[0-9]+\.png$'))
EVENT = obj(id=ID, game_ms=MS, source=choice('riot_timeline'),
            visibility=choice('global_event_not_player_view'), side=choice('ally', 'enemy', None),
            killer=text(32, True), victim=text(32, True), assists=array(text(32), 5),
            me=choice('kill', 'death', 'assist', None))
KNOWLEDGE = obj(id=ID, patch=text(64), text=text(800))
PACKET = obj(schema_version=choice(1), moment_id=ID, patch=text(64, True),
             champion=text(32, True), opponent=text(32, True), start_ms=MS, decision_ms=MS,
             sync_verified=dict(type='boolean'), focus=text(500),
             frames=array(FRAME, MAX_FRAMES), events=array(EVENT, MAX_EVENTS),
             knowledge=array(KNOWLEDGE, 4))
OBSERVATIONS = obj(schema_version=choice(1), moment_id=ID,
                   observations=array(obj(id=ID, game_ms=MS, statement=text(),
                                          source=choice('local_vision'),
                                          verification=choice('model_observed'), evidence_refs=REFS), 16),
                   unknowns=array(text(300), 12))
ALTERNATIVE = obj(action=text(), tradeoff=text(500), evidence_refs=REFS)
REVIEW = obj(schema_version=choice(1), moment_id=ID,
             assessment=choice('reviewable', 'needs_more_evidence'),
             claims=array(obj(statement=text(), kind=choice('observation', 'hypothesis'), evidence_refs=REFS), 6),
             alternative=dict(anyOf=[ALTERNATIVE, dict(type='null')]),
             practice_focus=text(500, True), missing_evidence=array(text(300), 8))
SCHEMAS = dict(packet=PACKET, observations=OBSERVATIONS, review=REVIEW)


def validate(value, schema, path='$'):
    """Validate the JSON Schema subset used here, including strict object keys."""
    if 'anyOf' in schema:
        for option in schema['anyOf']:
            try:
                validate(value, option, path)
                return
            except ValueError:
                pass
        raise ValueError(f'{path}: does not match an allowed type')
    if 'enum' in schema:
        if not any(type(value) is type(v) and value == v for v in schema['enum']):
            raise ValueError(f'{path}: invalid enum value')
        return
    kinds = schema['type'] if isinstance(schema['type'], list) else [schema['type']]
    types = dict(object=dict, array=list, string=str, integer=int, boolean=bool, null=type(None))
    if type(value) not in [types[k] for k in kinds]:
        raise ValueError(f'{path}: invalid type')
    if value is None:
        return
    if isinstance(value, dict):
        properties = schema['properties']
        if set(value) != set(schema['required']):
            raise ValueError(f'{path}: missing or unexpected fields')
        for key, item in value.items():
            validate(item, properties[key], f'{path}.{key}')
    elif isinstance(value, list):
        if not schema.get('minItems', 0) <= len(value) <= schema['maxItems']:
            raise ValueError(f'{path}: too many or too few items')
        for index, item in enumerate(value):
            validate(item, schema['items'], f'{path}[{index}]')
    elif isinstance(value, str):
        if not value.strip() or not schema['minLength'] <= len(value) <= schema['maxLength']:
            raise ValueError(f'{path}: empty or overlong text')
        if 'pattern' in schema and re.fullmatch(schema['pattern'], value) is None:
            raise ValueError(f'{path}: invalid identifier or filename')
    elif type(value) is int and value < schema.get('minimum', 0):
        raise ValueError(f'{path}: out of bounds')


def unique_ids(items):
    ids = [item['id'] for item in items]
    if len(ids) != len(set(ids)):
        raise ValueError('Duplicate evidence IDs')
    return set(ids)


def validate_packet(packet):
    validate(packet, PACKET)
    start, end = packet['start_ms'], packet['decision_ms']
    if not 0 <= end - start <= MAX_WINDOW_MS:
        raise ValueError('Moment must have a window of at most 60 seconds')
    unique_ids(packet['frames'] + packet['events'] + packet['knowledge'])
    for item in packet['frames'] + packet['events']:
        if not start <= item['game_ms'] <= end:
            raise ValueError('Evidence falls outside the decision window')
    if len({f['image_file'] for f in packet['frames']}) != len(packet['frames']):
        raise ValueError('Duplicate frame filenames')
    for rule in packet['knowledge']:
        if rule['patch'] not in ('general', packet['patch']):
            raise ValueError('Knowledge patch does not match this moment')
    if len(json.dumps(packet, ensure_ascii=False)) > MAX_TEXT_CHARS:
        raise ValueError('Moment exceeds the text budget')


def validate_observations(observations, packet):
    validate_packet(packet)
    validate(observations, OBSERVATIONS)
    if observations['moment_id'] != packet['moment_id']:
        raise ValueError('Observations belong to another moment')
    frames = {f['id']: f['game_ms'] for f in packet['frames']}
    ids = unique_ids(observations['observations'])
    if ids & unique_ids(packet['frames'] + packet['events'] + packet['knowledge']):
        raise ValueError('Observation ID collides with input evidence')
    for observation in observations['observations']:
        refs = observation['evidence_refs']
        if len(refs) != len(set(refs)) or not set(refs) <= frames.keys():
            raise ValueError('Observation cites unknown or duplicate frames')
        times = [frames[ref] for ref in refs]
        if not min(times) <= observation['game_ms'] <= max(times):
            raise ValueError('Observation time is not supported by its cited frames')


def validate_review(review, packet, observations):
    validate_observations(observations, packet)
    validate(review, REVIEW)
    if review['moment_id'] != packet['moment_id']:
        raise ValueError('Review belongs to another moment')
    evidence = unique_ids(observations['observations'] + packet['events'])
    rules = unique_ids(packet['knowledge'])
    cited = review['claims'] + ([review['alternative']] if review['alternative'] else [])
    for claim in cited:
        refs = claim['evidence_refs']
        if len(refs) != len(set(refs)) or not set(refs) <= evidence | rules or not set(refs) & evidence:
            raise ValueError('Claim needs valid moment evidence, not just a game rule')
    if review['assessment'] == 'needs_more_evidence':
        if review['alternative'] is not None or review['practice_focus'] is not None or not review['missing_evidence']:
            raise ValueError('Insufficient evidence cannot produce advice or a practice focus')
    elif not evidence:
        raise ValueError('A reviewable moment needs evidence')
    if review['practice_focus'] is not None and review['alternative'] is None:
        raise ValueError('A practice focus needs a supported alternative')
