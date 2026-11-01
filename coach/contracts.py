"""Versioned moment JSON and evidence checks; valid structure does not prove a claim.

schema_version stays 1. Frames, events, observations and the moment use
different ID namespaces. Files that already used those shapes still validate.
A frame ID reused as an observation ID is rejected, not rewritten.
"""
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
OBS_ID = text(128, pattern=r'^obs-[A-Za-z0-9_-]+$')
FRAME_ID = text(128, pattern=r'^frame-[0-9]+$')
EVENT_ID = text(128, pattern=r'^event-[A-Za-z0-9_-]+$')
MS = dict(type='integer', minimum=0)
REFS = array(ID, 8, 1)
FRAME = obj(id=FRAME_ID, game_ms=MS, image_file=text(160, pattern=r'^frame-[0-9]+\.png$'))
EVENT = obj(id=EVENT_ID, game_ms=MS, source=choice('riot_timeline'),
            visibility=choice('global_event_not_player_view'), side=choice('ally', 'enemy', None),
            killer=text(32, True), victim=text(32, True), assists=array(text(32), 5),
            me=choice('kill', 'death', 'assist', None))
KNOWLEDGE = obj(id=ID, patch=text(64), text=text(800))
PACKET = obj(schema_version=choice(1), moment_id=ID, patch=text(64, True),
             champion=text(32, True), opponent=text(32, True), start_ms=MS, decision_ms=MS,
             sync_verified=dict(type='boolean'), focus=text(500),
             frames=array(FRAME, MAX_FRAMES), events=array(EVENT, MAX_EVENTS),
             knowledge=array(KNOWLEDGE, 4))


def observations_schema(sources, verifications):
    return obj(schema_version=choice(1), moment_id=ID,
               observations=array(obj(id=OBS_ID, game_ms=MS, statement=text(),
                                      source=choice(*sources),
                                      verification=choice(*verifications), evidence_refs=REFS), 16),
               unknowns=array(text(300), 12))


# A model may only produce unverified vision observations.
OBSERVATIONS = observations_schema(('local_vision',), ('model_observed',))
# Stored evidence may also record that the player checked a statement against the footage.
STORED_OBSERVATIONS = observations_schema(('local_vision', 'human'), ('model_observed', 'human_verified'))
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


def _reserved_evidence_prefix(value):
    return value.startswith(('frame-', 'obs-', 'event-', 'moment-'))


def _check_packet_ids(packet):
    """Frame, event, knowledge and moment IDs are one set, and each has a role.

    A frame ID is frame-{game_ms} and its file is that ID plus .png. The
    number is the labelled game clock of the frame, not a check that the
    picture shows it. Event IDs use event-. Knowledge IDs stay free-form
    except they cannot borrow those prefixes or the moment- prefix.
    """
    for frame in packet['frames']:
        expected = f"frame-{frame['game_ms']}"
        if frame['id'] != expected or frame['image_file'] != f'{expected}.png':
            raise ValueError('Frame ID, filename and game clock must match')
    for event in packet['events']:
        if re.fullmatch(r'event-[A-Za-z0-9_-]+', event['id']) is None:
            raise ValueError('Event ID must use the event- prefix')
    for rule in packet['knowledge']:
        if _reserved_evidence_prefix(rule['id']):
            raise ValueError('Knowledge ID uses a reserved evidence prefix')
    moment = packet['moment_id']
    if moment.startswith(('frame-', 'obs-', 'event-')):
        raise ValueError('Moment ID collides with the frame, observation or event namespace')
    ids = [moment]
    ids.extend(item['id'] for item in packet['frames'] + packet['events'] + packet['knowledge'])
    if len(ids) != len(set(ids)):
        raise ValueError('Duplicate or colliding evidence IDs')


def _reject_non_obs_ids(observations):
    """Say why a frame ID cannot be an observation ID before the generic pattern error."""
    items = observations.get('observations') if isinstance(observations, dict) else None
    if not isinstance(items, list):
        return
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        oid = item.get('id')
        if isinstance(oid, str) and re.fullmatch(r'obs-[A-Za-z0-9_-]+', oid) is None:
            raise ValueError(
                f'observations[{index}].id must start with obs-; a frame ID is not an observation ID')


def validate_packet(packet):
    validate(packet, PACKET)
    start, end = packet['start_ms'], packet['decision_ms']
    if not 0 <= end - start <= MAX_WINDOW_MS:
        raise ValueError('Moment must have a window of at most 60 seconds')
    _check_packet_ids(packet)
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


def validate_observations(observations, packet, model_output=False):
    """Model output must stay model_observed; stored evidence may be human_verified."""
    validate_packet(packet)
    _reject_non_obs_ids(observations)
    validate(observations, OBSERVATIONS if model_output else STORED_OBSERVATIONS)
    if observations['moment_id'] != packet['moment_id']:
        raise ValueError('Observations belong to another moment')
    frames = {f['id']: f['game_ms'] for f in packet['frames']}
    events = {e['id'] for e in packet['events']}
    rules = {r['id'] for r in packet['knowledge']}
    moment = packet['moment_id']
    ids = unique_ids(observations['observations'])
    if ids & (set(frames) | events | rules | {moment}):
        raise ValueError('Observation ID collides with a frame, event, rule or moment ID')
    for observation in observations['observations']:
        refs = observation['evidence_refs']
        if len(refs) != len(set(refs)):
            raise ValueError('Observation cites duplicate frames')
        for ref in refs:
            if ref in events or ref in rules or ref in ids or ref == moment:
                raise ValueError('Observation cites a non-frame ID')
            if ref not in frames:
                raise ValueError('Observation cites an unknown frame')
        if observation['source'] == 'human' and observation['verification'] != 'human_verified':
            raise ValueError('A human-written observation must be marked human_verified')
        times = [frames[ref] for ref in refs]
        if not min(times) <= observation['game_ms'] <= max(times):
            raise ValueError('Observation time is not supported by its cited frames')


def validate_review(review, packet, observations):
    validate_observations(observations, packet)
    validate(review, REVIEW)
    if review['moment_id'] != packet['moment_id']:
        raise ValueError('Review belongs to another moment')
    obs_ids = unique_ids(observations['observations'])
    evidence = obs_ids | unique_ids(packet['events'])
    rules = unique_ids(packet['knowledge'])
    frames = {f['id'] for f in packet['frames']}
    moment = packet['moment_id']
    cited = review['claims'] + ([review['alternative']] if review['alternative'] else [])
    for claim in cited:
        refs = claim['evidence_refs']
        if moment in refs or set(refs) & frames:
            raise ValueError('Review cites a frame or moment ID; cite an observation or event')
        if len(refs) != len(set(refs)) or not set(refs) <= evidence | rules or not set(refs) & evidence:
            raise ValueError('Claim needs valid moment evidence, not just a game rule')
        if claim.get('kind') == 'observation' and not set(refs) & obs_ids:
            raise ValueError('An observation claim must cite an observation ID')
    if review['assessment'] == 'needs_more_evidence':
        if review['alternative'] is not None or review['practice_focus'] is not None or not review['missing_evidence']:
            raise ValueError('Insufficient evidence cannot produce advice or a practice focus')
    elif not evidence:
        raise ValueError('A reviewable moment needs evidence')
    if review['practice_focus'] is not None and review['alternative'] is None:
        raise ValueError('A practice focus needs a supported alternative')
