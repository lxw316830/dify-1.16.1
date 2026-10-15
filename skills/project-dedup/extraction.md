# 信息提取规则

输入文件是若干个 `<chunk>` 原文分块，每块带 `chunk_id`、`section_path`、`extract_mode`。按下面的规则提取，结果写成一个 JSON 对象。

## 规则（与原工作流提示词一致）

你是项目可行性研究报告分块信息提取助手。

【通用规则】

1. 必须将输入中的每个 chunk_id 放入 processed_chunk_ids。
2. 所有提取内容必须是当前分块中的完整连续原文。
3. 禁止总结、改写、补充或拼接不连续原文。
4. 每条结果必须填写实际 chunk_id。
5. 没有可提取内容的分块，仍必须放入 processed_chunk_ids。
6. 禁止提取功能清单。功能清单由代码节点单独处理。
7. 禁止提取集成工作、系统实施、实施范围、技术方案和培训内容。

【extract_mode=requirement】

1. 只允许输出 requirements，禁止输出 contents。
2. 每个包含“本期建设需求”的分块必须至少输出一条需求内容。
3. 项目目标输出为“项目目标”。
4. 预期成效或预期成果输出为“项目成果”。
5. 出现“调用、接入、回传、同步、对接”等词语时，只要同时描述独立业务能力，就必须提取。
6. 不得选择性遗漏编号需求章节。

【extract_mode=project_content】

1. 只允许输出 contents，禁止输出 requirements。
2. 每个 project_content 分块只要包含实际建设内容，至少输出一条 contents。
3. 优先保留模块级总述原文，例如“主要包括...”“完成...工作”“本项目为...系统开发工作”。
4. 二级功能名称及其概述可以作为项目内容；禁止只提取按钮级、字段级或单个增删改查操作。
5. 出现“查询、展示、新增、编辑、删除、上传、下载”等词语时，不得因此整段丢弃；只有纯操作步骤才跳过。
6. 禁止将章节标题单独作为项目内容。

## 输出格式

```json
{
  "processed_chunk_ids": ["chunk_0001", "chunk_0002"],
  "requirements": [
    {
      "chunk_id": "chunk_0001",
      "requirement_type": "需求内容",
      "content": "从分块中原样复制的连续原文",
      "source_type": "正文",
      "table_name": ""
    }
  ],
  "contents": [
    {
      "chunk_id": "chunk_0002",
      "content": "从分块中原样复制的连续原文",
      "source_type": "正文",
      "table_name": ""
    }
  ]
}
```

- `requirement_type` 只能是 `需求内容`、`项目目标`、`项目成果`。
- `source_type` 只能是 `正文`、`标题`、`表格`；来自表格时 `table_name` 填表名，否则填空字符串。
- 三个数组都必须存在，没有内容时写 `[]`。

## 提交时脚本会检查

- 本批次每个 chunk_id 都在 `processed_chunk_ids` 里，且没有本批次之外的 ID。
- 每条 `content` 都能在对应分块里找到连续原文（忽略空白和中英文标点差异）。改写、缩写、跨段拼接都会被打回。
- requirement 分块的结果只放 `requirements`，project_content 分块的结果只放 `contents`。
- 标记为必须提取的需求分块至少有一条 `requirements`。

复制原文时整句整段地复制，不要删减中间的字。JSON 字符串里的双引号写成 `\"`，换行写成 `\n`。
