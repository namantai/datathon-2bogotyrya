"""Owner-run, cached-feature-only v4/v5 experiment. Never changes production paths.

One uniform replay and one fixed hard-negative trial, with v4's exact split,
seed, hyperparameters, normalization recipe and detection configuration.
No threshold search, backbone, source video IO, or independent-test inspection.
"""
from __future__ import annotations

import argparse
import csv
from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time
import uuid

import numpy as np
import torch

from ml.backbone import sha256_file
from ml.dataset import normalize_video_id
from ml.diagnose import score_video
from ml.evaluate import binary_metrics, localization, localization_at_tolerance
from ml.feature_data import VideoFeatures, dataset_fingerprint, load_features, validate_split
from ml.local_io import private_path, run_cli, write_json
from ml.temporal import BinaryHead, DetectionConfig, detect_events, load_checkpoint, save_checkpoint
from ml.train import fit_head


def validate_index(index: dict, training: dict) -> None:
    """Reject extra/development-test inputs before loading any embedding arrays."""
    identities = training.get('identities', [])
    entries = index.get('videos', [])
    if (index.get('schema_version') != 1 or len(entries) != 25 or len(identities) != 25
            or len({r['video_id'] for r in entries}) != 25
            or {r['video_id'] for r in entries} != {r['video_id'] for r in identities}
            or sum(type(r.get('label')) is int and r['label'] == 1 for r in entries) != 13
            or sum(type(r.get('label')) is int and r['label'] == 0 for r in entries) != 12
            or any(normalize_video_id(r['video_id']).removesuffix('.MOV') in {'X-2', 'X-48'} for r in entries)):
        raise ValueError('Expected exactly v4 identities: 25 unique official videos, 13 positive/12 negative; no X-2/X-48')
    split = training['split']
    if (set(split) != {'train', 'validation', 'test'} or len(split['train']) != 20
            or len(split['validation']) != 5 or split['test']):
        raise ValueError('Expected the existing v4 20/5 development split and empty test')


def validate_baseline(videos: list[VideoFeatures], model: BinaryHead, config: DetectionConfig,
                      training: dict) -> dict[str, list[VideoFeatures]]:
    if model.kind != 'logistic' or dataset_fingerprint(videos) != training.get('dataset_fingerprint'):
        raise ValueError('Cache does not match the saved logistic v4 dataset fingerprint')
    validate_split(videos, training['split'])
    identities = {r['video_id']: r for r in training['identities']}
    if any(v.identity() != identities.get(v.video_id) for v in videos):
        raise ValueError('Labels, points, groups or source hashes differ from v4')
    if any(v.metadata['sample_fps'] != config.sample_fps for v in videos):
        raise ValueError('Sampling rate differs from v4')
    selected = {name: [v for v in videos if v.video_id in training['split'][name]]
                for name in ('train', 'validation')}
    train = selected['train']
    mean = np.mean([v.embeddings.mean(axis=0) for v in train], axis=0)
    variance = np.mean([((v.embeddings - mean)**2).mean(axis=0) for v in train], axis=0)
    std = np.sqrt(variance).clip(min=1e-4)
    if (not np.allclose(model.feature_mean.numpy(), mean, atol=1e-5)
            or not np.allclose(model.feature_std.numpy(), std, atol=1e-5)):
        raise ValueError('Baseline normalization does not match the train-only recipe; investigate before training')
    return selected


def evaluate_partition(model: BinaryHead, videos: list[VideoFeatures], config: DetectionConfig) -> dict:
    rows, errors, limited_errors = [], [], []
    matched = missed = extra = unmatched_refs = unmatched_dets = 0
    for video in videos:
        row = score_video(model, video, config, order_checks=False)
        bounds = np.asarray(row['bounds_sec'])
        events = detect_events(np.asarray(row['window_scores']), bounds, config)
        stamps = []
        for event in events:
            start, end = bounds[event['window_index']]
            eligible = np.flatnonzero((video.times >= start) & (video.times <= end))
            # Match inference's stable nearest-PTS tie handling exactly.
            nearest = eligible[np.argsort(np.abs(video.times[eligible] - event['timecode_sec']), kind='stable')[0]]
            stamps.append(float(video.times[nearest]))
        match = localization_at_tolerance(video.points, stamps, 3)
        unconstrained = localization(video.points, stamps)
        errors.extend(unconstrained['absolute_errors_sec'])
        limited_errors.extend(match['absolute_errors_sec'])
        matched += match['matched_events']; missed += match['missed_reference_events']
        extra += match['false_positive_events']
        unmatched_refs += unconstrained['unmatched_references']; unmatched_dets += unconstrained['unmatched_detections']
        row.update(detections_sec=stamps, reference_points_sec=video.points,
                   localization_at_3_sec=match, localization_unconstrained=unconstrained)
        rows.append(row)
    metrics = binary_metrics([r['label'] for r in rows], [r['prediction'] for r in rows])
    ((tn, fp), (fn, tp)) = metrics['confusion_matrix']
    metrics.update(TP=tp, TN=tn, FP=fp, FN=fn)
    ratio = lambda a, b: a/b if b else None
    return {'metrics': metrics, 'videos': rows,
        'window_counts': {key: sum(r['window_counts'][key] for r in rows) for key in ('positive', 'negative', 'ignored')},
        'localization': {'tolerance_sec': 3, 'matched_events': matched, 'missed_reference_events': missed,
            'extra_predicted_events': extra, 'precision': ratio(matched, matched+extra),
            'recall': ratio(matched, matched+missed), 'f1': ratio(2*matched, 2*matched+missed+extra),
            'matched_only_mae_within_tolerance_sec': float(np.mean(limited_errors)) if limited_errors else None,
            'unconstrained_matching': 'greedy_one_to_one_no_tolerance',
            'unconstrained_matched_only_mae_sec': float(np.mean(errors)) if errors else None,
            'unconstrained_unmatched_references': unmatched_refs, 'unconstrained_unmatched_detections': unmatched_dets}}


def comparison(models: dict) -> dict:
    """Predeclared conservative gate, never automatic promotion or test selection."""
    baseline, candidate = models['v4'], models['v5_hard_negative']
    nonregression = []
    strict = []
    for name in ('train', 'validation'):
        a, b = baseline[name], candidate[name]
        nonregression += [b['metrics'][k] <= a['metrics'][k] for k in ('FP', 'FN')]
        nonregression += [b['localization'][k] <= a['localization'][k]
                          for k in ('missed_reference_events', 'extra_predicted_events')]
        for metric in ('matched_only_mae_within_tolerance_sec', 'unconstrained_matched_only_mae_sec'):
            before, after = a['localization'].get(metric), b['localization'].get(metric)
            if before is not None:
                nonregression.append(after is not None and after <= before + 1e-6)
        # Require an improvement on validation, not merely fitting training errors.
        if name == 'validation':
            strict += [b['metrics'][k] < a['metrics'][k] for k in ('FP', 'FN')]
            strict += [b['localization'][k] < a['localization'][k]
                       for k in ('missed_reference_events', 'extra_predicted_events')]
    return {'production_change_performed': False,
        'recommendation': 'review_v5_for_controlled_local_demo_only' if all(nonregression) and any(strict) else 'keep_v4',
        'gate': 'no_increase_train_or_validation_FP_FN_localization_missed_extra_MAE; strict_validation_improvement',
        'independent_improvement_demonstrated': False,
        'note': 'Validation has 5 development videos. This gate does not establish generalization.'}


def run_experiment(*, features: Path, baseline: Path, output: Path, private_root: Path) -> dict:
    features, baseline, output = (private_path(p, private_root) for p in (features, baseline, output))
    if output.exists():
        raise FileExistsError('Experiment output already exists; select a fresh directory')
    if (output == features or output.is_relative_to(features) or features.is_relative_to(output)
            or output == baseline.parent or output.is_relative_to(baseline.parent)
            or baseline.is_relative_to(output)):
        raise ValueError('Output overlaps baseline or feature inputs')
    original_sha = sha256_file(baseline)
    reference, config, training = load_checkpoint(baseline)
    reference.requires_grad_(False)
    index = json.loads(private_path(features / 'index.json', features).read_text())
    validate_index(index, training)
    videos = load_features(features, private_root)
    selected = validate_baseline(videos, reference, config, training)
    # Pin exact settings instead of quietly choosing fresh defaults or a new split.
    settings = {key: training[key] for key in ('batch_size', 'learning_rate', 'patience', 'seed')}
    settings.update(epochs=training['epochs_requested'], kind='logistic', device='cpu')
    report = {'schema_version': 1, 'created_at_utc': datetime.now(timezone.utc).isoformat(),
        'status': 'running', 'source_videos_opened': False, 'backbone_loaded': False,
        'independent_videos_used': False, 'threshold_search_performed': False,
        'production_modified': False, 'validation_is_development_only': True,
        'versions': {'python': sys.version, 'torch': str(torch.__version__), 'numpy': np.__version__},
        'source_sha256': {name: sha256_file(Path(__file__).parent / name) for name in
            ('experiment_logistic_v5.py', 'train.py', 'temporal.py', 'feature_data.py', 'diagnose.py', 'evaluate.py')},
        'baseline_sha256': original_sha, 'dataset_fingerprint': training['dataset_fingerprint'],
        'split': training['split'], 'config': asdict(config), 'fit_settings': settings,
        'candidate': {'negative_top_fraction': .25, 'relative_weight_factor': 3.0,
                      'score_source': 'frozen_v4_train_negatives_only'}, 'models': {}}
    # All feature/model compatibility checks run before training or creating output.
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    started = time.perf_counter()
    report['models']['v4'] = {name: evaluate_partition(reference, subset, config) for name, subset in selected.items()}
    for name, hard in (('uniform_replay', False), ('v5_hard_negative', True)):
        model, fitted = fit_head(selected['train'], selected['validation'], config, **settings,
                                negative_reference=reference if hard else None)
        fitted.update(split=training['split'], identities=training['identities'],
            dataset_fingerprint=training['dataset_fingerprint'], detection_config=asdict(config),
            experiment_name=name, baseline_checkpoint_sha256=original_sha,
            normalization_recipe='train_only_equal_video', threshold_tuned=False)
        destination = output / name; destination.mkdir()
        save_checkpoint(destination / 'head.pt', model, config, fitted)
        write_json(destination / 'training.json', fitted)
        write_json(destination / 'split.json', {'split': training['split'],
                                                'dataset_fingerprint': training['dataset_fingerprint']})
        # Compare the SAVED model, as runtime will load it, not an unsaved in-memory state.
        loaded, restored_config, _ = load_checkpoint(destination / 'head.pt')
        if restored_config != config:
            raise ValueError('Saved experiment detection configuration changed')
        report['models'][name] = {partition: evaluate_partition(loaded, subset, config)
                                 for partition, subset in selected.items()}
    report['uniform_replay_max_score_difference_vs_v4'] = {
        name: max(abs(a['video_score_max_smoothed']-b['video_score_max_smoothed'])
                  for a, b in zip(report['models']['v4'][name]['videos'], report['models']['uniform_replay'][name]['videos']))
        for name in selected}
    if (sha256_file(baseline) != original_sha
            or dataset_fingerprint(load_features(features, private_root)) != training['dataset_fingerprint']):
        raise ValueError('Baseline or cached data changed during experiment; results invalid')
    report.update(status='complete', experiment_sec=time.perf_counter()-started,
                  comparison=comparison(report['models']), baseline_unchanged=True)
    faithful = all(delta < 1e-4 for delta in report['uniform_replay_max_score_difference_vs_v4'].values())
    report['comparison']['uniform_replay_reproduces_v4_within_1e_4'] = faithful
    if not faithful:
        report['comparison'].update(recommendation='keep_v4',
            replay_warning='Investigate replay differences before attributing changes to hard-negative weighting')
    write_json(output / 'comparison.json', report)
    columns = ['model', 'partition', 'accuracy', 'precision', 'recall', 'f1', 'TP', 'TN', 'FP', 'FN',
               'unconstrained_matched_only_mae_sec', 'matched_only_mae_within_tolerance_sec',
               'matched_events', 'missed_reference_events', 'extra_predicted_events']
    with (output / 'summary.csv').open('x', newline='', encoding='utf-8-sig') as handle:
        writer = csv.DictWriter(handle, columns)
        writer.writeheader()
        for name, partitions in report['models'].items():
            for partition, evaluated in partitions.items():
                writer.writerow({'model': name, 'partition': partition,
                    **{k: v for k, v in evaluated['metrics'].items() if k in columns},
                    **{k: v for k, v in evaluated['localization'].items() if k in columns}})
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--private-root', type=Path, default=Path('private_data'))
    parser.add_argument('--features', type=Path, required=True, help='EXACT cache used by v4: 25 official unique videos')
    parser.add_argument('--baseline', type=Path, default=Path('private_data/checkpoints/logistic-v4/head.pt'))
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    os.environ.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', HF_HUB_DISABLE_TELEMETRY='1')
    output = args.output or args.private_root / 'experiments/logistic-v5' / (
        datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8])
    run_experiment(features=args.features, baseline=args.baseline, output=output, private_root=args.private_root)
    print('Local v4/v5 comparison saved. Production remains v4. Keep comparison.json and checkpoints local.')


if __name__ == '__main__':
    run_cli(main)
