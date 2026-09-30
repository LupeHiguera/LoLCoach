"""Score dense, labelled action timelines; this does not infer events from video."""
import argparse
import statistics
from pathlib import Path

from coach import contracts, harness

EVENT = contracts.obj(label=contracts.ID, actor=contracts.ID, game_ms=contracts.MS)
TIMELINE = contracts.obj(schema_version=contracts.choice(1), moment_id=contracts.ID, start_ms=contracts.MS,
                         decision_ms=contracts.MS, events=contracts.array(EVENT, 10000))


def checked_timeline(value):
    contracts.validate(value, TIMELINE)
    start, end = value['start_ms'], value['decision_ms']
    if not 0 <= end - start <= contracts.MAX_WINDOW_MS:
        raise ValueError('Timeline must cover one bounded moment, at most 60 seconds')
    if any(not start <= e['game_ms'] <= end for e in value['events']):
        raise ValueError('Action falls outside the decision window')


def score_events(gold, predictions, tolerance_ms=250):
    """One-to-one exact label/actor matches within tolerance; missing actions count as false negatives."""
    checked_timeline(gold)
    checked_timeline(predictions)
    identity = ('moment_id', 'start_ms', 'decision_ms')
    if any(gold[key] != predictions[key] for key in identity):
        raise ValueError('Timeline moment IDs and windows must match')
    if type(tolerance_ms) is not int or not 0 <= tolerance_ms <= 5000:
        raise ValueError('Timestamp tolerance must be 0–5000 milliseconds')
    groups = {}
    for index, timeline in enumerate((gold, predictions)):
        for event in timeline['events']:
            groups.setdefault((event['label'], event['actor']), ([], []))[index].append(event['game_ms'])
    errors, per_label = [], {}
    for (label, _), (truth, found) in groups.items():
        truth.sort()
        found.sort()
        i, j, matched = 0, 0, 0
        # Earliest-compatible matching maximises count for these timestamp intervals.
        # Timing error uses those matches; it is not a global minimum-error assignment.
        while i < len(truth) and j < len(found):
            difference = found[j] - truth[i]
            if abs(difference) <= tolerance_ms:
                errors.append(abs(difference))
                matched += 1
                i += 1
                j += 1
            elif difference < 0:
                j += 1
            else:
                i += 1
        counts = per_label.setdefault(label, dict(tp=0, fp=0, fn=0))
        counts['tp'] += matched
        counts['fp'] += len(found) - matched
        counts['fn'] += len(truth) - matched
    tp = len(errors)
    fp, fn = len(predictions['events']) - tp, len(gold['events']) - tp
    return dict(gold_actions=len(gold['events']), predicted_actions=len(predictions['events']),
                tp=tp, fp=fp, fn=fn, tolerance_ms=tolerance_ms,
                precision=tp / (tp + fp) if tp + fp else None,
                recall=tp / (tp + fn) if tp + fn else None,
                f1=2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else None,
                mean_abs_timing_error_ms=statistics.mean(errors) if errors else None,
                per_label=per_label, note='Canonical labels and actors require human checking; no semantic LLM grading')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--gold', type=Path, required=True)
    parser.add_argument('--predictions', type=Path, required=True)
    parser.add_argument('--tolerance-ms', type=int, default=250)
    args = parser.parse_args(argv)
    try:
        harness.write_output(score_events(harness.read_json(args.gold), harness.read_json(args.predictions), args.tolerance_ms))
    except (ValueError, OSError) as exc:
        parser.exit(1, f'Event evaluation: {exc}\n')


if __name__ == '__main__':
    main()
