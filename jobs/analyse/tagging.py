"""News tagging with GLiNER2: companies, places, commodities and an event type.

Model: ``fastino/gliner2-base-v1`` (Apache-2.0), run on CPU through the
``gliner2`` package. One combined schema per title does entity extraction and
single-label classification in the same encoder pass.

* Places become ISO 3166-1 alpha-3 codes only on an exact country-name match
  (:mod:`jobs.analyse.places`); anything else keeps ``"country": null``.
* Sources that already say what kind of event they are (GDACS and EONET are
  natural hazards, OFAC is sanctions) keep that type (``type_source =
  'source'``); the model's own guess is still stored as ``type_guess`` so its
  accuracy can be checked. GDELT articles take the model's type
  (``type_source = 'model'``) when its confidence is at least
  :data:`MIN_TYPE_CONFIDENCE`, otherwise ``other``.
* ``model_version`` combines the model revision with a hash of the label
  schema below, so editing a label or threshold re-tags everything once.

The heavy libraries (torch, transformers) are imported only when a real model
is loaded, so the rest of the jobs package and the unit tests do not need them.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, Sequence

from jobs.analyse import places

MODEL_ID = os.environ.get("TAG_MODEL_ID", "fastino/gliner2-base-v1")
# Pinned Hugging Face commit; "main" until the first workflow run resolves it.
MODEL_REVISION = os.environ.get("TAG_MODEL_REVISION", "main")

# Prompt label (what the model reads) → description, keyed by the stored slug.
EVENT_TYPES: dict[str, tuple[str, str]] = {
    "conflict": ("armed conflict", "war, military strikes, armed attacks, terrorism or fighting"),
    "protest": ("protest", "demonstrations, riots or civil unrest"),
    "sanctions": ("sanctions", "government sanctions, asset freezes, export controls or blacklists"),
    "trade_policy": ("trade policy", "tariffs, trade deals, import or export bans and quotas"),
    "port_shipping_disruption": (
        "port or shipping disruption",
        "port closures, congestion, canal or strait blockages, attacks on vessels, freight delays",
    ),
    "natural_hazard": ("natural hazard", "earthquakes, storms, floods, wildfires, volcanoes, droughts"),
    "cyber_incident": ("cyber incident", "cyberattacks, ransomware, data breaches, software vulnerabilities"),
    "labour_action": ("labour action", "strikes, walkouts or industrial action by workers or unions"),
    "financial_stress": (
        "financial stress",
        "currency crises, defaults, bank failures, market crashes or capital controls",
    ),
    "other": ("other", "anything else, including general business news"),
}
LABEL_TO_SLUG = {label: slug for slug, (label, _) in EVENT_TYPES.items()}

# Stored entity group → (prompt label, description).
ENTITY_TYPES: dict[str, tuple[str, str]] = {
    "companies": ("company", "a named business, corporation or brand"),
    "places": ("location", "a named country, region, city, port, sea, strait or canal"),
    "commodities": ("commodity", "a traded raw material, energy product, crop or industrial good"),
}

ENTITY_THRESHOLD = 0.5
MIN_TYPE_CONFIDENCE = 0.4
MAX_TEXT_CHARS = 600  # titles are short; this only guards against odd inputs

# Which sources are tagged, and the type the source itself already states.
TAG_SOURCES = ("gdelt", "gdacs", "eonet", "ofac")
SOURCE_TYPES = {"gdacs": "natural_hazard", "eonet": "natural_hazard", "ofac": "sanctions"}


def schema_spec() -> dict[str, Any]:
    return {
        "entities": {k: list(v) for k, v in ENTITY_TYPES.items()},
        "event_types": {k: list(v) for k, v in EVENT_TYPES.items()},
        "entity_threshold": ENTITY_THRESHOLD,
        "min_type_confidence": MIN_TYPE_CONFIDENCE,
        "source_types": SOURCE_TYPES,
    }


def schema_hash() -> str:
    raw = json.dumps(schema_spec(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:8]


def model_version(revision: str) -> str:
    return f"{revision[:12]}+schema.{schema_hash()}"


def text_md5(text: str) -> str:
    """Same digest as Postgres ``md5(title)`` (both hash the UTF-8 bytes)."""
    return hashlib.md5(text.encode("utf-8")).hexdigest()


# --- model interface ---------------------------------------------------------


class Model(Protocol):
    """Anything that turns texts into GLiNER2-style formatted results."""

    name: str
    revision: str

    def predict(self, texts: Sequence[str]) -> list[dict[str, Any]]: ...


class ModelUnavailable(Exception):
    """The model or its libraries could not be loaded (safe to log)."""


class Gliner2Model:
    """The real model, loaded from the Hugging Face cache (or downloaded)."""

    def __init__(
        self,
        model_id: str = MODEL_ID,
        revision: str = MODEL_REVISION,
        *,
        batch_size: int = 16,
        threads: int | None = None,
    ):
        try:
            import torch
            from gliner2 import GLiNER2
            from huggingface_hub import snapshot_download
        except ImportError as exc:
            raise ModelUnavailable(f"model libraries not installed ({exc.name})") from None
        try:
            if threads:
                torch.set_num_threads(threads)
            # Honours HF_HUB_OFFLINE=1: then only the local cache is used.
            path = snapshot_download(model_id, revision=revision)
            self.model = GLiNER2.from_pretrained(path)
            self.model.eval()
        except Exception as exc:  # network, cache miss, bad checkpoint...
            raise ModelUnavailable(f"model could not be loaded ({type(exc).__name__})") from None
        self.name = model_id
        # snapshot paths end in the resolved commit hash.
        self.revision = os.path.basename(os.path.normpath(path))
        self.batch_size = batch_size
        self.schema = (
            self.model.create_schema()
            .entities({label: desc for label, desc in ENTITY_TYPES.values()}, threshold=ENTITY_THRESHOLD)
            .classification("event_type", {label: desc for label, desc in EVENT_TYPES.values()})
        )
        self._torch = torch

    def predict(self, texts: Sequence[str]) -> list[dict[str, Any]]:
        with self._torch.inference_mode():
            return self.model.batch_extract(
                list(texts),
                self.schema,
                batch_size=self.batch_size,
                threshold=ENTITY_THRESHOLD,
                include_confidence=True,
            )


# --- turning model output into stored tags ------------------------------------


@dataclass(frozen=True)
class TagInput:
    source: str
    raw_id: str
    title: str


@dataclass
class Tag:
    source: str
    raw_id: str
    input_md5: str
    entities: dict[str, Any]
    event_type: str
    type_confidence: float | None
    type_source: str  # 'model' | 'source'


def _spans(value: Any) -> list[tuple[str, float | None]]:
    """Accept GLiNER2's ``[{"text", "confidence"}]``, ``["text"]`` or a single span."""
    if value is None:
        return []
    if isinstance(value, (str, dict)):
        value = [value]
    out = []
    for v in value:
        if isinstance(v, dict):
            text, conf = v.get("text"), v.get("confidence")
        else:
            text, conf = v, None
        if isinstance(text, str) and text.strip():
            out.append((text.strip(), float(conf) if conf is not None else None))
    return out


def _classification(value: Any) -> tuple[str | None, float | None]:
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, dict):
        conf = value.get("confidence")
        return value.get("label"), float(conf) if conf is not None else None
    if isinstance(value, str):
        return value, None
    return None, None


def build_tag(item: TagInput, raw: dict[str, Any]) -> Tag:
    """Convert one formatted GLiNER2 result into a :class:`Tag`.

    Raises ``ValueError`` if the classification label is not one of ours.
    """
    found = raw.get("entities") or {}
    entities: dict[str, Any] = {}
    for group, (label, _) in ENTITY_TYPES.items():
        seen: dict[str, dict[str, Any]] = {}
        for text, conf in _spans(found.get(label)):
            k = text.casefold()
            if k in seen and (seen[k].get("confidence") or 0) >= (conf or 0):
                continue
            entry: dict[str, Any] = {"text": text}
            if conf is not None:
                entry["confidence"] = round(conf, 3)
            if group == "places":
                entry["country"] = places.country_code(text)
            seen[k] = entry
        entities[group] = list(seen.values())
    entities["countries"] = sorted({p["country"] for p in entities["places"] if p.get("country")})

    label, conf = _classification(raw.get("event_type"))
    slug = LABEL_TO_SLUG.get(label or "")
    if slug is None:
        raise ValueError("model returned an unknown event type")
    entities["type_guess"] = {"type": slug, "confidence": round(conf, 3) if conf is not None else None}

    if item.source in SOURCE_TYPES:
        event_type, type_conf, type_source = SOURCE_TYPES[item.source], None, "source"
    elif conf is not None and conf >= MIN_TYPE_CONFIDENCE:
        event_type, type_conf, type_source = slug, round(conf, 4), "model"
    else:
        event_type, type_conf, type_source = "other", None, "model"
    return Tag(item.source, item.raw_id, text_md5(item.title), entities, event_type, type_conf, type_source)


@dataclass
class TagRun:
    tags: list[Tag] = field(default_factory=list)
    notes: dict[str, int] = field(default_factory=dict)
    error: str | None = None
    stopped_early: bool = False


def tag_inputs(
    model: Model,
    items: Sequence[TagInput],
    *,
    chunk: int = 64,
    budget_s: float = 600.0,
    clock: Callable[[], float] = time.monotonic,
    max_consecutive_failures: int = 2,
) -> TagRun:
    """Tag ``items`` in chunks until done or the time budget runs out.

    A failing chunk is counted and skipped; two failures in a row stop the run.
    Errors carry only the exception class, never input text.
    """
    run = TagRun()
    deadline = clock() + budget_s
    streak = 0
    notes = run.notes
    for start in range(0, len(items), chunk):
        if clock() >= deadline or streak >= max_consecutive_failures:
            run.stopped_early = True
            notes["not_run"] = len(items) - start
            break
        part = list(items[start : start + chunk])
        texts = [it.title[:MAX_TEXT_CHARS] for it in part]
        try:
            results = model.predict(texts)
            if len(results) != len(part):
                raise ValueError("result count mismatch")
        except Exception as exc:
            streak += 1
            notes["failed"] = notes.get("failed", 0) + len(part)
            run.error = f"inference failed ({type(exc).__name__})"
            continue
        streak = 0
        for it, raw in zip(part, results):
            try:
                run.tags.append(build_tag(it, raw or {}))
            except (ValueError, TypeError, AttributeError):
                notes["unparsed"] = notes.get("unparsed", 0) + 1
    notes["tagged"] = len(run.tags)
    return run
