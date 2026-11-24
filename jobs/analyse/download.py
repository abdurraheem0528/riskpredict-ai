"""Download the tagging model into the Hugging Face cache and load it once.

    python -m jobs.analyse.download
    python -m jobs.analyse.download --cache-key   # print the workflow cache key

Used by the workflows only when the model cache misses, so every other step
can run with ``HF_HUB_OFFLINE=1``. Loading once also caches the tokenizer.
Prints a single status line.
"""

from __future__ import annotations

import re
import sys

from jobs.analyse import tagging


def cache_key() -> str:
    """Changes when the model id or pinned revision changes (bump ``-v1`` to force)."""
    return "hf-" + re.sub(r"[^A-Za-z0-9._-]+", "--", f"{tagging.MODEL_ID}-{tagging.MODEL_REVISION}") + "-v1"


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv == ["--cache-key"]:
        print(cache_key())
        return 0
    try:
        model = tagging.Gliner2Model()
    except tagging.ModelUnavailable as exc:
        print(f"model download failed: {exc}", file=sys.stderr)
        return 1
    print(f"model cached: {model.name} @ {model.revision[:12]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
