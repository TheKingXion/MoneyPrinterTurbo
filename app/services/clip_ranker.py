import io
import re
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from typing import Iterable

import numpy as np
import requests
from loguru import logger
from PIL import Image

from app.config import config
from app.models.schema import MaterialInfo


MODEL_ID = "Xenova/clip-vit-base-patch32"
MODEL_FILE = "model_quantized.onnx"
DEFAULT_THRESHOLD = 0.235
LOCATION_MARGIN = 0.0


@lru_cache(maxsize=1)
def _load_clip():
    from optimum.onnxruntime import ORTModelForZeroShotImageClassification
    from transformers import AutoProcessor

    logger.info("loading CLIP ONNX model for material relevance scoring")
    model = ORTModelForZeroShotImageClassification.from_pretrained(
        MODEL_ID,
        subfolder="onnx",
        file_name=MODEL_FILE,
    )
    # The slow processor uses Pillow/NumPy and avoids a heavyweight torchvision
    # dependency while ONNX Runtime still performs the actual model inference.
    processor = AutoProcessor.from_pretrained(MODEL_ID, use_fast=False)
    return model, processor


def _download_image(url: str) -> Image.Image | None:
    if not url:
        return None
    try:
        response = requests.get(
            url,
            proxies=config.proxy,
            verify=config.app.get("tls_verify", True),
            timeout=(10, 30),
        )
        response.raise_for_status()
        return Image.open(io.BytesIO(response.content)).convert("RGB")
    except Exception as exc:
        logger.warning(f"failed to load candidate thumbnail: {exc}")
        return None


def _query_negatives(query: str) -> list[str]:
    value = query.lower()
    negatives = []
    if any(token in value for token in ("teenage", "teenager", "adolescent")):
        negatives.extend(
            [
                "adult man instead of a teenager",
                "adult woman instead of a teenager",
                "young child instead of a teenager",
            ]
        )
    if "boy" in value:
        negatives.append("woman or girl instead of a boy")
    if "girl" in value:
        negatives.append("man or boy instead of a girl")
    if "microphone" in value:
        negatives.extend(
            [
                "adult corporate presenter holding microphone",
                "singer performing at concert",
            ]
        )
    if "street" in value or "neighborhood" in value:
        negatives.append("aerial drone view of houses")
    if "radio station" in value or "radio studio" in value:
        negatives.append("generic exterior brick building")
    if "warehouse" in value:
        negatives.extend(
            [
                "child studying in a classroom",
                "woman working in a home office",
                "people cooking in a kitchen",
                "bookshelves inside a library",
            ]
        )
    return negatives


def _demographic_labels(query: str) -> tuple[str, list[str]] | None:
    value = query.lower()
    if any(token in value for token in ("teenage boy", "teenager boy", "adolescent boy")):
        return (
            "a photo of a teenage boy",
            [
                "a photo of an adult man",
                "a photo of an adult woman",
                "a photo of a teenage girl",
                "a photo of a young child",
                "an abstract image or object with no human person",
            ],
        )
    if any(token in value for token in ("teenage girl", "teenager girl", "adolescent girl")):
        return (
            "a photo of a teenage girl",
            [
                "a photo of an adult woman",
                "a photo of an adult man",
                "a photo of a teenage boy",
                "a photo of a young child",
                "an abstract image or object with no human person",
            ],
        )
    if any(token in value for token in ("young adult woman", "young woman")):
        return (
            "a photo of a young adult woman",
            [
                "a photo of a young girl or female child",
                "a photo of an adult man",
                "a photo of a teenage boy",
                "an abstract image or object with no human person",
            ],
        )
    if any(token in value for token in ("young adult man", "young man")):
        return (
            "a photo of a young adult man",
            [
                "a photo of a young boy or male child",
                "a photo of an adult woman",
                "a photo of a teenage girl",
                "an abstract image or object with no human person",
            ],
        )
    return None


def _location_labels(query: str) -> tuple[str, list[str]] | None:
    value = query.lower()
    if "warehouse" in value:
        person = next(
            (
                descriptor
                for descriptor in (
                    "young adult woman",
                    "young adult man",
                    "teenage girl",
                    "teenage boy",
                    "woman",
                    "man",
                )
                if descriptor in value
            ),
            "person",
        )
        return (
            f"{person} organizing boxes beside industrial warehouse shelving",
            [
                f"{person} packing moving boxes inside an apartment bedroom",
                f"{person} packing cardboard boxes at home",
                f"{person} doing crafts with boxes in a classroom",
                f"{person} handling boxes inside an office",
                f"{person} standing near bookshelves in a library",
            ],
        )
    return None


def requires_strict_verification(query: str) -> bool:
    return bool(_demographic_labels(query) or _location_labels(query))


def relax_query_for_fallback(query: str) -> str:
    value = re.sub(
        r"\b(?:young adult woman|young adult man|young woman|young man|teenage girl|"
        r"teenage boy|teenager girl|teenager boy|adolescent girl|adolescent boy)\b",
        "person",
        query,
        flags=re.IGNORECASE,
    )
    value = re.sub(r"\b(?:warehouse|radio studio|radio station)\b", "", value, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", value).strip()


def rank_materials(
    materials: Iterable[MaterialInfo],
    query: str,
    required_objects: Iterable[str] | None = None,
    excluded_elements: Iterable[str] | None = None,
    limit: int = 4,
    threshold: float | None = None,
) -> list[MaterialInfo]:
    all_materials = list(materials)
    candidates = []
    seen_thumbnails = set()
    for item in all_materials:
        if not item.thumbnail_url or item.thumbnail_url in seen_thumbnails:
            continue
        seen_thumbnails.add(item.thumbnail_url)
        candidates.append(item)
        if len(candidates) >= 30:
            break
    if not candidates:
        if requires_strict_verification(query):
            return []
        return all_materials[: max(1, limit)]

    with ThreadPoolExecutor(max_workers=8) as executor:
        downloaded_images = list(
            executor.map(
                _download_image, [candidate.thumbnail_url for candidate in candidates]
            )
        )
    images = []
    valid_candidates = []
    for candidate, image in zip(candidates, downloaded_images):
        if image is not None:
            images.append(image)
            valid_candidates.append(candidate)
    if not images:
        return []

    required = ", ".join(required_objects or [])
    positive = f"a stock video frame showing {query}"
    if required:
        positive += f", clearly including {required}"
    negatives = [
        "cryptocurrency trading screen",
        "concert audience or generic crowd",
        "animal wildlife or nature scenery",
        "cigarette next to a smartphone",
        "unrelated adult using a phone",
    ]
    negatives.extend(_query_negatives(query))
    negatives.extend(
        f"a frame showing {value}" for value in (excluded_elements or []) if value
    )
    scene_labels = [positive, "an unrelated generic stock video", *negatives[:8]]
    demographic = _demographic_labels(query)
    labels = list(scene_labels)
    demographic_start = None
    if demographic:
        demographic_start = len(labels)
        labels.extend([demographic[0], *demographic[1]])
    location = _location_labels(query)
    location_start = None
    if location:
        location_start = len(labels)
        labels.extend([location[0], *location[1]])

    try:
        model, processor = _load_clip()
        inputs = processor(
            text=labels,
            images=images,
            return_tensors="np",
            padding=True,
        )
        outputs = model(**inputs)
        image_embeddings = np.asarray(outputs.image_embeds)
        text_embeddings = np.asarray(outputs.text_embeds)
        similarities = image_embeddings @ text_embeddings.T
        positive_scores = similarities[:, 0]
        strongest_negative = np.max(similarities[:, 1 : len(scene_labels)], axis=1)
        scene_match = positive_scores >= strongest_negative - 0.005
        demographic_match = np.ones(len(valid_candidates), dtype=bool)
        if demographic:
            target_score = similarities[:, demographic_start]
            demographic_end = location_start or len(labels)
            alternative_score = np.max(
                similarities[:, demographic_start + 1 : demographic_end], axis=1
            )
            # CLIP often assigns nearby scores to adjacent age/gender labels.
            # Require a real margin so ambiguous adults and non-person imagery
            # are rejected instead of silently becoming the protagonist.
            demographic_match = target_score >= alternative_score + 0.004
        location_match = np.ones(len(valid_candidates), dtype=bool)
        if location:
            target_score = similarities[:, location_start]
            alternative_score = np.max(similarities[:, location_start + 1 :], axis=1)
            location_match = target_score >= alternative_score + LOCATION_MARGIN
        probabilities = np.where(
            scene_match & demographic_match & location_match,
            positive_scores,
            0.0,
        )
    except Exception as exc:
        if demographic or location:
            logger.error(f"CLIP scoring unavailable for strict query: {exc}")
            return []
        logger.error(f"CLIP scoring unavailable, preserving provider order: {exc}")
        probabilities = np.linspace(0.25, 0.24, num=len(valid_candidates))

    for candidate, score in zip(valid_candidates, probabilities):
        candidate.score = float(score)
    ranked = sorted(valid_candidates, key=lambda item: item.score, reverse=True)
    minimum = (
        float(threshold)
        if threshold is not None
        else float(config.app.get("clip_relevance_threshold", DEFAULT_THRESHOLD))
    )
    accepted = [item for item in ranked if item.score >= minimum]
    return accepted[: max(1, limit)]
