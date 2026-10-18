import json
import os
from pathlib import Path
import socket

import numpy as np
import pytest

from ml.backbone import DinoV2Encoder, prepare_rgb
from ml.extract_features import extract_video


def test_letterbox_keeps_frame_edges():
    image = np.zeros((10, 40, 3), dtype=np.uint8)
    image[:, :10, 0] = 255
    image[:, -10:, 2] = 255
    result = prepare_rgb(image, [0.5] * 3, [0.5] * 3)
    assert result.shape == (3, 224, 224)
    assert result[0, 112, 0] == pytest.approx(1)
    assert result[2, 112, -1] == pytest.approx(1)
    assert abs(result[:, 0, 0]).max() < 0.01


class SyntheticEncoder:
    """Test double only for cache plumbing, never a production detector."""
    device = "cpu"
    weight_sha256 = "synthetic-test-double"
    fallback_used = False

    def __init__(self):
        self.batch_sizes = []

    def encode(self, images):
        self.batch_sizes.append(len(images))
        return np.ones((len(images), 384), dtype=np.float32)


def test_bounded_batches_and_numeric_cache(tmp_path, make_video):
    encoder = SyntheticEncoder()
    result = extract_video(make_video(), tmp_path / "cache", encoder, batch_size=2)
    assert encoder.batch_sizes == [2, 2, 1]
    assert result["processing_sec"] > 0
    with np.load(tmp_path / "cache" / "features.npz", allow_pickle=False) as archive:
        assert archive["embeddings"].shape == (5, 384)
        assert archive["embeddings"].dtype == np.float32
        assert archive["timestamps_sec"].dtype == np.float64
        assert archive["frame_indices"].tolist() == [0, 5, 10, 15, 19]
    assert json.loads((tmp_path / "cache" / "metadata.json").read_text())["embedding_dim"] == 384


def test_missing_snapshot_never_connects(tmp_path, monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Unexpected network access")
    monkeypatch.setattr(socket.socket, "connect", denied)
    with pytest.raises((FileNotFoundError, OSError)):
        DinoV2Encoder(tmp_path / "missing")


def test_mps_operation_fallback_retries_on_cpu(monkeypatch):
    import torch
    from types import SimpleNamespace

    class FailingMpsModel:
        def __init__(self):
            self.calls = 0
        def to(self, device):
            assert device == "cpu"
        def __call__(self, pixel_values):
            self.calls += 1
            if self.calls == 1:
                raise NotImplementedError("Synthetic unsupported MPS operation")
            return SimpleNamespace(last_hidden_state=torch.ones((1, 257, 384)))

    encoder = DinoV2Encoder.__new__(DinoV2Encoder)
    encoder.model = FailingMpsModel()
    encoder.mean, encoder.std = [0.5] * 3, [0.5] * 3
    encoder.device, encoder.fallback_used = "mps", False
    # Exercise fallback control flow without requiring real MPS in the sandbox.
    real_to = torch.Tensor.to
    monkeypatch.setattr(torch.Tensor, "to", lambda self, device, *a, **kw:
                        self if device == "mps" else real_to(self, device, *a, **kw))
    monkeypatch.setattr(torch.mps, "empty_cache", lambda: None)
    with pytest.warns(RuntimeWarning, match="retrying"):
        result = encoder.encode([np.zeros((32, 32, 3), dtype=np.uint8)])
    assert result.shape == (1, 384)
    assert encoder.device == "cpu" and encoder.fallback_used
    assert encoder.model.calls == 2


@pytest.mark.skipif(not os.environ.get("FORTEVISION_TEST_MODEL_DIR"), reason="Set public snapshot path for real pretrained smoke test")
def test_real_pretrained_offline_video_embeddings(tmp_path, make_video, monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Offline encoder attempted network access")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    encoder = DinoV2Encoder(Path(os.environ["FORTEVISION_TEST_MODEL_DIR"]), device="cpu")
    assert not encoder.model.training
    assert not any(p.requires_grad for p in encoder.model.parameters())
    pixels = np.random.default_rng(11).integers(0, 256, (64, 96, 3), dtype=np.uint8)
    first = encoder.encode([pixels])
    assert first.shape == (1, 384)
    np.testing.assert_allclose(first, encoder.encode([pixels]), atol=1e-5)
    result = extract_video(make_video(), tmp_path / "real_features", encoder, batch_size=2)
    assert len(result["frames"]) == 5
    with np.load(tmp_path / "real_features" / "features.npz", allow_pickle=False) as archive:
        assert np.isfinite(archive["embeddings"]).all()
        assert not np.allclose(archive["embeddings"][0], archive["embeddings"][-1])


def test_brightness_feature_cache_keeps_pts(tmp_path, make_video):
    encoder = SyntheticEncoder()
    result = extract_video(make_video(), tmp_path / "cache", encoder, batch_size=2, augment_brightness=True)
    with np.load(tmp_path / "cache/features.npz", allow_pickle=False) as archive:
        assert archive["brightness_embeddings"].shape == archive["embeddings"].shape == (5, 384)
        assert archive["timestamps_sec"].tolist() == [0, .5, 1, 1.5, 1.9]
    assert result["augmentations"] == ["brightness"]
    assert result["duration_sec"] == 2


def test_public_checksum_rejects_forged_local_manifest(tmp_path):
    from ml.backbone import MODEL_FILES, MODEL_ID, MODEL_REVISION, sha256_file
    for name in MODEL_FILES:
        (tmp_path / name).write_bytes(b"synthetic corrupted public snapshot")
    manifest = {"model_id": MODEL_ID, "revision": MODEL_REVISION,
                "sha256": {name: sha256_file(tmp_path / name) for name in MODEL_FILES}}
    (tmp_path / "download_manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="published"):
        DinoV2Encoder(tmp_path)
