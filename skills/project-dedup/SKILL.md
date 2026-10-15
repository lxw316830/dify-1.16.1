---
name: project-dedup
description: 对项目可行性研究报告（可研）做查重：提取需求、项目内容和功能清单，到历史项目知识库检索相似片段，评估相似度并生成查重报告。用于用户要求对可研文档、立项材料查重，判断是否与历史项目重复建设，或批量查重一个文件夹的 doc/docx/wps 文档时。
---

# 项目查重

脚本负责确定性步骤，并把需要判断的步骤拆成待办交给你。你按规则完成待办、写入 JSON，再由脚本校验。所有命令都用 `python3`，脚本只依赖标准库。下文 `SKILL` 指本文件所在目录。

## 流程

```
- [ ] 1. 检查知识库配置
- [ ] 2. prepare 文档
- [ ] 3. 循环：next → 完成待办 → submit，直到查重完成
- [ ] 4. 向用户汇报结论
```

**1. 检查知识库配置**

确认 `SKILL/config/kb.json` 存在。不存在时按 [knowledge-base.md](knowledge-base.md) 向用户要知识库地址、API Key 和知识库 ID 并写入该文件；用户没有可用接口时，检索会以 `manual` 方式交给你完成。

**2. prepare 文档**

```bash
python3 SKILL/scripts/dedup.py prepare <可研文档> --work <工作目录>
```

`--work` 默认是 `./dedup-work/<文件名>`。非 docx 文档会自动转换（macOS 用 textutil，Windows 用 WPS/Word，其他情况用 LibreOffice）。提示未识别项目名称时，向用户确认后加 `--project-name` 重新运行。

**3. 处理待办**

`prepare`、`next`、`submit` 都会打印下一个待办，包括读取哪个文件、按哪份规则、写到哪里、怎么提交：

- 信息提取待办：按 [extraction.md](extraction.md)。
- 证据评估待办：按 [evaluation.md](evaluation.md)。
- 知识库检索待办（仅 manual 后端）：按 [knowledge-base.md](knowledge-base.md)。

每完成一个就运行打印出来的 `submit` 命令。校验失败时按列出的问题修改同一个结果文件再提交，不要跳过。同一阶段的待办互不依赖，能并行时可以分给子代理，用 `next <工作目录> --all` 查看全部。

检索失败时修复配置或网络后运行 `python3 SKILL/scripts/dedup.py next <工作目录>` 继续，已完成的步骤不会重做。

**4. 汇报**

完成后读取 `<工作目录>/report.md`，按 [report-guide.md](report-guide.md) 向用户汇报。脚本打印的警告要告诉用户。

## 批量查重

对文件夹中每个文档分别 prepare，工作目录放在同一个上级目录下，例如 `dedup-work/<文件名>`，然后逐篇（或用子代理并行）完成第 3 步。全部结束后汇总：

```bash
python3 SKILL/scripts/dedup.py summary dedup-work
```

## 其他命令

- `python3 SKILL/scripts/dedup.py status <工作目录>`：查看进度和每个待办的状态。
- `prepare ... --force`：丢弃已有进度重新开始。
