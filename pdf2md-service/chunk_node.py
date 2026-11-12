"""项目查重工作流「章节分块」节点代码（与 Dify 工作流节点保持一致，改动时两边同步）。

入口是 main(text)，text 为文档提取器输出的全文。
入库端（build_project_kb_chunks.py，PDF 经 build_kb_from_pdf.py 也调用它）有一份独立实现的
章节识别和分类规则（is_likely_heading、classify_section 等），改这里的规则时要同步改那边，保证查询端和库端切法一致。
"""

import re


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
        parsed = parse_numbered_heading_line(line)
        if not parsed or not looks_like_toc_entry(line):
            continue

        number, title = parsed
        if not is_likely_heading(number, title):
            continue

        key = title_key(title)
        values = headings.setdefault(key, [])
        if (number, title) not in values:
            values.append((number, title))
    return headings


def restore_auto_numbered_headings(text):
    toc_headings = build_toc_heading_map(text)
    if not toc_headings:
        return text

    restored = []
    used_counts = {}
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            restored.append(line)
            continue

        parsed = parse_numbered_heading_line(stripped)
        if parsed and is_likely_heading(parsed[0], parsed[1]):
            restored.append(f"{parsed[0]} {parsed[1]}")
            continue

        key = title_key(stripped)
        candidates = toc_headings.get(key, [])
        if not candidates or len(stripped) > 120:
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


# 统建模板的功能清单表没有固定表名，按表头识别：含「一级/二级业务需求」「二级功能」「状态」和「功能描述/功能点说明」。
# 一张表常有几千行，逐行作为功能会产生上千个检索任务，所以按「一级业务需求 + 状态」归组。
HEADER_FUNCTION_TABLE_MAX_ITEMS = 150


def normalize_table_function_status(status):
    status = re.sub(r"\s+", "", str(status or ""))
    if "新增" in status:
        return "新增"
    if any(word in status for word in ("非新增", "完善", "优化", "改造")):
        return "非新增"
    return "不明"


def function_table_columns(headers):
    """按列名含义找出统建功能清单表各列的位置；不是这类表时返回 None。

    各文档表头写法不一，例如「一级业务需求/一级业务项/一级业务」「二级功能/二级业务功能/功能」
    「功能描述/二级功能描述/功能概述」「状态/二级功能状态」，所以按关键词认列，不按全名。
    必须有一级或二级业务列：旧模板的功能清单表没有这两列，仍走原有路径。
    """
    names = [re.sub(r"\s+", "", str(h or "")) for h in headers]

    def find(test):
        for i, name in enumerate(names):
            if test(name):
                return i
        return None

    def is_desc(name):
        return any(word in name for word in ("描述", "说明", "概述"))

    def is_business(name, level):
        return name.startswith(level) and ("业务" in name or "任务" in name) and "功能" not in name

    columns = {
        "status": find(lambda n: "状态" in n),
        "desc": find(is_desc),
        "group": find(lambda n: is_business(n, "一级")),
        "requirement": find(lambda n: is_business(n, "二级")),
        "function": find(lambda n: "二级" in n and "功能" in n and "状态" not in n and not is_desc(n)),
        "module": find(lambda n: ("一级" in n and "功能" in n) or n in ("模块", "功能模块")),
    }
    if columns["function"] is None:
        columns["function"] = find(lambda n: n in ("功能", "功能名称", "功能点"))
    if None in (columns["status"], columns["desc"], columns["function"]):
        return None
    if columns["group"] is None and columns["requirement"] is None:
        return None
    return columns


def parse_header_function_tables(text):
    """返回 (功能清单, [(表格起点, 表格终点), ...])。没有识别到时返回 ([], [])。"""
    lines = str(text or "").splitlines(keepends=True)
    offsets = []
    position = 0
    for line in lines:
        offsets.append(position)
        position += len(line)

    spans = []
    groups = {}
    index = 0
    while index < len(lines):
        headers = parse_markdown_row(lines[index])
        columns = function_table_columns(headers) if headers else None
        if not columns:
            index += 1
            continue

        def cell(cells, role):
            position = columns.get(role)
            return cells[position].strip() if position is not None else ""

        start = offsets[index]
        index += 1
        carried = {"group": "", "requirement": "", "module": ""}
        while True:
            while index < len(lines) and lines[index].strip().startswith("|"):
                cells = parse_markdown_row(lines[index])
                index += 1
                if len(cells) != len(headers) or all(re.fullmatch(r"-+", cell or "-") for cell in cells):
                    continue
                # 合并单元格跨页或跨表时这几列是空的，沿用上一行的值。
                for role in carried:
                    carried[role] = cell(cells, role) or carried[role]
                second = cell(cells, "function")
                if not second:
                    continue
                combined = second + cell(cells, "desc")
                if any(word in combined for word in ("接口", "对接", "系统集成", "数据集成", "服务集成", "数据交换")):
                    continue

                group_name = carried["group"] or carried["requirement"] or carried["module"] or second
                status = normalize_table_function_status(cell(cells, "status"))
                group = groups.setdefault((group_name, status), {"需求": {}, "order": len(groups)})
                requirement = carried["requirement"] or carried["module"] or group_name
                names = group["需求"].setdefault(requirement, [])
                if second not in names:
                    names.append(second)

            # 表格跨页没拼上时会断成几张：中间只隔空行、列数相同、又没有自己表头的，算同一张表的续表。
            look = index
            while look < len(lines) and not lines[look].strip():
                look += 1
            if look >= len(lines) or look == index:
                break
            following = parse_markdown_row(lines[look])
            if len(following) != len(headers) or function_table_columns(following) is not None:
                break
            index = look
        end = offsets[index] if index < len(offsets) else len(text)
        spans.append((start, end))

    function_items = []
    for (group_name, status), group in sorted(groups.items(), key=lambda kv: kv[1]["order"]):
        details = []
        for requirement, names in group["需求"].items():
            # 二级功能名称全部列出，不截断：没列出的功能不会进入检索，等于不参与查重。
            details.append(f"{requirement}：{'、'.join(names)}")
        function_items.append({
            "功能名称": group_name,
            "功能描述": "；".join(details),
            "功能状态": status,
            "出处类型": "表格",
            "章节路径": "功能清单表",
            "表名": "功能清单表",
        })
        if len(function_items) >= HEADER_FUNCTION_TABLE_MAX_ITEMS:
            break

    return function_items, spans


def remove_spans(text, spans):
    if not spans:
        return text
    parts = []
    cursor = 0
    for start, end in sorted(spans):
        parts.append(text[cursor:start])
        parts.append("（功能清单表已由代码解析）\n")
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts)


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

    # 统建模板「2.x.1 业务需求描述」下的子节标题不一定带「业务需求」，但内容都是需求。
    if any("业务需求描述" in item for item in titles):
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


def main(text):
    text = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = restore_auto_numbered_headings(text)

    project_name = extract_project_name(text)
    is_design = is_design_project(project_name)
    function_table_spans = []
    has_named_table = re.search(r"(?m)^表[-－]?\s*\d+\s+本期系统开发功能清单表\s*$", text)
    header_items = []
    if is_design and not has_named_table:
        header_items, function_table_spans = parse_header_function_tables(text)

    if header_items:
        function_items, function_table_start = header_items, -1
    else:
        function_items, function_table_start = parse_function_table(text, is_design)

    llm_text = text[:function_table_start] if function_table_start >= 0 else text
    llm_text = remove_spans(llm_text, function_table_spans)
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