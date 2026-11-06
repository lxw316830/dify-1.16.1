"""本地 BGE-M3 向量 + 重排服务。

接口（与 Dify 的 OpenAI-API-compatible 插件对齐）：
  POST /v1/embeddings  OpenAI 格式
  POST /v1/rerank      Jina/Cohere 格式：{model, query, documents, top_n} -> {results:[{index, relevance_score}]}
  GET  /v1/models      已加载模型
  GET  /health

环境变量：
  HOST / PORT        监听地址，默认 127.0.0.1:8100
  DEVICE             mps / cuda / cpu，默认自动选择
  FP16               1 使用半精度（GPU 默认开启），0 关闭
  API_KEY            设置后要求 Authorization: Bearer <API_KEY>
  ENABLE_RERANK      0 不加载重排模型，默认 1
  EMBED_MAX_LENGTH   向量最大 token 数，默认 8192
  RERANK_MAX_LENGTH  重排 query+文档 最大 token 数，默认 1024
  BATCH_SIZE         默认 16
"""

import base64
import logging
import os
import threading
import time
from pathlib import Path

import torch
import torch.nn.functional as F
import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel
from transformers import AutoModel, AutoModelForSequenceClassification, AutoTokenizer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bge-m3-server")

MODELS_DIR = Path(__file__).parent / "models"
EMBED_NAME = "bge-m3"
RERANK_NAME = "bge-reranker-v2-m3"


def _pick_device() -> str:
    if os.getenv("DEVICE"):
        return os.environ["DEVICE"]
    if torch.backends.mps.is_available():
        return "mps"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


DEVICE = _pick_device()
FP16 = os.getenv("FP16", "1" if DEVICE != "cpu" else "0") == "1"
API_KEY = os.getenv("API_KEY", "")
ENABLE_RERANK = os.getenv("ENABLE_RERANK", "1") == "1"
EMBED_MAX_LENGTH = int(os.getenv("EMBED_MAX_LENGTH", "8192"))
RERANK_MAX_LENGTH = int(os.getenv("RERANK_MAX_LENGTH", "1024"))
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "16"))

# MPS 不适合多线程并发调用，同一时间只跑一个推理
_lock = threading.Lock()


def _load(cls, name: str):
    path = MODELS_DIR / name
    if not path.exists():
        raise SystemExit(f"找不到模型目录 {path}，请先运行 uv run download_models.py")
    tok = AutoTokenizer.from_pretrained(path)
    model = cls.from_pretrained(path)
    if FP16:
        model = model.half()
    return tok, model.to(DEVICE).eval()


log.info("device=%s fp16=%s", DEVICE, FP16)
embed_tok, embed_model = _load(AutoModel, EMBED_NAME)
log.info("已加载 %s", EMBED_NAME)
rerank_tok = rerank_model = None
if ENABLE_RERANK:
    rerank_tok, rerank_model = _load(AutoModelForSequenceClassification, RERANK_NAME)
    log.info("已加载 %s", RERANK_NAME)


@torch.inference_mode()
def embed(texts: list[str]) -> tuple[list[list[float]], int]:
    """BGE-M3 稠密向量：CLS 池化 + L2 归一化。按长度排序分批以减少 padding。"""
    order = sorted(range(len(texts)), key=lambda i: len(texts[i]), reverse=True)
    vectors: list[list[float] | None] = [None] * len(texts)
    tokens = 0
    for start in range(0, len(order), BATCH_SIZE):
        idx = order[start : start + BATCH_SIZE]
        enc = embed_tok(
            [texts[i] for i in idx], padding=True, truncation=True, max_length=EMBED_MAX_LENGTH, return_tensors="pt"
        ).to(DEVICE)
        tokens += int(enc["attention_mask"].sum())
        cls = embed_model(**enc).last_hidden_state[:, 0]
        out = F.normalize(cls.float(), p=2, dim=-1).cpu().tolist()
        for i, v in zip(idx, out):
            vectors[i] = v
    return vectors, tokens  # type: ignore[return-value]


@torch.inference_mode()
def rerank(query: str, docs: list[str]) -> list[float]:
    scores: list[float] = []
    for start in range(0, len(docs), BATCH_SIZE):
        batch = docs[start : start + BATCH_SIZE]
        enc = rerank_tok(
            [query] * len(batch), batch, padding=True, truncation=True, max_length=RERANK_MAX_LENGTH, return_tensors="pt"
        ).to(DEVICE)
        logits = rerank_model(**enc).logits.view(-1).float()
        scores.extend(torch.sigmoid(logits).cpu().tolist())
    return scores


def check_auth(authorization: str | None = Header(default=None)) -> None:
    if API_KEY and authorization != f"Bearer {API_KEY}":
        raise HTTPException(status_code=401, detail="invalid api key")


app = FastAPI(title="bge-m3-server")


class EmbeddingRequest(BaseModel):
    input: str | list[str]
    model: str = EMBED_NAME
    encoding_format: str | None = "float"


class RerankRequest(BaseModel):
    query: str
    documents: list[str | dict]
    model: str = RERANK_NAME
    top_n: int | None = None
    return_documents: bool = False


@app.get("/health")
def health():
    return {"status": "ok", "device": DEVICE, "fp16": FP16}


@app.get("/v1/models", dependencies=[Depends(check_auth)])
def models():
    names = [EMBED_NAME] + ([RERANK_NAME] if ENABLE_RERANK else [])
    return {"object": "list", "data": [{"id": n, "object": "model", "owned_by": "local"} for n in names]}


@app.post("/v1/embeddings", dependencies=[Depends(check_auth)])
def embeddings(req: EmbeddingRequest):
    texts = [req.input] if isinstance(req.input, str) else req.input
    if not texts:
        raise HTTPException(status_code=400, detail="input is empty")
    t = time.time()
    with _lock:
        vectors, tokens = embed(texts)
    log.info("embed %d texts, %d tokens, %.0fms", len(texts), tokens, (time.time() - t) * 1000)

    def encode(v: list[float]):
        if req.encoding_format == "base64":
            return base64.b64encode(torch.tensor(v, dtype=torch.float32).numpy().tobytes()).decode()
        return v

    return {
        "object": "list",
        "model": req.model,
        "data": [{"object": "embedding", "index": i, "embedding": encode(v)} for i, v in enumerate(vectors)],
        "usage": {"prompt_tokens": tokens, "total_tokens": tokens},
    }


@app.post("/v1/rerank", dependencies=[Depends(check_auth)])
@app.post("/rerank", dependencies=[Depends(check_auth)])
def rerank_api(req: RerankRequest):
    if not ENABLE_RERANK:
        raise HTTPException(status_code=404, detail="rerank disabled")
    docs = [d if isinstance(d, str) else str(d.get("text", d)) for d in req.documents]
    if not docs:
        return {"model": req.model, "results": []}
    t = time.time()
    with _lock:
        scores = rerank(req.query, docs)
    log.info("rerank %d docs, %.0fms", len(docs), (time.time() - t) * 1000)
    ranked = sorted(range(len(docs)), key=lambda i: scores[i], reverse=True)
    if req.top_n is not None:
        ranked = ranked[: req.top_n]
    results = []
    for i in ranked:
        item = {"index": i, "relevance_score": scores[i]}
        if req.return_documents:
            item["document"] = {"text": docs[i]}
        results.append(item)
    return {"model": req.model, "results": results}


if __name__ == "__main__":
    uvicorn.run(app, host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", "8100")))
