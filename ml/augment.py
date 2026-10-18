"""Deterministic spatial stress tests; no temporal changes or generated evidence.

Only mild brightness is offered as training augmentation. Other transforms can hide
an event and are evaluation stress tests, not assertions of label invariance.
"""
from __future__ import annotations

import numpy as np
from PIL import Image

PROFILES = ("none", "brightness", "scale", "perspective", "occlusion", "resolution")


def perturb_rgb(rgb: np.ndarray, profile: str) -> np.ndarray:
    if profile not in PROFILES:
        raise ValueError("Unknown perturbation profile")
    if profile == "none":
        return rgb
    image = Image.fromarray(rgb)
    width, height = image.size
    if profile == "brightness":
        return np.rint(rgb.astype(np.float32) * .85).clip(0, 255).astype(np.uint8)
    if profile == "scale":
        small = image.resize((max(1, round(width*.85)), max(1, round(height*.85))), Image.Resampling.BICUBIC)
        image = Image.new("RGB", (width, height), (124, 116, 104))
        image.paste(small, ((width-small.width)//2, (height-small.height)//2))
    elif profile == "perspective":
        image = image.transform((width, height), Image.Transform.PERSPECTIVE,
                                (1, .03, 0, .02, 1, 0, .04/width, .02/height), Image.Resampling.BICUBIC)
    elif profile == "occlusion":
        array = rgb.copy()
        array[height//3:height//2, width//3:width//2] = (124, 116, 104)
        return array
    elif profile == "resolution":
        image = image.resize((max(1, width//2), max(1, height//2)), Image.Resampling.BILINEAR)
        image = image.resize((width, height), Image.Resampling.BILINEAR)
    return np.asarray(image).copy()


class PerturbedEncoder:
    def __init__(self, encoder, profile: str):
        if profile not in PROFILES:
            raise ValueError("Unknown perturbation profile")
        self.encoder, self.profile = encoder, profile

    def __getattr__(self, name):
        return getattr(self.encoder, name)

    def encode(self, images: list[np.ndarray]) -> np.ndarray:
        return self.encoder.encode([perturb_rgb(image, self.profile) for image in images])
