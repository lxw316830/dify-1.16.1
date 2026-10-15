# 历史项目知识库

## 配置

脚本只从技能包内的 `config/kb.json` 读取配置，迁移时随技能包一起拷走即可。没有配置文件时使用 `manual` 后端。

```json
{
  "backend": "dify",
  "base_url": "http://知识库地址/v1",
  "api_key": "dataset-xxxx",
  "dataset_ids": ["知识库ID"],
  "search_method": "hybrid_search",
  "top_k": 3,
  "score_threshold": 0.7,
  "vector_weight": 0.5,
  "keyword_weight": 0.5,
  "parallel": 4
}
```

| 字段 | 说明 |
|---|---|
| `backend` | `dify`：Dify 兼容的知识库检索接口；`manual`：由 agent 用自己的工具检索 |
| `search_method` | `hybrid_search`、`semantic_search`、`full_text_search` |
| `score_threshold` | 低于该分数的命中丢弃；`null` 表示不过滤 |
| `max_query_chars` | 查询文本截断长度，默认 250（Dify 检索接口的上限）；`null` 表示不截断。查询最长约 1200 字，截断后只用开头部分检索 |

检查连通性：

```bash
python3 scripts/retrieval.py 负荷资源现场核查
```

## manual 后端：由 agent 检索

脚本生成 `retrieval/queries.json`：

```json
[{"index": 0, "query": "检索文本"}]
```

对每条 query 在历史项目知识库中检索（每条取最相关的 3 个片段），结果写入 `retrieval/hits.json`：

```json
{
  "results": [
    {
      "index": 0,
      "hits": [
        {
          "content": "片段原文",
          "document_name": "历史可研文档名",
          "score": 0.82,
          "project_name": "历史项目名称（没有可省略，默认用文档名）",
          "section_path": "章节路径（可省略）",
          "table_name": "表名（可省略）",
          "segment_id": "片段ID（可省略）"
        }
      ]
    }
  ]
}
```

每条 query 都要有一条结果，没有命中写 `"hits": []`。片段原文必须是知识库返回的原文，不要改写。
