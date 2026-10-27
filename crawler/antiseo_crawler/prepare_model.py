"""One-time: download all-MiniLM-L6-v2 (ONNX) and quantize it to int8.

Writes to MODEL_DIR (default /models/minilm):
  model.onnx       int8 model used by the crawler AND the API (copy this exact file)
  tokenizer.json
  source/          the downloaded fp32 original

Run it once. Afterwards never regenerate model.onnx: a different file means a
different hash, and the crawl will (correctly) refuse to mix the two.
"""
from __future__ import annotations

import argparse
import shutil
import urllib.request
from pathlib import Path

from .config import CFG
from .embedder import Embedder, file_sha256

REPO = "https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2/resolve/main"
FILES = {"onnx/model.onnx": "model_fp32.onnx", "tokenizer.json": "tokenizer.json"}


def download(url: str, dest: Path) -> None:
    print(f"downloading {url}")
    req = urllib.request.Request(url, headers={"User-Agent": CFG.user_agent})
    with urllib.request.urlopen(req, timeout=120) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)


def run(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="prepare-model")
    ap.add_argument("--force", action="store_true", help="overwrite an existing model.onnx")
    ap.add_argument("--source-dir", help="use already-downloaded files instead of downloading")
    args = ap.parse_args(argv)

    out = Path(CFG.model_dir)
    final = out / "model.onnx"
    if final.exists() and not args.force:
        print(f"{final} already exists (sha256 {file_sha256(final)}). Use --force to replace it.")
        return 0

    src = out / "source"
    src.mkdir(parents=True, exist_ok=True)
    for remote, local in FILES.items():
        dest = src / local
        if args.source_dir:
            shutil.copy(Path(args.source_dir) / local, dest)
        elif not dest.exists():
            download(f"{REPO}/{remote}", dest)
    shutil.copy(src / "tokenizer.json", out / "tokenizer.json")

    from onnxruntime.quantization import QuantType, quantize_dynamic

    print("quantizing to int8 ...")
    quantize_dynamic(str(src / "model_fp32.onnx"), str(final), weight_type=QuantType.QInt8)

    emb = Embedder(str(out), CFG.embed_threads)
    a, b, c = emb.embed([
        "How to debug a memory leak in Rust",
        "Finding memory leaks in Rust programs",
        "My favourite sourdough bread recipe",
    ])
    print(f"sanity check: similar={float(a @ b):.3f}  unrelated={float(a @ c):.3f}  (similar should be clearly higher)")
    size_mb = final.stat().st_size / 1e6
    print(f"\nmodel.onnx  {size_mb:.1f} MB\nsha256      {emb.sha256}")
    print("Copy this exact model.onnx + tokenizer.json to the app VM later.")
    return 0
