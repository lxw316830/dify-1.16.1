"""项目查重工作流各代码节点的移植版本，逻辑与 Dify 工作流保持一致。"""

import re


# ---- 工作流节点：章节分块 ----

def normalize_heading_number(number):
    return re.sub(r"\s+", "", str(number or "").replace("．", ".")).strip(".")


def clean_title(title):
    title = str(title or "").replace("\\t", "\t").replace("　", " ").strip()
    title = re.sub(r"^[.．、\s]+", "", title).strip()
    title = re.sub(r"[\t ]+\d{1,4}\s*$", "", title).strip()
    title = re.sub(r"(?<=[\u4e00-\u9fa5）)])\d{1,4}$", "", title).strip()
    title = re.sub(r"^[.．、\s]+", "", title).strip()
    return title


def title_text(label):
    return clean_title(re.sub(r"^\d+(?:\.\d+)*[\t .．]*", "", str(label or "").strip()))


def title_key(title):
    return re.sub(r"\s+", "", clean_title(title))


def parse_numbered_heading_line(line):
    line = str(line or "").strip()
    match = re.match(
        r"^(\d{1,2}(?:\s*[.．]\s*\d{1,3}){0,8})\s*(?:[.．]\s*)?(.+?)\s*$",
        line,
    )
    if not match:
        return None

    number = normalize_heading_number(match.group(1))
    title = clean_title(match.group(2))
    if not number or not title or title.isdigit():
        return None
    return number, title


def looks_like_toc_entry(line):
    line = str(line or "").strip()
    return bool(re.search(r"(?:[\t ]+\d{1,4}|(?<=[\u4e00-\u9fa5）)])\d{1,4})\s*$", line))


# textutil 把 Word 目录留成 HYPERLINK/PAGEREF 域。编号在域里，正文标题是纯文字。
_FIELD_TOC_HEADING = re.compile(
    r"HYPERLINK\s+\\l\s+_Toc\d+\s+(.+?)\s+PAGEREF\s+_Toc\d+\s+\\h(?:\s+\S+)?\s*$",
    re.IGNORECASE,
)


def field_toc_heading_text(line):
    match = _FIELD_TOC_HEADING.search(str(line or "").strip())
    if not match:
        return ""
    return match.group(1).replace("\t", " ").strip()


def is_likely_heading(number, title):
    title = clean_title(title)
    if not title or title.isdigit():
        return False
    if title[0] in ".．。 、,，;；:：)）（(、":
        return False
    if not re.search(r"[\u4e00-\u9fa5A-Za-z]", title):
        return False

    depth = normalize_heading_number(number).count(".") + 1
    if depth == 1 and re.search(r"[。；;]", title):
        return False

    if depth == 1:
        top_level_markers = (
            "总论",
            "现状",
            "需求",
            "方案",
            "设计",
            "设备",
            "材料",
            "清册",
            "估算",
            "概述",
            "投资",
            "结论",
            "附件",
            "项目",
            "建设",
        )
        if not any(marker in title for marker in top_level_markers):
            return False

    return True


def build_toc_heading_map(text):
    headings = {}
    for line in str(text or "").splitlines():
        field_heading = field_toc_heading_text(line)
        source = field_heading or line
        parsed = parse_numbered_heading_line(source)
        # 域代码本身就是目录项。普通行仍要行尾页码，避免把正文里的编号句子当成目录。
        if not parsed or (not field_heading and not looks_like_toc_entry(line)):
            continue

        number, title = parsed
        if not is_likely_heading(number, title):
            continue

        key = title_key(title)
        values = headings.setdefault(key, [])
        if (number, title) not in values:
            values.append((number, title))
    return headings


def plain_toc_title_keys(text):
    keys = set()
    for line in str(text or "").splitlines():
        if field_toc_heading_text(line):
            continue
        parsed = parse_numbered_heading_line(line)
        if not parsed or not looks_like_toc_entry(line):
            continue
        number, title = parsed
        if is_likely_heading(number, title):
            keys.add(title_key(title))
    return keys


def numbered_body_titles(text):
    titles = set()
    for line in str(text or "").splitlines():
        # 目录行（含页码或 HYPERLINK 域）不是正文标题，不能挡住同名标题补编号。
        if field_toc_heading_text(line) or looks_like_toc_entry(line):
            continue
        parsed = parse_numbered_heading_line(line)
        if parsed and is_likely_heading(parsed[0], parsed[1]):
            titles.add(title_key(parsed[1]))
    return titles


def restore_auto_numbered_headings(text):
    toc_headings = build_toc_heading_map(text)
    if not toc_headings:
        return text

    # 正文已经写了编号时，目录域不再把同名纯文字行补成第二条标题。
    # 普通目录行仍按原规则补，重复出现的标题继续沿用目录编号。
    already_numbered = numbered_body_titles(text)
    plain_titles = plain_toc_title_keys(text)
    restored = []
    used_counts = {}
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if not stripped or field_toc_heading_text(stripped):
            restored.append(line)
            continue

        parsed = parse_numbered_heading_line(stripped)
        if parsed and is_likely_heading(parsed[0], parsed[1]):
            restored.append(f"{parsed[0]} {parsed[1]}")
            continue

        key = title_key(stripped)
        candidates = toc_headings.get(key, [])
        if not candidates or len(stripped) > 120 or (key in already_numbered and key not in plain_titles):
            restored.append(line)
            continue

        used_index = min(used_counts.get(key, 0), len(candidates) - 1)
        number, title = candidates[used_index]
        used_counts[key] = used_counts.get(key, 0) + 1
        restored.append(f"{number} {title}")

    return "\n".join(restored)


def section_titles(section_path):
    return [title_text(part) for part in str(section_path or "").split(" > ") if title_text(part)]


def extract_project_name(text):
    match = re.search(r"(?m)^\s*项目名称\s*[：:]\s*(.+?)\s*$", str(text or "")[:10000])
    return match.group(1).strip() if match else ""


def is_design_project(project_name):
    name = str(project_name or "")
    return "设计开发" in name and "项目" in name


def split_long_text(content, limit=12000):
    results = []
    current = ""

    for line in str(content or "").splitlines(keepends=True):
        if len(current) + len(line) <= limit:
            current += line
            continue

        if current.strip():
            results.append(current.strip())

        current = line
        while len(current) > limit:
            results.append(current[:limit].strip())
            current = current[limit:]

    if current.strip():
        results.append(current.strip())

    return results


def parse_markdown_row(line):
    line = str(line or "").strip()
    if not line.startswith("|"):
        return []
    return [cell.strip() for cell in line.strip("|").split("|")]


def normalize_function_status(status):
    status = str(status or "").strip()
    if status == "新增":
        return "新增"
    if status in ("非新增", "完善", "优化", "改造"):
        return "非新增"
    return "不明"


def split_function_heading_title(title):
    title = clean_title(title)
    match = re.search(r"[（(](新增|非新增|完善|优化|改造)[）)]\s*$", title)
    if not match:
        return title, ""
    name = title[:match.start()].strip()
    return name, normalize_function_status(match.group(1))


def parse_numbered_function_headings(text):
    lines = str(text or "").splitlines()
    heading_pattern = re.compile(r"^\s*(4\.3\.2(?:\.\d+)+)\s*([^\n|]{1,120})\s*$")
    entries = []

    for index, line in enumerate(lines):
        stripped = line.strip()
        match = heading_pattern.match(stripped)
        if not match:
            continue

        name, status = split_function_heading_title(match.group(2))
        if not status:
            continue

        number = normalize_heading_number(match.group(1))
        entries.append({
            "index": index,
            "number": number,
            "parts": number.split("."),
            "name": name,
            "status": status,
            "line": stripped,
        })

    if not entries:
        return []

    def has_child(entry):
        prefix = entry["number"] + "."
        return any(other["number"].startswith(prefix) for other in entries)

    def next_body_line(start_index):
        for line in lines[start_index + 1:]:
            stripped = line.strip()
            if not stripped:
                continue
            if heading_pattern.match(stripped) or re.match(r"^\d+(?:\.\d+){0,8}\s*", stripped):
                return ""
            if stripped.startswith("|") or re.match(r"^[（(]?\d+[）)]\s*$", stripped):
                continue
            return stripped
        return ""

    function_items = []
    for entry in entries:
        depth = len(entry["parts"])
        if depth < 5 and has_child(entry):
            continue
        if depth > 5:
            continue

        details = []
        prefix = entry["number"] + "."
        for child in entries:
            if len(child["parts"]) != depth + 1 or not child["number"].startswith(prefix):
                continue
            detail_body = next_body_line(child["index"])
            detail = f"{child['name']}：{detail_body}" if detail_body else child["name"]
            if detail and detail not in details:
                details.append(detail)

        if not details:
            body = next_body_line(entry["index"])
            if body:
                details.append(body)

        function_items.append({
            "功能名称": entry["name"],
            "功能描述": "；".join(details),
            "功能状态": entry["status"],
            "出处类型": "标题",
            "章节路径": entry["line"],
            "表名": "编号功能清单",
        })

        if len(function_items) >= 100:
            break

    return function_items


def parse_function_table(text, enabled):
    if not enabled:
        return [], -1

    matches = list(re.finditer(r"(?m)^表[-－]?\s*\d+\s+本期系统开发功能清单表\s*$", text))
    if not matches:
        return parse_numbered_function_headings(text), -1

    table_match = matches[-1]
    table_name = table_match.group(0).strip()
    lines = text[table_match.end():].splitlines()

    headers = []
    rows = []
    table_started = False
    for line in lines:
        cells = parse_markdown_row(line)
        if not cells:
            if table_started and line.strip():
                break
            continue

        if "一级功能" in cells and "二级功能" in cells:
            headers = cells
            table_started = True
            continue

        if not headers:
            continue
        if all(re.fullmatch(r"-+", cell or "-") for cell in cells):
            continue
        if len(cells) != len(headers):
            continue

        rows.append(dict(zip(headers, cells)))

    groups = {}
    for row in rows:
        first = row.get("一级功能", "").strip()
        second = row.get("二级功能", "").strip()
        third = row.get("三级功能", "").strip()
        description = row.get("功能点说明", "").strip()
        status = row.get("状态", "").strip()

        if not second:
            continue
        if status not in ("新增", "非新增"):
            status = "不明"

        combined = third + description
        if any(word in combined for word in ("接口", "对接", "系统集成", "数据集成", "服务集成", "数据交换")):
            continue

        key = (first, second, status)
        group = groups.setdefault(key, {
            "功能名称": second,
            "功能状态": status,
            "功能描述明细": [],
        })

        if third and description:
            detail = f"{third}：{description}"
        else:
            detail = third or description

        if detail and detail not in group["功能描述明细"]:
            group["功能描述明细"].append(detail)

    function_items = []
    for group in groups.values():
        function_items.append({
            "功能名称": group["功能名称"],
            "功能描述": "；".join(group["功能描述明细"]),
            "功能状态": group["功能状态"],
            "出处类型": "表格",
            "章节路径": table_name,
            "表名": table_name,
        })

    if not function_items:
        return parse_numbered_function_headings(text), -1

    return function_items, table_match.start()


def classify_section(number, title, section_path):
    titles = section_titles(section_path)
    root_title = titles[0] if titles else title_text(title)
    current_title = titles[-1] if titles else title_text(title)

    skip_markers = (
        "集成需求",
        "非功能需求",
        "集成工作",
        "系统实施",
        "实施范围",
        "技术方案",
        "技术路线",
        "非功能要求",
        "数据需求",
        "数据需求及获取方式",
        "建设现状",
        "工作步骤",
        "关键技术",
        "工程范围",
    )
    exact_skip_titles = (
        "项目管理",
        "项目岗位要求",
        "项目进度",
        "项目会议",
        "项目培训需求",
        "项目分包要求",
        "培训内容",
    )

    if any(marker in item for item in titles for marker in skip_markers):
        return ""
    if any(item in exact_skip_titles for item in titles):
        return ""

    if any(marker in current_title for marker in ("项目目标", "预期成效", "预期成果")):
        return "requirement"

    if (
        any(marker in root_title for marker in ("项目需求分析", "需求分析"))
        or any(marker in current_title for marker in ("业务建设需求", "业务需求", "本期建设需求"))
    ):
        return "requirement"

    if any(marker in item for item in titles for marker in ("项目内容", "建设内容")):
        if len(normalize_heading_number(number).split(".")) <= 8:
            return "project_content"

    if any(marker in root_title for marker in ("项目方案", "建设方案")):
        if any(marker in current_title for marker in ("工作内容", "建设任务")):
            if len(normalize_heading_number(number).split(".")) <= 10:
                return "project_content"

    return ""


def chunk_document(text, project_name_override=""):
    text = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = restore_auto_numbered_headings(text)

    project_name = str(project_name_override or "").strip() or extract_project_name(text)
    is_design = is_design_project(project_name)
    function_items, function_table_start = parse_function_table(text, is_design)

    llm_text = text[:function_table_start] if function_table_start >= 0 else text
    heading_pattern = re.compile(r"(?m)^[ \t]*(\d+(?:\.\d+){0,8})[ \t]*([^\n|]{1,120})$")
    matches = [
        match
        for match in heading_pattern.finditer(llm_text)
        if is_likely_heading(match.group(1), clean_title(match.group(2)))
    ]

    heading_names = {}
    chunks = []
    chunk_sources = []
    must_extract_requirement_ids = []

    for index, match in enumerate(matches):
        number = normalize_heading_number(match.group(1))
        title = clean_title(match.group(2))
        heading_names[number] = f"{number} {title}"

        end = matches[index + 1].start() if index + 1 < len(matches) else len(llm_text)
        body = llm_text[match.end():end].strip()

        path_parts = []
        number_parts = number.split(".")
        for depth in range(1, len(number_parts) + 1):
            parent = ".".join(number_parts[:depth])
            if parent in heading_names:
                path_parts.append(heading_names[parent])

        section_path = " > ".join(path_parts)
        extract_mode = classify_section(number, title, section_path)
        if not extract_mode or not body:
            continue

        for part in split_long_text(body):
            if not part:
                continue

            chunk_id = f"chunk_{len(chunks) + 1:04d}"
            chunk = {
                "chunk_id": chunk_id,
                "section_path": section_path,
                "extract_mode": extract_mode,
                "content": part,
            }
            chunks.append(chunk)
            chunk_sources.append(chunk)

            background_only = ("业务总体情况" in part) and ("本期建设需求" not in part)
            if extract_mode == "requirement" and not background_only and (
                "本期建设需求" in part
                or "项目需求分析" in section_path
                or "业务建设需求" in section_path
                or "业务需求" in section_path
                or "项目目标" in section_path
                or "预期成效" in section_path
                or "预期成果" in section_path
            ):
                must_extract_requirement_ids.append(chunk_id)

    batches = []
    current = []
    current_chars = 0
    for chunk in chunks:
        chunk_chars = len(chunk["content"])
        if current and (len(current) >= 8 or current_chars + chunk_chars > 22000):
            batches.append({
                "batch_id": f"batch_{len(batches) + 1}",
                "chunks": current,
            })
            current = []
            current_chars = 0

        current.append(chunk)
        current_chars += chunk_chars

    if current:
        batches.append({
            "batch_id": f"batch_{len(batches) + 1}",
            "chunks": current,
        })

    return {
        "batches": batches,
        "expected_chunk_ids": [chunk["chunk_id"] for chunk in chunks],
        "must_extract_requirement_ids": must_extract_requirement_ids,
        "chunk_sources": chunk_sources,
        "project_name": project_name,
        "is_design_development_project": is_design,
        "function_items": function_items if is_design else [],
    }


# ---- 工作流节点：提取结果合并校验 ----

def normalize_with_map(text):
    punct_map = {
        "“": "\"", "”": "\"",
        "‘": "'", "’": "'",
        "：": ":", "；": ";",
        "，": ",", "。": ".",
        "（": "(", "）": ")",
        "【": "[", "】": "]",
        "、": ",",
        "－": "-", "—": "-", "–": "-",
        "\u3000": "",
    }

    chars = []
    index_map = []

    for index, char in enumerate(str(text or "")):
        if char.isspace():
            continue

        normalized = punct_map.get(char, char)

        for item in normalized:
            chars.append(item)
            index_map.append(index)

    return "".join(chars), index_map


def recover_exact_text(candidate, source_text):
    candidate = str(candidate or "").strip()
    source_text = str(source_text or "")

    if not candidate:
        return ""

    if candidate in source_text:
        return candidate

    normalized_candidate, _ = normalize_with_map(candidate)
    normalized_source, source_index_map = normalize_with_map(source_text)

    if len(normalized_candidate) < 12:
        return ""

    position = normalized_source.find(normalized_candidate)

    if position < 0:
        return ""

    start = source_index_map[position]
    end = source_index_map[position + len(normalized_candidate) - 1] + 1

    return source_text[start:end].strip()


def should_recover_requirement_summary(source):
    if not isinstance(source, dict):
        return False

    if source.get("extract_mode") != "requirement":
        return False

    content = str(source.get("content") or "").strip()
    if not content or len(content) > 500:
        return False

    summary_markers = (
        "项目建设包括",
        "建设内容包括",
        "建设包括",
        "业务场景",
        "功能应用",
        "需要开发",
        "新增",
        "构建",
        "实现",
    )
    return any(marker in content for marker in summary_markers)


def recover_project_content_summary(source):
    if not isinstance(source, dict):
        return ""

    if source.get("extract_mode") != "project_content":
        return ""

    lines = []
    for raw_line in str(source.get("content") or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("|"):
            continue
        if re.match(r"^[（(]?\d+[）)]\s*$", line):
            continue
        lines.append(line)

    if not lines:
        return ""

    summary_markers = (
        "主要包括",
        "完成",
        "本项目为",
        "项目建设包括",
        "建设内容包括",
        "建设包括",
        "系统开发工作",
    )

    for line in lines:
        if len(line) <= 500 and any(marker in line for marker in summary_markers):
            return line

    for line in lines:
        if len(line) <= 500 and not re.match(r"^[（(]?\d+[）)]", line):
            return line

    return ""

def merge_extractions(
    batch_results,
    expected_chunk_ids,
    must_extract_requirement_ids,
    chunk_sources,
    project_name,
    is_design,
    function_items,
):
    source_map = {
        item.get("chunk_id"): item
        for item in chunk_sources or []
        if isinstance(item, dict)
    }

    processed = set()
    valid_requirement_chunks = set()
    valid_content_chunks = set()
    requirements = []
    contents = []
    invalid_items = []

    def add_requirement(chunk_id, requirement_type, content, source, source_type="正文", table_name=""):
        valid_requirement_chunks.add(chunk_id)
        requirements.append({
            "需求类型": requirement_type or "需求内容",
            "内容": content,
            "出处类型": source_type or "正文",
            "章节路径": source.get("section_path") or "",
            "表名": table_name or "",
        })

    for result in batch_results or []:
        if not isinstance(result, dict):
            continue

        processed.update(result.get("processed_chunk_ids") or [])

        for item in result.get("requirements") or []:
            if not isinstance(item, dict):
                continue

            chunk_id = item.get("chunk_id")
            content = str(item.get("content") or "").strip()
            source = source_map.get(chunk_id, {})
            source_content = str(source.get("content") or "")

            exact_content = recover_exact_text(content, source_content)

            if not exact_content:
                # 临时放行：出版前先不因模型改写或拼接导致的原文不匹配阻断流程。
                exact_content = content

            add_requirement(
                chunk_id,
                item.get("requirement_type") or "需求内容",
                exact_content,
                source,
                item.get("source_type") or "正文",
                item.get("table_name") or "",
            )

        for item in result.get("contents") or []:
            if not isinstance(item, dict):
                continue

            chunk_id = item.get("chunk_id")
            content = str(item.get("content") or "").strip()
            source = source_map.get(chunk_id, {})
            source_content = str(source.get("content") or "")

            exact_content = recover_exact_text(content, source_content)

            if not exact_content:
                # 临时放行：出版前先不因模型改写或拼接导致的原文不匹配阻断流程。
                exact_content = content

            valid_content_chunks.add(chunk_id)
            contents.append({
                "内容": exact_content,
                "出处类型": item.get("source_type") or "正文",
                "章节路径": source.get("section_path") or "",
                "表名": item.get("table_name") or "",
            })

    for chunk_id in must_extract_requirement_ids or []:
        if chunk_id in valid_requirement_chunks:
            continue

        source = source_map.get(chunk_id, {})
        if should_recover_requirement_summary(source):
            add_requirement(
                chunk_id,
                "需求内容",
                str(source.get("content") or "").strip(),
                source,
            )


    for chunk_id, source in source_map.items():
        if chunk_id in valid_content_chunks:
            continue

        recovered_content = recover_project_content_summary(source)
        if not recovered_content:
            continue

        valid_content_chunks.add(chunk_id)
        contents.append({
            "内容": recovered_content,
            "出处类型": "正文",
            "章节路径": source.get("section_path") or "",
            "表名": "",
        })
    def deduplicate(items, fields):
        results = []
        seen = set()

        for item in items:
            key = tuple(str(item.get(field) or "") for field in fields)

            if key in seen:
                continue

            seen.add(key)
            results.append(item)

        return results

    missing_processed = [
        chunk_id
        for chunk_id in expected_chunk_ids or []
        if chunk_id not in processed
    ]

    missing_requirements = [
        chunk_id
        for chunk_id in must_extract_requirement_ids or []
        if chunk_id not in valid_requirement_chunks
    ]

    errors = []

    if not project_name:
        errors.append("未识别正式项目名称")

    if missing_processed:
        errors.append(
            "模型未处理分块：" + "、".join(missing_processed)
        )

    if missing_requirements:
        errors.append(
            "需求分块未提取内容：" + "、".join(missing_requirements)
        )

    # 临时放行：保留上方 exact_content 修复逻辑，但不再因原文不匹配写入 error_msg。
    # if invalid_items:
    #     errors.append(
    #         "模型输出不是原文连续摘录：" + "、".join(invalid_items)
    #     )

    return {
        "json_obj": {
            "项目需求分析章节": {
                "项目名称": project_name or "",
                "项目需求": deduplicate(
                    requirements,
                    ("需求类型", "内容", "章节路径", "表名"),
                ),
            },
            "项目内容章节": {
                "项目内容": deduplicate(
                    contents,
                    ("内容", "章节路径", "表名"),
                ),
                "功能清单": (
                    deduplicate(
                        function_items or [],
                        (
                            "功能名称",
                            "功能描述",
                            "功能状态",
                            "章节路径",
                            "表名",
                        ),
                    )
                    if is_design
                    else []
                ),
            },
        },
        "error_msg": "；".join(errors),
    }


# ---- 工作流节点：代码执行 2：拆分查重任务 ----

def build_tasks(arg1):
    data = arg1 or {}
    demand_section = data.get("项目需求分析章节", data)
    content_section = data.get("项目内容章节", data)
    demand_section = demand_section if isinstance(demand_section, dict) else {}
    content_section = content_section if isinstance(content_section, dict) else {}

    project_name = str(demand_section.get("项目名称") or data.get("项目名称") or "").strip()
    is_design_development_project = "设计开发" in project_name and "项目" in project_name
    requirement_tasks = []
    content_tasks = []

    def clean(value):
        return str(value or "").replace("\\p", "\n").strip()

    def is_noise_part(part):
        part = str(part or "").strip()
        if not part:
            return True

        # 纯章节标题，例如：4.3.1.2.2.1 工程项目物资后评价（新增）
        if re.match(r"^\d+(?:\.\d+){1,8}\s*[^\n]{1,100}$", part):
            return True

        # Markdown 表格分隔线，例如：| --- | --- |
        if re.match(r"^\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?$", part):
            return True

        # 只有序号的行，例如：（1）、1）
        if re.match(r"^[（(]?\d+[）)]?$", part):
            return True

        # 太短且没有业务含义的碎片
        if len(part) < 12 and not re.search(
            r"需求|建设|功能|指标|系统|项目|监控|分析|统计|数据|评价|管理|采购|供应|库存|合同",
            part,
        ):
            return True

        return False

    def split_task_text(text, max_len=1200):
        text = clean(text)
        if not text:
            return []

        # 优先按空行切成自然段；段内普通换行保留，避免把上下文拆散。
        blocks = re.split(r"\n\s*\n+", text)
        results = []

        for block in blocks:
            block = "\n".join(line.strip() for line in block.splitlines() if line.strip())
            if is_noise_part(block):
                continue

            if len(block) <= max_len:
                results.append(block)
                continue

            # 长段再按中文句末切，避免单条 query 太长。
            sentences = re.split(r"(?<=[。；;])", block)
            current = ""

            for sentence in sentences:
                sentence = sentence.strip()
                if not sentence:
                    continue

                if len(current) + len(sentence) > max_len and current:
                    if not is_noise_part(current):
                        results.append(current.strip())
                    current = sentence
                else:
                    current += sentence

            if current.strip() and not is_noise_part(current):
                results.append(current.strip())

        return results

    def build_query(part, source_section):
        section_tail = str(source_section or "").split(">")[-1].strip()
        if section_tail and section_tail not in part:
            return f"{section_tail}\n{part}"
        return part

    # 规则处理（新增）
    def extract_project_content_compare_text(text):
        text = clean(text)
        if not text:
            return ""

        patterns = [
            r"(?:完成|建设|开发|改造|实现|新增|完善|优化)([^。；;\n]{8,400}?)(?:等\s*\d+\s*个\s*(?:一|二|三)?级功能)",
            r"(?:主要包括|包括|包含|涵盖)([^。；;\n]{8,400}?)(?:等\s*\d+\s*个\s*(?:一|二|三)?级功能)",
        ]

        for pattern in patterns:
            match = re.search(pattern, text)
            if not match:
                continue

            candidate = match.group(1).strip(" ，,、；;。")
            parts = [
                part.strip(" ，,、；;。")
                for part in re.split(r"[、,，；;]", candidate)
                if part.strip(" ，,、；;。")
            ]

            cleaned = []
            for part in parts:
                part = re.sub(r"^(完成|建设|开发|改造|实现|新增|完善|优化)", "", part).strip()
                part = re.sub(r"(等)?\d+个.*$", "", part).strip()
                if len(part) < 4 or len(part) > 50:
                    continue
                if part in ("系统开发工作", "开发工作", "建设工作", "相关工作"):
                    continue
                if not re.search(r"[\u4e00-\u9fa5A-Za-z]", part):
                    continue
                if part not in cleaned:
                    cleaned.append(part)

            if len(cleaned) >= 2:
                return "、".join(cleaned)

        return ""

    # -----------------------

    def add_task(target, query_type, source_field, item, function_status=""):
        if isinstance(item, dict):
            text = clean(item.get("内容"))

            if query_type == "function_item":
                name = clean(item.get("功能名称"))
                description = clean(item.get("功能描述"))
                text = "；".join(part for part in (name, description) if part)
                function_status = clean(item.get("功能状态")) or "不明"

            source_type = clean(item.get("出处类型"))
            source_section = clean(item.get("章节路径"))
            source_table = clean(item.get("表名"))
        else:
            text = clean(item)
            source_type = ""
            source_section = ""
            source_table = ""

        # 新增处理逻辑
        compare_text = text
        if query_type == "project_content":
            compare_text = extract_project_content_compare_text(text) or text

        # for part in split_task_text(text):
        for part in split_task_text(compare_text):
            query = build_query(part, source_section)
            target.append({
                "query_type": query_type,
                "source_field": source_field,
                "query": query,
                "new_content": part,
                "new_source_type": source_type,
                "new_source_section": source_section,
                "new_source_table": source_table,
                "function_status": function_status,
                "new_project_name": project_name,
                "is_design_development_project": is_design_development_project,
            })

    project_requirements = demand_section.get("项目需求")
    if project_requirements is None:
        project_requirements = demand_section.get("需求内容") or []
    if not isinstance(project_requirements, list):
        project_requirements = [project_requirements]

    for item in project_requirements:
        requirement_type = item.get("需求类型") if isinstance(item, dict) else "需求内容"
        add_task(requirement_tasks, "project_requirement", str(requirement_type or "需求内容"), item)

    project_content = content_section.get("项目内容") or []
    if not isinstance(project_content, list):
        project_content = [project_content]

    for item in project_content:
        add_task(content_tasks, "project_content", "项目内容", item)

    if is_design_development_project:
        function_items = content_section.get("功能清单") or []
        if not isinstance(function_items, list):
            function_items = [function_items]

        for item in function_items:
            add_task(content_tasks, "function_item", "功能清单", item)

    return {
        "xmxq": requirement_tasks,
        "xmnr": content_tasks,
        "new_project_name": project_name,
        "is_design_development_project": is_design_development_project,
    }


# ---- 工作流节点：项目需求/内容证据包装 ----

def wrap_evidence(task, hits):
    task = task if isinstance(task, dict) else {}
    wrapped = []
    for hit in hits or []:
        if not isinstance(hit, dict):
            continue
        metadata = hit.get("metadata") if isinstance(hit.get("metadata"), dict) else {}
        doc_metadata = metadata.get("doc_metadata") if isinstance(metadata.get("doc_metadata"), dict) else {}
        document_name = metadata.get("document_name") or hit.get("title") or ""
        source_project = doc_metadata.get("project_name") or document_name or "无法确定具体项目名称"
        score = metadata.get("score")
        if score is None:
            score = hit.get("score", 0)
        try:
            score = round(float(score), 4)
        except (TypeError, ValueError):
            score = 0
        wrapped.append({
            "query_type": task.get("query_type", ""),
            "source_field": task.get("source_field", ""),
            "new_content": task.get("new_content") or task.get("query") or "",
            "new_source_type": task.get("new_source_type", ""),
            "new_source_section": task.get("new_source_section", ""),
            "new_source_table": task.get("new_source_table", ""),
            "function_status": task.get("function_status", ""),
            "new_project_name": task.get("new_project_name", ""),
            "is_design_development_project": task.get("is_design_development_project", False),
            "source_project": source_project,
            "matched_content": hit.get("content") or "",
            "document_name": document_name,
            "historical_section_path": (
                doc_metadata.get("section_path")
                or doc_metadata.get("section")
                or metadata.get("section_path")
                or ""
            ),
            "historical_table_name": doc_metadata.get("table_name") or metadata.get("table_name") or "",
            "segment_id": metadata.get("segment_id") or "",
            "retrieval_score": score,
        })
    return {"wrapped_results": wrapped}


# ---- 工作流节点：候选证据批处理 ----

def batch_candidates(xmxq_items, xmnr_items):
    BATCH_SIZE = 5
    MAX_BATCH_CHARS = 16000
    MAX_CANDIDATES = 3
    MAX_HITS_PER_CANDIDATE = 2
    MAX_HIT_CHARS = 1200

    def flatten(value):
        result = []
        if isinstance(value, list):
            for item in value:
                result.extend(flatten(item))
        elif isinstance(value, dict):
            if isinstance(value.get("xmxq_items"), list) or isinstance(value.get("xmnr_items"), list):
                result.extend(flatten(value.get("xmxq_items") or []))
                result.extend(flatten(value.get("xmnr_items") or []))
            else:
                result.append(value)
        return result

    def clip_text(text, limit):
        text = str(text or "").strip()
        if len(text) <= limit:
            return text
        excerpt = text[:limit]
        cut_at = max(
            excerpt.rfind("。"),
            excerpt.rfind("；"),
            excerpt.rfind("\n"),
            excerpt.rfind("."),
        )
        if cut_at >= int(limit * 0.6):
            return excerpt[:cut_at + 1].strip()
        return excerpt.strip()

    def to_score(value):
        try:
            return round(float(value), 4)
        except (TypeError, ValueError):
            return 0

    def build_task_from_flat(item, task_id):
        return {
            "task_id": task_id,
            "source_key": task_id,
            "query_type": item.get("query_type", ""),
            "source_field": item.get("source_field", ""),
            "new_content": item.get("new_content", ""),
            "new_source_type": item.get("new_source_type", ""),
            "new_source_section": item.get("new_source_section", ""),
            "new_source_table": item.get("new_source_table", ""),
            "function_status": item.get("function_status", ""),
            "new_project_name": item.get("new_project_name", ""),
            "is_design_development_project": item.get("is_design_development_project", False),
        }

    def flat_task_key(item):
        return (
            str(item.get("query_type") or ""),
            str(item.get("source_field") or ""),
            str(item.get("new_content") or ""),
            str(item.get("new_source_section") or ""),
            str(item.get("new_source_table") or ""),
            str(item.get("function_status") or ""),
        )

    def add_flat_hit(group, item):
        source_project = str(item.get("source_project") or "无法确定具体项目名称")
        document_name = str(item.get("document_name") or item.get("source_project") or "")
        candidate_key = (source_project, document_name)

        candidate = group["candidate_map"].setdefault(candidate_key, {
            "source_project": source_project,
            "document_name": document_name,
            "historical_section_path": item.get("historical_section_path", ""),
            "historical_table_name": item.get("historical_table_name", ""),
            "best_retrieval_score": 0,
            "hits": [],
            "seen_hits": set(),
        })

        score = to_score(item.get("retrieval_score"))
        matched_content = clip_text(item.get("matched_content"), MAX_HIT_CHARS)
        segment_id = str(item.get("segment_id") or "")
        hit_key = (segment_id, matched_content)

        if not matched_content or hit_key in candidate["seen_hits"]:
            return

        candidate["seen_hits"].add(hit_key)
        candidate["best_retrieval_score"] = max(candidate["best_retrieval_score"], score)
        candidate["hits"].append({
            "segment_id": segment_id,
            "matched_content": matched_content,
            "retrieval_score": score,
            "historical_section_path": item.get("historical_section_path", ""),
            "historical_table_name": item.get("historical_table_name", ""),
        })

    def finalize_flat_groups(groups):
        items = []
        for group in groups.values():
            candidates = sorted(
                group["candidate_map"].values(),
                key=lambda candidate: candidate.get("best_retrieval_score", 0),
                reverse=True,
            )

            candidate_groups = []
            for index, candidate in enumerate(candidates[:MAX_CANDIDATES], start=1):
                hits = sorted(
                    candidate.get("hits") or [],
                    key=lambda hit: hit.get("retrieval_score", 0),
                    reverse=True,
                )[:MAX_HITS_PER_CANDIDATE]

                candidate_groups.append({
                    "candidate_id": f"candidate_{index}",
                    "source_project": candidate.get("source_project", ""),
                    "document_name": candidate.get("document_name", ""),
                    "historical_section_path": candidate.get("historical_section_path", ""),
                    "historical_table_name": candidate.get("historical_table_name", ""),
                    "best_retrieval_score": candidate.get("best_retrieval_score", 0),
                    "hits": hits,
                })

            if candidate_groups:
                items.append({
                    "task": group["task"],
                    "candidate_groups": candidate_groups,
                    "candidate_count": len(candidate_groups),
                })
        return items

    def normalize_existing_item(raw):
        item = raw.get("evaluation_item") if isinstance(raw.get("evaluation_item"), dict) else raw
        if not isinstance(item, dict):
            return None

        task = item.get("task")
        task = task if isinstance(task, dict) else {}
        candidates = item.get("candidate_groups") or []

        if not isinstance(candidates, list) or not candidates:
            return None

        if not task.get("task_id"):
            task["task_id"] = task.get("source_key") or f"task_existing_{abs(hash(str(task.get('new_content') or ''))) % 1000000}"
        if not task.get("source_key"):
            task["source_key"] = task.get("task_id")

        return {
            "task": task,
            "candidate_groups": candidates,
            "candidate_count": len(candidates),
        }

    def estimate_item_chars(item):
        task = item.get("task") or {}
        total = len(str(task.get("new_content") or ""))

        for candidate in item.get("candidate_groups") or []:
            total += len(str(candidate.get("source_project") or ""))
            total += len(str(candidate.get("document_name") or ""))
            for hit in candidate.get("hits") or []:
                total += len(str(hit.get("matched_content") or ""))

        return total

    raw_items = flatten(xmxq_items) + flatten(xmnr_items)

    prepared = []
    flat_groups = {}
    skipped_empty_count = 0

    for raw in raw_items:
        if not isinstance(raw, dict):
            continue

        existing_item = normalize_existing_item(raw)
        if existing_item:
            prepared.append(existing_item)
            continue

        matched_content = str(raw.get("matched_content") or "").strip()
        new_content = str(raw.get("new_content") or "").strip()

        if not matched_content or not new_content:
            skipped_empty_count += 1
            continue

        key = flat_task_key(raw)
        group = flat_groups.get(key)

        if group is None:
            task_id = f"task_{len(flat_groups) + 1:04d}"
            group = {
                "task": build_task_from_flat(raw, task_id),
                "candidate_map": {},
            }
            flat_groups[key] = group

        add_flat_hit(group, raw)

    prepared.extend(finalize_flat_groups(flat_groups))

    deduped = []
    seen = set()

    for item in prepared:
        task = item.get("task") or {}
        key = (
            str(task.get("task_id") or ""),
            str(task.get("query_type") or ""),
            str(task.get("new_content") or ""),
            str(task.get("new_source_section") or ""),
            str(task.get("new_source_table") or ""),
        )
        if key in seen:
            continue
        seen.add(key)

        if not item.get("candidate_groups"):
            skipped_empty_count += 1
            continue

        item["estimated_chars"] = estimate_item_chars(item)
        deduped.append(item)

    batches = []
    current = []
    current_chars = 0

    for item in deduped:
        item_chars = item.get("estimated_chars", 0)
        should_flush = current and (
            len(current) >= BATCH_SIZE
            or current_chars + item_chars > MAX_BATCH_CHARS
        )

        if should_flush:
            batches.append({
                "batch_id": f"batch_{len(batches) + 1}",
                "items": current,
            })
            current = []
            current_chars = 0

        current.append(item)
        current_chars += item_chars

    if current:
        batches.append({
            "batch_id": f"batch_{len(batches) + 1}",
            "items": current,
        })

    return {
        "batches": batches,
        "batch_count": len(batches),
        "candidate_item_count": len(deduped),
        "skipped_empty_count": skipped_empty_count,
    }


# ---- 工作流节点：批量评估结果包装 ----

def wrap_assessment(batch, assessment):
    batch = batch if isinstance(batch, dict) else {}
    assessment = assessment if isinstance(assessment, dict) else {}

    item_map = {}

    def is_true(value):
        if isinstance(value, bool):
            return value
        text = str(value or "").strip().lower()
        return text in ("true", "1", "yes", "y", "是", "相关")

    for item in batch.get("items", []) or []:
        task = item.get("task")
        task = task if isinstance(task, dict) else {}
        task_id = str(task.get("task_id") or task.get("source_key") or "")

        candidates = {}
        for candidate in item.get("candidate_groups") or []:
            if isinstance(candidate, dict):
                candidates[str(candidate.get("candidate_id") or "")] = candidate

        item_map[task_id] = {
            "task": task,
            "candidates": candidates,
        }

    results = []

    for row in assessment.get("results", []) or []:
        if not isinstance(row, dict):
            continue

        task_id = str(row.get("task_id") or "")
        candidate_id = str(row.get("candidate_id") or "")
        mapped = item_map.get(task_id)
        if not mapped:
            continue

        candidate = mapped["candidates"].get(candidate_id)
        if not candidate or not is_true(row.get("is_relevant")):
            continue

        raw_score = str(row.get("similarity_score", 0)).strip().replace("%", "")

        try:
            score = max(0, min(100, float(raw_score)))
        except (TypeError, ValueError):
            score = 0

        if score <= 0:
            continue

        task = mapped["task"]
        query_type = task.get("query_type", "")
        source_field = task.get("source_field", "")
        function_status = task.get("function_status", "")

        if query_type == "project_requirement":
            display_type = source_field or "需求内容"
            threshold = 60 if display_type == "需求内容" else 70
            is_duplicate = score >= threshold
            focus_only = False
        elif query_type == "project_content":
            display_type = "项目内容"
            is_duplicate = score >= 60
            focus_only = False
        else:
            display_type = "新增功能" if function_status == "新增" else "非新增功能"
            is_duplicate = function_status == "新增" and score >= 70
            focus_only = function_status != "新增" and score > 70

        quotes = [
            str(quote).replace("|", "｜").replace("\n", "<br>")
            for quote in row.get("evidence_quotes", [])
            if str(quote).strip()
        ]

        results.append({
            "display_type": display_type,
            "query_type": query_type,
            "source_field": source_field,
            "function_status": function_status,
            "new_content": task.get("new_content", ""),
            "new_source_type": task.get("new_source_type", ""),
            "new_source_section": task.get("new_source_section", ""),
            "new_source_table": task.get("new_source_table", ""),
            "source_project": candidate.get("source_project", ""),
            "document_name": candidate.get("document_name", ""),
            "evidence_quotes": quotes,
            "similarity_score": round(score, 2),
            "is_duplicate": is_duplicate,
            "focus_only": focus_only,
            "reason": row.get("reason", ""),
        })

    return {
        "evaluated_results": results,
    }


# ---- 工作流节点：代码执行 3：汇总排名 ----

def rank_evidence(batch_results):
    def flatten(value):
        result = []
        if isinstance(value, list):
            for item in value:
                result.extend(flatten(item))
        elif isinstance(value, dict):
            if "evaluated_results" in value:
                result.extend(flatten(value.get("evaluated_results")))
            else:
                result.append(value)
        return result

    evidence = flatten(batch_results)

    seen = set()
    all_evidence = []

    for item in evidence:
        key = (
            item.get("display_type"),
            item.get("new_content"),
            item.get("source_project"),
            item.get("document_name"),
        )
        if key in seen:
            continue
        seen.add(key)
        all_evidence.append(item)

    projects = {}

    for item in all_evidence:
        if not item.get("is_duplicate"):
            continue

        name = item.get("source_project") or "无法确定具体项目名称"
        project = projects.setdefault(name, {
            "project_name": name,
            "duplicate_count": 0,
            "highest_similarity": 0,
        })

        project["duplicate_count"] += 1
        project["highest_similarity"] = max(
            project["highest_similarity"],
            item.get("similarity_score", 0),
        )

    ranked_projects = list(projects.values())
    ranked_projects.sort(
        key=lambda item: (
            -item["highest_similarity"],
            -item["duplicate_count"],
            item["project_name"],
        )
    )

    return {
        "ranked_projects": ranked_projects,
        "all_evidence": all_evidence,
    }


# ---- 工作流节点：报告生成 ----

def render_report(ranked_projects, all_evidence):
    ranked_projects = ranked_projects if isinstance(ranked_projects, list) else []
    all_evidence = all_evidence if isinstance(all_evidence, list) else []

    def cell(value):
        text = str(value or "").replace("|", "｜")
        return text.replace("\r\n", "<br>").replace("\n", "<br>").strip()

    def source_of(item):
        section = item.get("new_source_section") or ""
        table = item.get("new_source_table") or ""
        if section and table:
            return f"{section} > {table}"
        return section or table or "出处未识别"

    duplicate_items = [item for item in all_evidence if item.get("is_duplicate")]
    focus_items = [item for item in all_evidence if item.get("focus_only")]
    lines = []

    lines.append("1. **[查重结论]**")
    lines.append("")
    lines.append("重复" if duplicate_items else "不重复")
    lines.append("")
    lines.append("2. **[重复项目名称]**")
    lines.append("")

    if ranked_projects:
        lines.append("|排名|历史项目名称|重复证据数|")
        lines.append("|---|---|---|")
        for index, project in enumerate(ranked_projects, start=1):
            lines.append(
                f"|{index}|{cell(project.get('project_name'))}|{project.get('duplicate_count', 0)}|"
            )
    else:
        lines.append("无。")

    lines.append("")
    lines.append("3. **[全量重复明细]**")
    lines.append("")

    if all_evidence:
        lines.append("|序号|类型|新项目出处|新项目内容|历史项目名称|知识库命中片段|综合相似度|是否重复|")
        lines.append("|---|---|---|---|---|---|---|---|")
        for index, item in enumerate(all_evidence, start=1):
            quotes = item.get("evidence_quotes") or []
            quote_text = "<br>".join(cell(quote) for quote in quotes) if quotes else "无"
            lines.append(
                "|{index}|{display_type}|{source}|{new_content}|{source_project}|{quotes}|{score}%|{dup}|".format(
                    index=index,
                    display_type=cell(item.get("display_type")),
                    source=cell(source_of(item)),
                    new_content=cell(item.get("new_content")),
                    source_project=cell(item.get("source_project")),
                    quotes=quote_text,
                    score=item.get("similarity_score", 0),
                    dup="是" if item.get("is_duplicate") else "否",
                )
            )
    else:
        lines.append("无有效重复明细。")

    requirement_types = {"需求内容", "项目目标", "项目成果"}
    requirement_duplicates = [
        item for item in duplicate_items
        if item.get("display_type") in requirement_types
    ]
    content_duplicates = [
        item for item in duplicate_items
        if item.get("display_type") in {"项目内容", "新增功能"}
    ]
    duplicate_functions = [
        item for item in duplicate_items
        if item.get("display_type") == "新增功能"
    ]

    lines.append("")
    lines.append("4. **[项目需求重复点]**")
    lines.append("")
    if requirement_duplicates:
        for item in requirement_duplicates:
            lines.append(
                f"- {cell(item.get('display_type'))}：{cell(item.get('new_content'))}（{cell(item.get('source_project'))}，综合相似度 {item.get('similarity_score', 0)}%）"
            )
    else:
        lines.append("未发现项目需求重复点。")

    lines.append("")
    lines.append("5. **[项目内容重复点]**")
    lines.append("")
    if content_duplicates:
        for item in content_duplicates:
            lines.append(
                f"- {cell(item.get('display_type'))}：{cell(item.get('new_content'))}（{cell(item.get('source_project'))}，综合相似度 {item.get('similarity_score', 0)}%）"
            )
    else:
        lines.append("未发现项目内容重复点。")

    if duplicate_functions:
        lines.append("")
        lines.append("**重复功能清单**")
        lines.append("")
        lines.append("|功能名称|新项目功能出处|历史项目名称|知识库命中片段|综合相似度|")
        lines.append("|---|---|---|---|---|")
        for item in duplicate_functions:
            quotes = item.get("evidence_quotes") or []
            quote_text = "<br>".join(cell(quote) for quote in quotes) if quotes else "无"
            lines.append(
                "|{name}|{source}|{project}|{quotes}|{score}%|".format(
                    name=cell(item.get("new_content")),
                    source=cell(source_of(item)),
                    project=cell(item.get("source_project")),
                    quotes=quote_text,
                    score=item.get("similarity_score", 0),
                )
            )

    if focus_items:
        lines.append("")
        lines.append("**重点标注**")
        for item in focus_items:
            lines.append(
                f"- {cell(item.get('new_content'))}（{cell(item.get('source_project'))}，综合相似度 {item.get('similarity_score', 0)}%）"
            )

    return {
        "text": "\n".join(lines),
    }
