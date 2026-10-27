"""MiniLM sentence embeddings with ONNX Runtime (no PyTorch).

The model directory holds two files:
  model.onnx      int8-quantized all-MiniLM-L6-v2 (made by `prepare-model`)
  tokenizer.json  the matching WordPiece tokenizer

The API on the app VM must load the *same* model.onnx. Its SHA-256 is stored in
meta.embedding_model, and a crawl refuses to run if the hashes differ.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer

DIM = 384
MAX_TOKENS = 256  # all-MiniLM-L6-v2 was trained with max_seq_length 256


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class Embedder:
    def __init__(self, model_dir: str, threads: int = 3):
        d = Path(model_dir)
        model_path, tok_path = d / "model.onnx", d / "tokenizer.json"
        for p in (model_path, tok_path):
            if not p.exists():
                raise FileNotFoundError(f"{p} missing - run `prepare-model` first")
        self.sha256 = file_sha256(model_path)

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = threads
        opts.inter_op_num_threads = 1
        self.session = ort.InferenceSession(
            str(model_path), opts, providers=["CPUExecutionProvider"]
        )
        self.input_names = {i.name for i in self.session.get_inputs()}

        self.tok = Tokenizer.from_file(str(tok_path))
        self.tok.enable_truncation(max_length=MAX_TOKENS)
        self.tok.enable_padding(pad_id=0, pad_token="[PAD]")

    def embed(self, texts: list[str], batch_size: int = 32) -> np.ndarray:
        """Return an (n, 384) float32 array of L2-normalized embeddings."""
        out = []
        for i in range(0, len(texts), batch_size):
            out.append(self._embed_batch(texts[i : i + batch_size]))
        return np.vstack(out) if out else np.zeros((0, DIM), dtype=np.float32)

    def _embed_batch(self, texts: list[str]) -> np.ndarray:
        enc = self.tok.encode_batch(texts)
        ids = np.array([e.ids for e in enc], dtype=np.int64)
        mask = np.array([e.attention_mask for e in enc], dtype=np.int64)
        feeds = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in self.input_names:
            feeds["token_type_ids"] = np.zeros_like(ids)
        token_vecs = self.session.run(None, feeds)[0]  # (batch, seq, 384)

        # Mean pooling over real tokens, then L2 normalization (what
        # sentence-transformers does for this model).
        m = mask[..., None].astype(np.float32)
        summed = (token_vecs * m).sum(axis=1)
        counts = np.clip(m.sum(axis=1), 1e-9, None)
        vecs = summed / counts
        norms = np.clip(np.linalg.norm(vecs, axis=1, keepdims=True), 1e-12, None)
        return (vecs / norms).astype(np.float32)


def to_pgvector(vec: np.ndarray) -> str:
    """Text form accepted by `::halfvec` casts, e.g. '[0.1,-0.2,...]'."""
    return "[" + ",".join(f"{x:.6f}" for x in vec.tolist()) + "]"
