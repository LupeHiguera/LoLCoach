"""Model coaching for the Watch view: prepared moment bundles placed on one match.

A bundle is a `coach.harness prepare` output folder (packet.json, plus observations.json
and review.json once the models have run). Packets carry no match ID, so a bundle is
placed on a match only when its opaque moment ID re-hashes from that match's own death
(see `coach.harness.make_packet`). The coaching shown is model output: validated for
structure and citations, not checked for truth.
"""
import hashlib
import json
import os
import threading
from contextlib import closing
from pathlib import Path

from coach import contracts
from review_data import connect

WRITE_LOCK = threading.Lock()
MAX_BUNDLES = 500
MAX_FILE_BYTES = 2 << 20
VERIFICATION = {'model_observed': 'Model observation, unchecked', 'human_verified': 'Checked by you'}


def read_json(path):
    """Small local JSON file, or None when it does not exist."""
    if not path.is_file():
        return None
    with path.open('rb') as handle:
        data = handle.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise ValueError(f'{path.name} is larger than 2 MiB')
    return json.loads(data)


def opaque_id(match_id, clip_moment, start_ms, decision_ms):
    """The packet moment ID `make_packet` derives; a match is never stored in the packet."""
    digest = hashlib.sha256(f'{match_id}:{clip_moment}:{start_ms}:{decision_ms}'.encode()).hexdigest()[:16]
    return f'moment-{digest}'


def my_deaths(conn, match_id):
    return [r[0] for r in conn.execute("""SELECT e.timestamp_ms FROM events e JOIN matches m
        USING(match_id) WHERE match_id=? AND e.type='CHAMPION_KILL'
        AND e.victim_id=m.my_participant_id ORDER BY e.timestamp_ms""", (match_id,))]


def placed_death(packet, match_id, deaths):
    """The death this packet was prepared for, or None if it belongs to another match."""
    if not isinstance(packet, dict):
        return None
    start, decision = packet.get('start_ms'), packet.get('decision_ms')
    if type(start) is not int or type(decision) is not int:
        return None
    for death in deaths:
        if death > decision and packet.get('moment_id') == opaque_id(match_id, f'death-{death}', start, decision):
            return death
    return None


def evidence_index(packet, observations):
    """Citable ID -> {id, game_ms, source, text} for every item a review may cite."""
    index = {}
    for o in observations['observations']:
        index[o['id']] = dict(id=o['id'], game_ms=o['game_ms'], source=VERIFICATION[o['verification']],
                              text=o['statement'])
    for e in packet['events']:
        who = f"{e['killer'] or 'Executed'} killed {e['victim'] or '?'}"
        index[e['id']] = dict(id=e['id'], game_ms=e['game_ms'], source='Riot event', text=who + '.')
    for f in contracts.state_facts(packet):
        index[f['id']] = dict(id=f['id'], game_ms=f['game_ms'], source='Riot timeline', text=f['statement'])
    for k in packet['knowledge']:
        index[k['id']] = dict(id=k['id'], game_ms=None, source='Selected knowledge', text=k['text'])
    return index


def cited(item, index):
    return dict(item, evidence=[index[ref] for ref in item['evidence_refs']])


def bundle_view(folder, packet, death_ms):
    """One moment for display. `problem` explains a missing or invalid stage."""
    view = dict(bundle=folder.name, death_ms=death_ms, decision_ms=packet['decision_ms'],
                start_ms=packet['start_ms'], focus=packet['focus'], sync_verified=packet['sync_verified'],
                frames=len(packet['frames']), state=packet.get('state') is not None,
                observations=[], unknowns=[], review=None, problem=None)
    try:
        contracts.validate_packet(packet)
        observations = read_json(folder / 'observations.json')
        if observations is None:
            view['problem'] = 'No observations yet. Run coach.harness observe on this bundle.'
            return view
        contracts.validate_observations(observations, packet)
        view['observations'] = [dict(id=o['id'], game_ms=o['game_ms'], text=o['statement'],
                                     source=VERIFICATION[o['verification']])
                                for o in observations['observations']]
        view['unknowns'] = observations['unknowns']
        review = read_json(folder / 'review.json')
        if review is None:
            view['problem'] = 'No review yet. Run coach.harness review on this bundle.'
            return view
        contracts.validate_review(review, packet, observations)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        view['problem'] = f'Bundle failed validation: {exc}'
        return view
    index = evidence_index(packet, observations)
    view['review'] = dict(
        assessment=review['assessment'],
        claims=[cited(c, index) for c in review['claims']],
        alternative=cited(review['alternative'], index) if review['alternative'] else None,
        practice_focus=review['practice_focus'], missing_evidence=review['missing_evidence'])
    return view


def coaching(matches_path, moments_dir, match_id):
    """{moments, scanned} for one match; bundles from other matches are skipped silently."""
    with closing(connect(matches_path, True)) as conn:
        if conn.execute("SELECT 1 FROM matches WHERE match_id=?", (match_id,)).fetchone() is None:
            raise KeyError('Match not found')
        deaths = my_deaths(conn, match_id)
    root = Path(moments_dir)
    folders = sorted(p for p in root.iterdir() if p.is_dir())[:MAX_BUNDLES] if root.is_dir() else []
    moments = []
    for folder in folders:
        try:
            packet = read_json(folder / 'packet.json')
        except (ValueError, OSError):
            continue  # unreadable bundles cannot be placed on any match
        death = placed_death(packet, match_id, deaths)
        if death is not None:
            moments.append(bundle_view(folder, packet, death))
    moments.sort(key=lambda m: (m['decision_ms'], m['bundle']))
    return dict(moments=moments, scanned=len(folders))


def bundle_folder(moments_dir, bundle):
    """A direct child folder of moments_dir, by name; never a path that escapes it."""
    root = Path(moments_dir).resolve()
    if not isinstance(bundle, str) or bundle in ('', '.', '..') or '/' in bundle or '\\' in bundle:
        raise ValueError('Unknown moment bundle')
    folder = (root / bundle).resolve()
    if folder.parent != root or not folder.is_dir():
        raise ValueError('Unknown moment bundle')
    return folder


def set_checked(matches_path, moments_dir, match_id, bundle, observation_id, checked):
    """Record that the player checked one observation against the footage, or undo it.

    Rewrites observations.json with verification human_verified (or model_observed).
    The statement itself is unchanged; a check says it matched the video, nothing more.
    """
    if type(checked) is not bool:
        raise ValueError('checked must be true or false')
    folder = bundle_folder(moments_dir, bundle)
    with closing(connect(matches_path, True)) as conn:
        if conn.execute("SELECT 1 FROM matches WHERE match_id=?", (match_id,)).fetchone() is None:
            raise KeyError('Match not found')
        deaths = my_deaths(conn, match_id)
    with WRITE_LOCK:
        packet = read_json(folder / 'packet.json')
        death = placed_death(packet, match_id, deaths)
        if death is None:
            raise ValueError('This moment bundle belongs to another game')
        path = folder / 'observations.json'
        observations = read_json(path)
        if observations is None:
            raise ValueError('This bundle has no observations yet')
        contracts.validate_observations(observations, packet)
        target = next((o for o in observations['observations'] if o['id'] == observation_id), None)
        if target is None:
            raise ValueError('Unknown observation')
        if target['source'] == 'human':
            raise ValueError('Observations you wrote are always checked')
        target['verification'] = 'human_verified' if checked else 'model_observed'
        contracts.validate_observations(observations, packet)
        temp = path.with_name('.observations.json.tmp')
        temp.write_text(json.dumps(observations, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
        os.replace(temp, path)  # never a half-written file
    return bundle_view(folder, packet, death)
