# bge-m3-server

本地 BGE-M3 向量 + bge-reranker-v2-m3 重排服务，Mac 上用 PyTorch MPS（Metal GPU），有 NVIDIA 卡的 Linux 自动用 CUDA，都没有则用 CPU。接口与 Dify 的 OpenAI-API-compatible 插件对齐。

## 使用

```bash
uv sync --python 3.12          # 安装依赖
uv run download_models.py      # 下载模型到 ./models（约 4.2GB；慢可加 HF_ENDPOINT=https://hf-mirror.com）
./start.sh                     # 后台启动，默认 127.0.0.1:8100
./start.sh status | stop
```

环境变量见 `server.py` 顶部注释（HOST、PORT、DEVICE、FP16、API_KEY、ENABLE_RERANK 等）。

## 接口

| 接口 | 说明 |
|---|---|
| `POST /v1/embeddings` | OpenAI 格式，1024 维，已 L2 归一化 |
| `POST /v1/rerank`（也可 `/rerank`） | `{query, documents, top_n}` → `{results:[{index, relevance_score}]}`，分数 0~1 |
| `GET /v1/models`、`GET /health` | 模型列表、健康检查 |

## 接入 Dify

设置 → 模型供应商 → OpenAI-API-compatible → 添加模型：

| 模型类型 | 模型名称 | API endpoint URL | 其他 |
|---|---|---|---|
| Text Embedding | `bge-m3` | `http://host.docker.internal:8100/v1` | 上下文长度 8192 |
| Rerank | `bge-reranker-v2-m3` | `http://host.docker.internal:8100/v1` | — |

API Key 未设置 `API_KEY` 环境变量时随便填。

## 内网部署

把整个目录（含 `models/`）拷过去。模型文件可直接离线使用；Python 依赖需要内网能访问 PyPI 镜像，或提前在同架构/同系统的机器上准备好依赖（注意 Linux 下 torch 需按有无 CUDA 选择对应版本）。对外提供服务时设置 `HOST=0.0.0.0` 和 `API_KEY`。
