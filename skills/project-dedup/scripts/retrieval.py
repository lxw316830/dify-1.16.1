"""知识库检索后端。

每个后端把命中结果整理成 Dify 知识检索节点的输出格式，供 workflow_nodes.wrap_evidence 使用：
{"content": 片段原文, "title": 文档名,
 "metadata": {"document_name", "score", "segment_id",
              "doc_metadata": {"project_name", "section_path", "table_name"}}}
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "kb.json"
DEFAULTS = {
    "top_k": 3,
    "score_threshold": 0.7,
    "search_method": "hybrid_search",
    "vector_weight": 0.5,
    "keyword_weight": 0.5,
    "parallel": 4,
    "timeout": 60,
    "max_retries": 3,
    "max_query_chars": 250,
}


class RetrievalError(RuntimeError):
    pass


def load_config() -> Dict:
    config: Dict = dict(DEFAULTS)
    if CONFIG_PATH.is_file():
        config.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    else:
        config["backend"] = "manual"
    return config


def normalize_hit(raw: Dict) -> Dict:
    """把通用格式的命中整理成 Dify 知识检索节点格式。已是该格式的原样返回。"""
    if isinstance(raw.get("metadata"), dict):
        return raw
    document_name = str(raw.get("document_name") or raw.get("title") or "")
    return {
        "content": str(raw.get("content") or ""),
        "title": document_name,
        "metadata": {
            "document_name": document_name,
            "score": raw.get("score", 0),
            "segment_id": str(raw.get("segment_id") or ""),
            "doc_metadata": {
                key: raw[key]
                for key in ("project_name", "section_path", "table_name")
                if raw.get(key)
            },
        },
    }


def _dify_retrieve(config: Dict, query: str) -> List[Dict]:
    base_url = str(config.get("base_url") or "").rstrip("/")
    api_key = str(config.get("api_key") or "")
    dataset_ids = config.get("dataset_ids") or []
    if not base_url or not api_key or not dataset_ids:
        raise RetrievalError("dify 后端需要 base_url、api_key、dataset_ids")

    max_chars = config.get("max_query_chars")
    if max_chars:
        query = query[: int(max_chars)]
    body = json.dumps({
        "query": query,
        "retrieval_model": {
            "search_method": config["search_method"],
            "reranking_enable": False,
            "reranking_mode": "weighted_score",
            "weights": {
                "weight_type": "customized",
                "vector_setting": {
                    "vector_weight": config["vector_weight"],
                    "embedding_provider_name": "",
                    "embedding_model_name": "",
                },
                "keyword_setting": {"keyword_weight": config["keyword_weight"]},
            },
            "top_k": config["top_k"],
            "score_threshold_enabled": config["score_threshold"] is not None,
            "score_threshold": config["score_threshold"] or 0,
        },
    }, ensure_ascii=False).encode("utf-8")

    hits: List[Dict] = []
    for dataset_id in dataset_ids:
        payload = _post_json(
            f"{base_url}/datasets/{dataset_id}/retrieve",
            body,
            {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            config,
        )
        for record in payload.get("records") or []:
            segment = record.get("segment") or {}
            document = segment.get("document") or {}
            doc_metadata = document.get("doc_metadata") if isinstance(document.get("doc_metadata"), dict) else {}
            hits.append({
                "content": segment.get("content") or "",
                "title": document.get("name") or "",
                "metadata": {
                    "document_name": document.get("name") or "",
                    "score": record.get("score", 0),
                    "segment_id": segment.get("id") or "",
                    "doc_metadata": doc_metadata,
                },
            })
    hits.sort(key=lambda hit: float(hit["metadata"].get("score") or 0), reverse=True)
    return hits[: int(config["top_k"])] if len(dataset_ids) > 1 else hits


def _post_json(url: str, body: bytes, headers: Dict[str, str], config: Dict) -> Dict:
    last_error = ""
    for attempt in range(int(config["max_retries"]) + 1):
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=int(config["timeout"])) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            last_error = f"HTTP {exc.code}：{detail}"
            retryable = exc.code >= 500 or exc.code == 429 or "429" in detail
        except urllib.error.URLError as exc:
            last_error = f"无法连接知识库：{exc.reason}"
            retryable = True
        if not retryable or attempt == int(config["max_retries"]):
            break
        time.sleep(2 ** attempt * 2)
    raise RetrievalError(last_error)


BACKENDS: Dict[str, Callable[[Dict, str], List[Dict]]] = {
    "dify": _dify_retrieve,
}


def retrieve_all(queries: Sequence[str], config: Optional[Dict] = None) -> List[List[Dict]]:
    config = config or load_config()
    backend = BACKENDS.get(str(config.get("backend") or ""))
    if backend is None:
        raise RetrievalError(f"不支持的检索后端：{config.get('backend')}")
    with ThreadPoolExecutor(max_workers=max(1, int(config["parallel"]))) as pool:
        return list(pool.map(lambda query: backend(config, query), queries))


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("用法：python3 retrieval.py <查询文本>", file=sys.stderr)
        sys.exit(2)
    for hit in retrieve_all([" ".join(sys.argv[1:])])[0]:
        meta = hit["metadata"]
        print(f"{float(meta['score'] or 0):.4f}  {meta['document_name']}\n    {hit['content'][:120]}")
