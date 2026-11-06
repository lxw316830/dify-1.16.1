"""下载 BGE-M3 和 bge-reranker-v2-m3 到 ./models。

只下载 PyTorch 推理需要的文件（跳过 onnx 等大文件）。
国内网络慢时可设置镜像：HF_ENDPOINT=https://hf-mirror.com uv run download_models.py
"""

from pathlib import Path

from huggingface_hub import snapshot_download

MODELS_DIR = Path(__file__).parent / "models"

REPOS = {
    "bge-m3": "BAAI/bge-m3",
    "bge-reranker-v2-m3": "BAAI/bge-reranker-v2-m3",
}

ALLOW = ["*.json", "*.model", "*.txt", "*.safetensors", "pytorch_model.bin"]
IGNORE = ["onnx/*", "imgs/*", "colbert_linear.pt", "sparse_linear.pt"]


def main() -> None:
    for name, repo in REPOS.items():
        target = MODELS_DIR / name
        print(f"==> {repo} -> {target}")
        snapshot_download(repo_id=repo, local_dir=target, allow_patterns=ALLOW, ignore_patterns=IGNORE)
    print("完成")


if __name__ == "__main__":
    main()
