"""Synthetic-only v5 weighting, full experiment, and FP-mechanism regression tests."""
from copy import deepcopy
import csv
import hashlib
import json
import socket
import sys

import numpy as np
import pytest
import torch
from torch.nn import functional as F

from ml import experiment_logistic_v5 as experiment
from ml import train
from ml.feature_data import VideoFeatures, dataset_fingerprint, load_features
from ml.temporal import (BinaryHead, DetectionConfig, detect_events, load_checkpoint,
                         save_checkpoint, smooth_scores, window_labels, windows)
from test_training import write_cache


@pytest.fixture
def development_inputs(tmp_path):
    rng = np.random.default_rng(41)
    videos = []
    for i in range(25):
        label = int(i % 2 == 0)
        times = np.arange(0, 12, .5)
        x = rng.normal(0, .1, (len(times), 384)).astype(np.float32)
        x[:, 0] += times / 10  # Variable negative window difficulty for the reference.
        if label:
            x[(times >= 3) & (times <= 6), :12] += 2
        videos.append(VideoFeatures(f'synthetic-{i}', label, [4.5] if label else [],
            f'scenario-{i}', hashlib.sha256(f'source-{i}'.encode()).hexdigest(), x, times, {'sample_fps': 2.}))
    cache = tmp_path / 'features'; write_cache(cache, videos)
    videos = load_features(cache, tmp_path)
    split = {'train': [v.video_id for v in videos[:20]], 'validation': [v.video_id for v in videos[20:]], 'test': []}
    config = DetectionConfig()
    model, fitted = train.fit_head(videos[:20], videos[20:], config, epochs=4, batch_size=32,
                                   patience=3, learning_rate=.01)
    fitted.update(split=split, dataset_fingerprint=dataset_fingerprint(videos),
                  identities=[v.identity() for v in videos])
    path = tmp_path / 'checkpoints/logistic-v4/head.pt'; path.parent.mkdir(parents=True)
    save_checkpoint(path, model, config, fitted)
    return cache, path, videos, model, config, fitted


def test_max_aggregation_can_false_positive_despite_low_average_window_score():
    config = DetectionConfig(window_sec=1, stride_sec=1)
    scores = np.full(101, .01)
    scores[50:53] = .6
    bounds = np.array([[i, i+1] for i in range(len(scores))])
    assert scores.mean() < .03
    assert smooth_scores(scores, config).max() >= config.threshold
    assert len(detect_events(scores, bounds, config)) == 1
    # A lower all-negative WINDOW loss need not mean fewer VIDEO-level FP.
    clean = np.full(len(scores), .2)
    bce = lambda values: float(F.binary_cross_entropy(torch.tensor(values), torch.zeros(len(values), dtype=torch.float64)))
    assert bce(scores) < bce(clean)
    assert detect_events(clean, bounds, config) == []


def test_hard_weighting_preserves_supervision_video_class_mass_and_reference(development_inputs):
    _, _, videos, _, config, _ = development_inputs
    reference = BinaryHead('logistic')
    with torch.no_grad():
        reference.net.weight.zero_(); reference.net.weight[0, 0] = 1; reference.net.bias.zero_()
    before = {k: v.clone() for k, v in reference.state_dict().items()}
    for v in videos[:20]:
        v.brightness_embeddings = v.embeddings + .05
    x, y, uniform = train.training_arrays(videos[:20], config, augment=True)
    hx, hy, weights = train.training_arrays(videos[:20], config, augment=True, negative_reference=reference)
    np.testing.assert_array_equal(x, hx); np.testing.assert_array_equal(y, hy)
    assert weights.mean() == pytest.approx(1)
    assert weights[y == 0].sum() == pytest.approx(weights[y == 1].sum())
    offset = 0
    for video in videos[:20]:
        _, bounds = windows(video.embeddings, video.times, config)
        count = 2 * int((window_labels(bounds, video.label, video.points) >= 0).sum())
        selected = weights[offset:offset+count]
        assert selected.sum() == pytest.approx(uniform[offset:offset+count].sum())
        if video.label == 0:
            assert selected.max() / selected.min() == pytest.approx(3)
            assert (selected == selected.max()).sum() == int(np.ceil(count*.25))
        else:
            np.testing.assert_allclose(selected, uniform[offset:offset+count])
        offset += count
    assert offset == len(x)
    assert all(torch.equal(v, before[k]) for k, v in reference.state_dict().items())
    assert all(p.grad is None for p in reference.parameters())


def test_reference_is_never_scored_on_validation_or_positive_windows(development_inputs, monkeypatch):
    _, _, videos, reference, config, _ = development_inputs
    arrays, scoring = train.training_arrays, train.score_windows
    mined_ids, scoring_calls = [], []
    def checked_arrays(subset, *args, **kwargs):
        if kwargs.get('negative_reference') is not None:
            assert {v.video_id for v in subset} == {v.video_id for v in videos[:20]}
            mined_ids.extend(v.video_id for v in subset)
        return arrays(subset, *args, **kwargs)
    def checked_scoring(model, sequences, *args, **kwargs):
        scoring_calls.append(1)
        return scoring(model, sequences, *args, **kwargs)
    monkeypatch.setattr(train, 'training_arrays', checked_arrays)
    monkeypatch.setattr(train, 'score_windows', checked_scoring)
    model, fitted = train.fit_head(videos[:20], videos[20:], config, epochs=2, negative_reference=reference)
    assert fitted['gradient_seen'] and fitted['weights_changed']
    assert len(mined_ids) == 20 and len(scoring_calls) == 10  # One call per TRAIN negative only.
    assert fitted['negative_window_weighting']['per_video_and_class_mass_preserved']
    torch.testing.assert_close(model.feature_mean, reference.feature_mean)
    torch.testing.assert_close(model.feature_std, reference.feature_std)


def test_complete_saved_experiment_is_reproducible_read_only_and_offline(tmp_path, development_inputs, monkeypatch):
    features, baseline, videos, _, config, fitted = development_inputs
    before = {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    import av
    import ml.backbone
    def forbidden(*args, **kwargs):
        raise AssertionError('No source video, network or DINOv2 in this experiment')
    monkeypatch.setattr(av, 'open', forbidden)
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(ml.backbone, 'DinoV2Encoder', forbidden)
    output = tmp_path / 'experiments/first'
    report = experiment.run_experiment(features=features, baseline=baseline, output=output, private_root=tmp_path)
    assert report['status'] == 'complete' and report['baseline_unchanged']
    assert not report['production_modified'] and not report['independent_videos_used']
    assert not report['threshold_search_performed']
    assert report['split'] == fitted['split'] and report['config'] == vars(config)
    assert set(report['models']) == {'v4', 'uniform_replay', 'v5_hard_negative'}
    assert all(value < 1e-6 for value in report['uniform_replay_max_score_difference_vs_v4'].values())
    assert {r['video_id'] for r in report['models']['v5_hard_negative']['validation']['videos']} == set(fitted['split']['validation'])
    for name in ('uniform_replay', 'v5_hard_negative'):
        saved, restored, metadata = load_checkpoint(output / name / 'head.pt')
        assert restored == config and metadata['split'] == fitted['split']
        assert saved.kind == 'logistic' and metadata['gradient_seen']
        for partition, count in (('train', 20), ('validation', 5)):
            evaluated = report['models'][name][partition]
            assert evaluated['metrics']['videos'] == count
            assert sum(evaluated['metrics'][k] for k in ('TP', 'TN', 'FP', 'FN')) == count
            assert evaluated['localization']['tolerance_sec'] == 3
    again = experiment.run_experiment(features=features, baseline=baseline,
                                      output=tmp_path / 'experiments/second', private_root=tmp_path)
    assert again['models'] == report['models']
    assert all(p.read_bytes() == value for p, value in before.items())
    with pytest.raises(FileExistsError):
        experiment.run_experiment(features=features, baseline=baseline, output=output, private_root=tmp_path)


@pytest.mark.parametrize('change', ['extra_video', 'missing_video', 'x2', 'x48', 'changed_label', 'split', 'fingerprint', 'normalization'])
def test_incompatible_data_blocks_before_any_training(tmp_path, development_inputs, monkeypatch, change):
    features, baseline, videos, model, config, fitted = development_inputs
    index_path = features / 'index.json'; index = json.loads(index_path.read_text())
    if change == 'extra_video': index['videos'].append(deepcopy(index['videos'][0]))
    if change == 'missing_video': index['videos'].pop()
    if change in ('x2', 'x48'):
        value = 'Х-2.MOV' if change == 'x2' else 'х - 48 .mov'
        index['videos'][0]['video_id'] = value; fitted['identities'][0]['video_id'] = value
    if change == 'changed_label': index['videos'][0]['label'] = 0
    if change == 'split': fitted['split']['test'] = [fitted['split']['validation'].pop()]
    if change == 'fingerprint': fitted['dataset_fingerprint'] = 'changed'
    if change == 'normalization': model.feature_mean.add_(1)
    index_path.write_text(json.dumps(index))
    # Replace ONLY the synthetic fixture checkpoint to simulate incompatible inputs.
    baseline.unlink(); save_checkpoint(baseline, model, config, fitted)
    def forbidden(*args, **kwargs):
        raise AssertionError('Preflight should block before training')
    monkeypatch.setattr(experiment, 'fit_head', forbidden)
    output = tmp_path / 'blocked'
    with pytest.raises(ValueError):
        experiment.run_experiment(features=features, baseline=baseline, output=output, private_root=tmp_path)
    assert not output.exists()


def test_classification_match_does_not_hide_localization_miss(development_inputs):
    _, _, videos, _, config, _ = development_inputs
    video = deepcopy(videos[0]); video.points = [10]
    video.embeddings[:] = 0
    model = BinaryHead('logistic')
    with torch.no_grad():
        model.net.weight.zero_(); model.net.bias.fill_(10)
    report = experiment.evaluate_partition(model, [video], config)
    assert report['metrics']['TP'] == 1
    assert report['localization']['matched_events'] == 0
    assert report['localization']['missed_reference_events'] == report['localization']['extra_predicted_events'] == 1
    assert report['localization']['unconstrained_matched_only_mae_sec'] == 8


@pytest.mark.parametrize('fp,fn,recommendation', [(0, 0, 'review_v5_for_controlled_local_demo_only'),
    (1, 0, 'keep_v4'), (0, 2, 'keep_v4'), (0, 1, 'keep_v4')])
def test_recommendation_considers_both_fp_and_fn(fp, fn, recommendation):
    def partition(false_pos, false_neg):
        return {'metrics': {'FP': false_pos, 'FN': false_neg},
                'localization': {'missed_reference_events': 1, 'extra_predicted_events': 1}}
    models = {'v4': {'train': partition(4, 0), 'validation': partition(0, 1)},
              'v5_hard_negative': {'train': partition(2, 0), 'validation': partition(fp, fn)}}
    assert experiment.comparison(models)['recommendation'] == recommendation


def test_recommendation_does_not_hide_worse_localization_error():
    def partition(fp, fn, mae):
        return {'metrics': {'FP': fp, 'FN': fn}, 'localization': {
            'missed_reference_events': 1, 'extra_predicted_events': 1,
            'matched_only_mae_within_tolerance_sec': mae, 'unconstrained_matched_only_mae_sec': mae}}
    models = {'v4': {'train': partition(4, 0, 1), 'validation': partition(0, 1, 1)},
        'v5_hard_negative': {'train': partition(2, 0, 1), 'validation': partition(0, 0, 2)}}
    assert experiment.comparison(models)['recommendation'] == 'keep_v4'


def test_owner_cli_writes_comparison_and_six_summary_rows(tmp_path, development_inputs, monkeypatch, capsys):
    features, baseline, _, _, _, _ = development_inputs
    output = tmp_path / 'cli-experiment'
    monkeypatch.setattr(sys, 'argv', ['experiment_logistic_v5', '--private-root', str(tmp_path),
        '--features', str(features), '--baseline', str(baseline), '--output', str(output)])
    experiment.main()
    assert 'synthetic-' not in capsys.readouterr().out
    with (output / 'summary.csv').open(encoding='utf-8-sig', newline='') as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 6
    assert {r['model'] for r in rows} == {'v4', 'uniform_replay', 'v5_hard_negative'}
    assert {r['partition'] for r in rows} == {'train', 'validation'}
    assert all(k in rows[0] for k in ('accuracy', 'precision', 'recall', 'f1', 'FP', 'FN',
                                    'unconstrained_matched_only_mae_sec', 'matched_events'))
    assert json.loads((output / 'comparison.json').read_text())['baseline_unchanged']


@pytest.mark.parametrize('settings', [{'hard_negative_factor': .5}, {'hard_negative_fraction': 0},
    {'hard_negative_factor': float('nan')}, {'hard_negative_fraction': 1.1}])
def test_invalid_hard_negative_parameters_are_rejected(settings):
    with pytest.raises(ValueError, match='hard-negative'):
        train.training_arrays([], DetectionConfig(), **settings)
