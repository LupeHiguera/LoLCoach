"""Portable moment benchmarks: python -m coach.eval --help. Labels never enter model input."""
import argparse
import json
import math
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

from coach import cloud, contracts, harness

CASE = contracts.obj(id=contracts.ID, group=contracts.ID,
                     split=contracts.choice('dev', 'test'), packet=contracts.text(300),
                     observations=contracts.text(300),
                     expected_assessment=contracts.choice('reviewable', 'needs_more_evidence', None),
                     cloud_approved_sha256=contracts.text(64, True, r'^[a-f0-9]{64}$'))
DATASET = contracts.obj(schema_version=contracts.choice(1), id=contracts.ID,
                        synthetic=dict(type='boolean'), cases=contracts.array(CASE, 2000, 1))
RUBRIC = ('factual_support', 'positioning_reasoning', 'action_feasibility',
          'uncertainty', 'practice_usefulness')
VISION_RUBRIC = ('visible_fact_accuracy', 'timestamp_accuracy', 'action_coverage',
                 'uncertainty', 'evidence_use')
DEFAULT_OUTPUT_TOKENS = dict(local=harness.MAX_OUTPUT_TOKENS, openai=4096)
Z_95 = 1.96


def wilson_95(successes, trials):
    """95% Wilson score interval for a rate; with few cases it is wide on purpose."""
    if not trials:
        return None
    rate, spread = successes / trials, Z_95 ** 2 / trials
    centre = (rate + spread / 2) / (1 + spread)
    half = Z_95 * math.sqrt(rate * (1 - rate) / trials + spread / (4 * trials)) / (1 + spread)
    return [max(0.0, centre - half), min(1.0, centre + half)]


def dataset_fingerprint(cases):
    # File locations and privacy approvals are bookkeeping, not model input or gold labels.
    return cloud.fingerprint([dict(id=c['spec']['id'], group=c['spec']['group'], split=c['spec']['split'],
                                  expected_assessment=c['spec']['expected_assessment'],
                                  packet=c['packet'], observations=c['observations']) for c in cases])


def within(root, name):
    """Dataset paths are relative and portable; symlinks cannot escape the dataset directory."""
    if '\\' in name or ':' in name or Path(name).is_absolute():
        raise ValueError('Dataset files must use relative paths with forward slashes')
    target = (root / name).resolve()
    if not target.is_relative_to(root.resolve()):
        raise ValueError('Dataset file escapes its directory')
    return target


def load_dataset(path):
    data = harness.read_json(path)
    contracts.validate(data, DATASET)
    contracts.unique_ids(data['cases'])
    groups, cases = {}, []
    for item in data['cases']:
        previous = groups.setdefault(item['group'], item['split'])
        if previous != item['split']:
            raise ValueError('One game/group cannot appear in both dev and test')
        packet = harness.read_json(within(path.parent, item['packet']))
        observations = harness.read_json(within(path.parent, item['observations']))
        contracts.validate_observations(observations, packet)
        cases.append(dict(spec=item, packet=packet, observations=observations))
    return data, cases


def selected_cases(cases, split, limit):
    selected = [case for case in cases if split == 'all' or case['spec']['split'] == split]
    if limit is not None:
        if type(limit) is not int or limit < 1:
            raise ValueError('Limit must be positive')
        selected = selected[:limit]
    if not selected:
        raise ValueError('No cases selected')
    return selected


def request_for(case, provider, model, task, max_output_tokens, reasoning_effort=None):
    if task == 'observe':
        if provider != 'local':
            raise ValueError('Vision benchmarks are local-only; never send images to OpenAI')
        return harness.build_request(task, case['packet'], model, root=case['root'],
                                     max_tokens=max_output_tokens, reasoning_effort=reasoning_effort)
    context = cloud.checked_text(case['packet'], case['observations'])
    if provider == 'openai':
        if model != cloud.MODEL:
            raise ValueError('Cloud benchmark supports only the explicitly selected GPT-6.1 Sol')
        if case['spec']['cloud_approved_sha256'] != cloud.fingerprint(context):
            raise ValueError('Cloud text needs human privacy review; use preview-cloud and approve its exact hash')
        return cloud.make_request(context, max_output_tokens)
    payload = harness.build_request(task, case['packet'], model, case['observations'],
                                    max_tokens=max_output_tokens, reasoning_effort=reasoning_effort)
    payload['messages'][1]['content'][0]['text'] = json.dumps(context, ensure_ascii=False)
    return payload


def result_path(run_dir, case_id):
    return run_dir / (cloud.fingerprint(case_id)[:24] + '.json')


def save_json(path, value):
    """Replace a local ledger atomically so a crash does not erase the previous reservation."""
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.unlink(missing_ok=True)  # left by a crash; the run lock guarantees one writer
    with temporary.open('x', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write('\n')
    temporary.replace(path)


def validate_result(result, task, case):
    if task == 'observe':
        contracts.validate_observations(result, case['packet'], model_output=True)
    else:
        contracts.validate_review(result, case['packet'], case['observations'])


def run_dataset(path, run_dir, provider, model, base_url=harness.DEFAULT_BASE_URL,
                split='test', limit=None, task='review', budget_usd=None,
                max_output_tokens=None, runtime_info=None, timeout_s=harness.MODEL_TIMEOUT_S,
                reasoning_effort=None):
    """Save every attempted case; resuming never repeats a paid, failed or uncertain request."""
    if provider not in ('local', 'openai') or task not in ('review', 'observe'):
        raise ValueError('Unsupported provider or task')
    if provider == 'local':
        harness.local_endpoint(base_url)
    elif task != 'review':
        raise ValueError('OpenAI benchmarks accept text-only reviews')
    if provider == 'openai' and reasoning_effort is not None:
        raise ValueError('The OpenAI reference always uses low reasoning effort')
    if not isinstance(timeout_s, (int, float)) or not 1 <= timeout_s <= 3600:
        raise ValueError('Timeout must be 1..3600 seconds')
    if max_output_tokens is None:
        max_output_tokens = DEFAULT_OUTPUT_TOKENS[provider]
    if provider == 'openai' and (budget_usd is None or not math.isfinite(budget_usd) or budget_usd <= 0):
        raise ValueError('A paid run requires an explicit positive --budget-usd')
    data, all_cases = load_dataset(path)
    cases = selected_cases(all_cases, split, limit)
    for case in cases:
        case['root'] = within(path.parent, case['spec']['packet']).parent
    info = runtime_info or {}
    if not isinstance(info, dict) or len(json.dumps(info)) > 8000:
        raise ValueError('Runtime info must be a JSON object of at most 8000 characters')
    config = dict(schema_version=1, dataset_id=data['id'], dataset_sha256=dataset_fingerprint(cases),
                  synthetic=data['synthetic'], task=task, provider=provider, model=model,
                  endpoint=base_url if provider == 'local' else cloud.ENDPOINT,
                  split=split, limit=limit, budget_usd=budget_usd, max_output_tokens=max_output_tokens,
                  runtime_info=info, pricing=cloud.PRICING if provider == 'openai' else None,
                  timeout_s=timeout_s, reasoning_effort=reasoning_effort)
    # Preflight every case before any network request or ledger mutation.
    requests = [request_for(case, provider, model, task, max_output_tokens, reasoning_effort) for case in cases]
    config['requests_sha256'] = cloud.fingerprint(requests)
    key = None
    run_dir.mkdir(parents=True, exist_ok=True)
    lock = run_dir / '.lock'
    try:
        with lock.open('x', encoding='utf-8'):
            pass
    except FileExistsError:
        raise ValueError(f'{lock} exists: another run is writing here, or a previous run was '
                         'interrupted. Inspect pending cases, then delete the lock by hand') from None
    try:
        manifest = run_dir / 'run.json'
        if manifest.exists():
            if harness.read_json(manifest)['config'] != config:
                raise ValueError('Run configuration or evidence changed; choose another output directory')
        else:
            if any(p != lock for p in run_dir.iterdir()):
                raise ValueError('New run directory must be empty')
            save_json(manifest, dict(config=config, created_utc=datetime.now(timezone.utc).isoformat()))
        reserved = 0.0
        for case in cases:
            output = result_path(run_dir, case['spec']['id'])
            if output.exists():
                previous = harness.read_json(output)
                reserved += previous['reserved_usd']
        for case, request in zip(cases, requests):
            output = result_path(run_dir, case['spec']['id'])
            if output.exists():
                continue
            allocation = cloud.reserve_usd(request) if provider == 'openai' else 0.0
            if budget_usd is not None and provider == 'openai' and reserved + allocation > budget_usd:
                break
            if provider == 'openai' and key is None:
                key = cloud.load_key()
            row = dict(schema_version=1, case_id=case['spec']['id'], group=case['spec']['group'],
                       request_sha256=cloud.fingerprint(request), status='pending',
                       reserved_usd=allocation, estimated_cost_usd=None, latency_s=None,
                       usage=None, served_model=None, result=None, error=None)
            save_json(output, row)  # reserve before transmitting, including unknown billing on failure
            reserved += allocation
            start = time.perf_counter()
            try:
                if provider == 'local':
                    response = harness.complete(base_url, request, with_metadata=True, timeout_s=timeout_s)
                    row['usage'], row['served_model'] = response['usage'], response['served_model']
                    result = response['result']
                else:
                    response = cloud.complete(request, key)
                    row['usage'], row['served_model'] = response.get('usage'), response.get('model')
                    row['estimated_cost_usd'] = cloud.cost_usd(row['usage'])
                    result = cloud.parse_result(response)
                validate_result(result, task, case)
                row.update(status='ok', result=result)
            except (ValueError, OSError, KeyError, TypeError, IndexError):
                # Model-supplied errors can contain private text; store no exception/server body.
                row.update(status='failed', error='Request failed or output violated the contract; inspect runtime locally')
            row['latency_s'] = time.perf_counter() - start
            save_json(output, row)
            print(f"{case['spec']['id']}: {row['status']} ({row['latency_s']:.2f}s)")
            if provider == 'openai' and row['status'] != 'ok':
                break  # no repeated costs from bad schemas, access failures or an unreliable connection
        summary = report(path, run_dir)
        save_json(run_dir / 'report.json', summary)
        return summary
    finally:
        lock.unlink()


def report(path, run_dir):
    """Structural/assessment rates are screening checks, never a measure of coaching truth."""
    data, cases = load_dataset(path)
    config = harness.read_json(run_dir / 'run.json')['config']
    cases = selected_cases(cases, config['split'], config['limit'])
    digest = dataset_fingerprint(cases)
    if (digest != config['dataset_sha256'] or data['id'] != config['dataset_id'] or
            data['synthetic'] != config['synthetic']):
        raise ValueError('Report dataset does not match the run')
    counts = dict(ok=0, failed=0, pending=0, not_run=0)
    latencies, matched, labelled, labelled_ok, costs, reserved = [], 0, 0, 0, [], 0.0
    unknown_cost = 0
    for case in cases:
        target = result_path(run_dir, case['spec']['id'])
        if not target.exists():
            counts['not_run'] += 1
            continue
        row = harness.read_json(target)
        if row['status'] not in counts or row['case_id'] != case['spec']['id']:
            raise ValueError('Invalid saved result ledger')
        counts[row['status']] += 1
        reserved += row['reserved_usd']
        if row['estimated_cost_usd'] is not None:
            costs.append(row['estimated_cost_usd'])
        elif config['provider'] == 'openai':
            unknown_cost += 1
        if row['latency_s'] is not None:
            latencies.append(row['latency_s'])
        expected = case['spec']['expected_assessment']
        if config['task'] == 'review' and expected is not None:
            labelled += 1
            if row['status'] == 'ok':
                validate_result(row['result'], config['task'], case)
                labelled_ok += 1
                matched += row['result']['assessment'] == expected
        elif row['status'] == 'ok':
            validate_result(row['result'], config['task'], case)
    finished = counts['ok'] + counts['failed']
    return dict(dataset=data['id'], synthetic=data['synthetic'], task=config['task'], model=config['model'],
                selected_cases=len(cases), groups=len({c['spec']['group'] for c in cases}), counts=counts,
                # Rates use finished attempts only; not_run and pending cases are neither passes nor failures.
                contract_pass_rate=counts['ok'] / finished if finished else None,
                contract_pass_ci95=wilson_95(counts['ok'], finished),
                assessment_labelled_attempts=labelled, assessment_labelled_ok=labelled_ok,
                assessment_matches=matched,
                assessment_match_rate=matched / labelled_ok if labelled_ok else None,
                assessment_match_ci95=wilson_95(matched, labelled_ok),
                median_latency_s=statistics.median(latencies) if latencies else None,
                estimated_cost_usd=sum(costs) if costs else None, unknown_billing_cases=unknown_cost,
                reserved_usd=reserved, quality='Human rubric ratings required; Sol is a reference, not ground truth')


def rating_sheet(path, run_dir):
    report(path, run_dir)  # verify run/evidence before showing private review material
    _, cases = load_dataset(path)
    config = harness.read_json(run_dir / 'run.json')['config']
    rubric = VISION_RUBRIC if config['task'] == 'observe' else RUBRIC
    rows = []
    for case in selected_cases(cases, config['split'], config['limit']):
        target = result_path(run_dir, case['spec']['id'])
        if target.exists():
            result = harness.read_json(target)
            rows.append(dict(case_id=case['spec']['id'], request_sha256=result['request_sha256'],
                             status=result['status'], result=result['result'],
                             scores={key: None for key in rubric}, unsupported_claims=None, notes=''))
    return dict(schema_version=1, run_sha256=cloud.fingerprint(config), ratings=rows)


def summarize_ratings(sheet, run_dir):
    """Report human 0–2 rubric scores separately from automatic schema checks."""
    config = harness.read_json(run_dir / 'run.json')['config']
    if sheet.get('run_sha256') != cloud.fingerprint(config) or sheet.get('schema_version') != 1:
        raise ValueError('Ratings belong to a different run')
    rubric = VISION_RUBRIC if config['task'] == 'observe' else RUBRIC
    values = {key: [] for key in rubric}
    seen, unsupported, completed = set(), 0, 0
    for row in sheet['ratings']:
        case_id = row['case_id']
        if case_id in seen:
            raise ValueError('Duplicate rated case')
        seen.add(case_id)
        result = harness.read_json(result_path(run_dir, case_id))
        if row['request_sha256'] != result['request_sha256'] or row['result'] != result['result']:
            raise ValueError('Rated result differs from the saved run')
        if set(row['scores']) != set(rubric):
            raise ValueError('Rating rubric fields do not match')
        scores = list(row['scores'].values())
        if all(v is None for v in scores) and row['unsupported_claims'] is None:
            continue
        if result['status'] != 'ok' or any(type(v) is not int or v not in (0, 1, 2) for v in scores):
            raise ValueError('Only successful results with all five 0–2 scores can be rated')
        count = row['unsupported_claims']
        if type(count) is not int or count < 0:
            raise ValueError('Unsupported claim count must be a nonnegative integer')
        for key in rubric:
            values[key].append(row['scores'][key])
        unsupported += count
        completed += 1
    return dict(rated_cases=completed, mean_scores={k: statistics.mean(v) if v else None for k, v in values.items()},
                unsupported_claims=unsupported, scale='0 incorrect/absent, 1 partial, 2 sound; human judgement')


def compare(path, run_dirs):
    """Compare identical cases/tasks only; different models can legitimately choose different advice."""
    if len(run_dirs) < 2:
        raise ValueError('Comparison requires at least two runs')
    rows, common = [], None
    for directory in run_dirs:
        summary = report(path, directory)
        config = harness.read_json(directory / 'run.json')['config']
        identity = (config['dataset_sha256'], config['task'], config['split'], config['limit'])
        if common is not None and identity != common:
            raise ValueError('Comparison requires the same cases, split and task')
        common = identity
        ratings = directory / 'ratings.json'
        rows.append(dict(run=str(directory), summary=summary, runtime_info=config['runtime_info'],
                         human_ratings=summarize_ratings(harness.read_json(ratings), directory) if ratings.exists() else None))
    return dict(comparisons=rows, note='Contract checks and assessment agreement are not coaching accuracy; inspect human ratings')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    check = sub.add_parser('validate')
    check.add_argument('--dataset', type=Path, required=True)
    preview = sub.add_parser('preview-cloud', help='Print exact text for a human privacy check; no API call')
    preview.add_argument('--dataset', type=Path, required=True)
    preview.add_argument('--case', required=True)
    run = sub.add_parser('run')
    run.add_argument('--dataset', type=Path, required=True)
    run.add_argument('--output', type=Path, required=True)
    run.add_argument('--provider', choices=('local', 'openai'), default='local')
    run.add_argument('--model', required=True)
    run.add_argument('--task', choices=('review', 'observe'), default='review')
    run.add_argument('--base-url', default=harness.DEFAULT_BASE_URL)
    run.add_argument('--split', choices=('dev', 'test', 'all'), default='test')
    run.add_argument('--limit', type=int)
    run.add_argument('--budget-usd', type=float)
    run.add_argument('--max-output-tokens', type=int, help='Default: 8192 local, 4096 OpenAI')
    run.add_argument('--runtime-info', type=Path)
    run.add_argument('--timeout-s', type=float, default=harness.MODEL_TIMEOUT_S)
    run.add_argument('--reasoning-effort', choices=harness.REASONING_EFFORTS, help='Local runs only')
    for name in ('report', 'rating-sheet', 'score-ratings'):
        command = sub.add_parser(name)
        command.add_argument('--run', type=Path, required=True)
        if name != 'score-ratings':
            command.add_argument('--dataset', type=Path, required=True)
        if name == 'rating-sheet':
            command.add_argument('--output', type=Path, required=True)
        if name == 'score-ratings':
            command.add_argument('--ratings', type=Path, required=True)
    comparison = sub.add_parser('compare')
    comparison.add_argument('--dataset', type=Path, required=True)
    comparison.add_argument('--runs', type=Path, nargs='+', required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'validate':
            data, cases = load_dataset(args.dataset)
            harness.write_output(dict(dataset=data['id'], synthetic=data['synthetic'], cases=len(cases),
                                      groups=len({c['spec']['group'] for c in cases})))
        elif args.command == 'preview-cloud':
            _, cases = load_dataset(args.dataset)
            selected = [c for c in cases if c['spec']['id'] == args.case]
            if not selected:
                raise ValueError('Case not found')
            context = cloud.checked_text(selected[0]['packet'], selected[0]['observations'])
            harness.write_output(dict(case_id=args.case, cloud_approved_sha256=cloud.fingerprint(context), text=context))
        elif args.command == 'run':
            info = harness.read_json(args.runtime_info) if args.runtime_info else None
            harness.write_output(run_dataset(args.dataset, args.output, args.provider, args.model, args.base_url,
                                            args.split, args.limit, args.task, args.budget_usd,
                                            args.max_output_tokens, info, args.timeout_s,
                                            args.reasoning_effort))
        elif args.command == 'report':
            harness.write_output(report(args.dataset, args.run))
        elif args.command == 'rating-sheet':
            harness.write_output(rating_sheet(args.dataset, args.run), args.output)
        elif args.command == 'compare':
            harness.write_output(compare(args.dataset, args.runs))
        else:
            harness.write_output(summarize_ratings(harness.read_json(args.ratings), args.run))
    except (ValueError, OSError, KeyError, TypeError) as exc:
        parser.exit(1, f'Evaluation: {exc}\n')


if __name__ == '__main__':
    main()
