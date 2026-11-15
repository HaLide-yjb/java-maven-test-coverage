"""Read coverage evidence without editing source, reports, or Markdown."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as ET

STATUSES = {"KILLED", "SURVIVED", "NO_COVERAGE", "TIMED_OUT", "NON_VIABLE",
            "MEMORY_ERROR", "RUN_ERROR", "NOT_STARTED", "STARTED"}


class Node:
    """保存 HTML 标签、属性以及按顺序排列的文本和子节点。"""
    def __init__(self, tag="", attrs=()):
        """初始化节点结构，并按名称存储属性。"""
        self.tag, self.attrs, self.children = tag, dict(attrs), []

    @property
    def text(self):
        """合并子节点文本并折叠空白，便于解析表头和计数。"""
        return " ".join(" ".join(c.text if isinstance(c, Node) else c
                                 for c in self.children).split())


class Document(HTMLParser):
    """将报告页面解析为节点树，保留用于定位的文本换行。"""
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input",
            "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self, path):
        """读取 UTF-8 报告，从虚拟根节点开始构建节点树。"""
        super().__init__(convert_charrefs=True)
        self.root = Node()
        self.stack = [self.root]
        self.feed(path.read_text(encoding="utf-8-sig"))

    def handle_starttag(self, tag, attrs):
        """将开始标签挂入父节点，仅为非空元素压栈。"""
        node = Node(tag, attrs)
        self.stack[-1].children.append(node)
        if tag not in self.VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        """保存自闭合节点及其属性。"""
        self.stack[-1].children.append(Node(tag, attrs))

    def handle_endtag(self, tag):
        """从栈中关闭匹配的标签层级，保留已解析节点。"""
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        """将原始文本保存到当前节点。"""
        self.stack[-1].children.append(data)


def descendants(node, tag=None):
    """按文档顺序遍历子树，可用 tag 指定返回的标签。"""
    for child in node.children:
        if isinstance(child, Node):
            if tag is None or child.tag == tag:
                yield child
            yield from descendants(child, tag)


def cells(row):
    """取得表格行的直接列，避免混入嵌套表格。"""
    return [c for c in row.children if isinstance(c, Node) and c.tag in ("td", "th")]


def metric(missed, covered):
    """按原始计数计算比例和总数，分母为零时比例留空。"""
    missed, covered = int(missed), int(covered)
    if min(missed, covered) < 0:
        raise ValueError("Negative coverage count")
    total = missed + covered
    return {"missed": missed, "covered": covered, "total": total,
            "percent": round(100 * covered / total, 4) if total else None}


def jacoco_footer(path):
    """从 JaCoCo HTML 总计行提取原始计数。"""
    doc = Document(path)
    footer = next(descendants(doc.root, "tfoot"), None)
    row = next(descendants(footer, "tr"), None) if footer else None
    data = [c.text for c in cells(row)] if row else []
    if len(data) < 11:
        raise ValueError("Unsupported HTML coverage table; inspect real column headers.")
    result = {}
    for name, index in (("INSTRUCTION", 1), ("BRANCH", 3)):
        match = re.fullmatch(r"([\d,]+)\s+of\s+([\d,]+)", data[index])
        if not match:
            raise ValueError("Unsupported JaCoCo total-count format.")
        missed, total = (int(n.replace(",", "")) for n in match.groups())
        result[name] = metric(missed, total - missed)
    for name, index in (("COMPLEXITY", 5), ("LINE", 7), ("METHOD", 9), ("CLASS", 11)):
        if index + 1 < len(data):
            missed, total = (int(data[i].replace(",", "")) for i in (index, index + 1))
            result[name] = metric(missed, total - missed)
    return result


def timestamp(path):
    """把文件修改时间转为 UTC 时间戳。"""
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()


def freshness(path, latest_source):
    """记录证据路径和修改时间，并判断它是否早于最新输入。"""
    return {"path": str(path), "modified_utc": timestamp(path),
            "older_than_latest_source": bool(latest_source and
                                             path.stat().st_mtime + 0.001 < latest_source.stat().st_mtime)}


def read_jacoco(project, latest_source):
    """读取 JaCoCo XML 的生产统计和存在缺口的源码行。"""
    path = project / "target/site/jacoco/jacoco.xml"
    if not path.is_file():
        return {"available": False, "reason": "JaCoCo XML missing; do not invent coverage."}
    root = ET.parse(path).getroot()
    counts = {c.attrib["type"]: metric(c.attrib["missed"], c.attrib["covered"])
              for c in root.findall("counter")}
    missed = []
    for package in root.findall("package"):
        for source in package.findall("sourcefile"):
            entries = [{"line": int(line.attrib["nr"]),
                        "missed_instructions": int(line.attrib.get("mi", 0)),
                        "covered_instructions": int(line.attrib.get("ci", 0)),
                        "missed_branches": int(line.attrib.get("mb", 0)),
                        "covered_branches": int(line.attrib.get("cb", 0))}
                       for line in source.findall("line")
                       if int(line.attrib.get("mi", 0)) or int(line.attrib.get("mb", 0))]
            if entries:
                file_counts = {c.attrib["type"]: metric(c.attrib["missed"], c.attrib["covered"])
                               for c in source.findall("counter")}
                missed.append({"source_file": package.attrib["name"] + "/" + source.attrib["name"],
                               "counts": file_counts, "locations": entries})
    return {"available": True, **freshness(path, latest_source), "counts": counts, "uncovered": missed}


def pit_summary(index):
    """读取 PIT HTML 总览中的目标类数和各指标原始计数。"""
    doc = Document(index)
    table = next(descendants(doc.root, "table"), None)
    tbody = next(descendants(table, "tbody"), None) if table else None
    row = next(descendants(tbody, "tr"), None) if tbody else None
    columns = cells(row) if row else []
    summary = {}
    if len(columns) >= 4:
        summary["mutated_classes"] = int(columns[0].text)
        for name, column in zip(("line", "mutation", "test_strength"), columns[1:4]):
            legend = next((n for n in descendants(column)
                           if "coverage_legend" in n.attrs.get("class", "").split()), None)
            match = re.fullmatch(r"(\d+)/(\d+)", legend.text) if legend else None
            if match:
                covered, total = map(int, match.groups())
                summary[name] = metric(total - covered, covered)
    return summary


def pit_xml_records(path):
    """读取 PIT XML 的逐个变异状态、算子和字节码索引。"""
    root = ET.parse(path).getroot()
    if root.tag != "mutations":
        raise ValueError("Unsupported PIT XML root. Expected <mutations>.")
    records = []
    for mutation in root.findall("mutation"):
        class_name = mutation.findtext("mutatedClass", "")
        records.append({
            "class_file": mutation.findtext("sourceFile", class_name.rsplit(".", 1)[-1] + ".java"),
            "package": class_name.rpartition(".")[0],
            "mutated_class": class_name,
            "method": mutation.findtext("mutatedMethod"),
            "method_description": mutation.findtext("methodDescription"),
            "line": int(mutation.findtext("lineNumber", "0")),
            "status": mutation.attrib["status"],
            "detected": mutation.get("detected") == "true",
            "mutator": mutation.findtext("mutator"),
            "indexes": [int(index.text) for index in mutation.findall("indexes/index")],
            "description": mutation.findtext("description"),
            "report": str(path),
        })
    return records


def pit_html_records(directory):
    """从 PIT 源码 HTML 页提取逐个变异的可用明细。"""
    records = []
    for page in sorted(directory.rglob("*.java.html")):
        parsed = Document(page)
        for row in descendants(parsed.root, "tr"):
            columns = cells(row)
            if not columns or not columns[0].text.isdigit():
                continue
            for paragraph in descendants(row, "p"):
                status = next((s for s in paragraph.attrs.get("class", "").split()
                               if s in STATUSES), None)
                if not status:
                    continue
                location = re.search(r"Location\s*:\s*(.*?)\s+Killed by\s*:", paragraph.text)
                description = " ".join(c for c in paragraph.children if isinstance(c, str)).strip()
                records.append({"class_file": page.name.removesuffix(".html"),
                                "package": page.parent.relative_to(directory).as_posix().replace("/", "."),
                                "method": location.group(1) if location else None,
                                "line": int(columns[0].text), "status": status,
                                "description": description.split("→", 1)[0].strip(),
                                "report": str(page)})
    return records


def pit_report_paths(location):
    """定位显式 XML 或报告目录中的候选 PIT 产物。"""
    if location.suffix.lower() == ".xml" and not location.is_dir():
        return location, location.parent / "index.html"
    if location.is_dir():
        directories = [location, *(p for p in location.iterdir()
                                    if p.is_dir() and re.fullmatch(r"\d{8,14}", p.name))]
        artifacts = [p for directory in directories
                     for p in (directory / "mutations.xml", directory / "index.html") if p.is_file()]
        if artifacts:
            location = max(artifacts, key=lambda p: p.stat().st_mtime).parent
    return location / "mutations.xml", location / "index.html"


def read_pit(project, latest_source, location=None):
    """汇总 PIT 原始状态，并检查报告的分母和时间是否一致。"""
    xml, index = pit_report_paths(location or project / "target/pit-reports")
    if not xml.is_file() and not index.is_file():
        return {"available": False, "reason": "PIT XML and HTML missing."}
    if xml.is_file():
        records = pit_xml_records(xml)
        evidence = xml
    else:
        records = pit_html_records(index.parent)
        evidence = index
    # A stock index may predate a newly started run in the same directory.
    has_current_index = index.is_file() and (evidence == index or
                                            index.stat().st_mtime + 0.001 >= evidence.stat().st_mtime)
    summary = pit_summary(index) if has_current_index else {}
    status_counts = Counter(record["status"] for record in records)
    if xml.is_file() and "mutation" not in summary:
        detected = sum(record["detected"] for record in records)
        summary["mutation"] = metric(len(records) - detected, detected)
        summary["mutated_classes"] = len({record["mutated_class"] for record in records})
    result = {"available": True, **freshness(evidence, latest_source), "summary": summary,
              "summary_path": str(index) if has_current_index else None,
              "status_counts": {status: status_counts.get(status, 0)
                                for status in sorted(STATUSES | status_counts.keys())},
              "mutation_entries": len(records),
              "survivors": [r for r in records if r["status"] == "SURVIVED"],
              "other_non_killed": [r for r in records if r["status"] not in ("SURVIVED", "KILLED")]}
    if summary.get("mutation", {}).get("total") != len(records):
        result["warning"] = "Mutation detail count differs from summary. Inspect format or incomplete generation."
    elif not records or status_counts["NOT_STARTED"] or status_counts["STARTED"]:
        result["warning"] = "Mutation data is empty or still pending. Check the Maven run result."
    return result


IDEA_COLUMNS = {"class": "CLASS", "method": "METHOD", "branch": "BRANCH", "line": "LINE"}


def idea_rows(path):
    """按识别出的 IDEA 表头提取每行的计数与首列链接。"""
    for table in descendants(Document(path).root, "table"):
        rows = list(descendants(table, "tr"))
        headers = [re.sub(r"[,%\s]+", "", c.text).casefold() for c in cells(rows[0])] if rows else []
        indexes = {IDEA_COLUMNS[header]: i for i, header in enumerate(headers) if header in IDEA_COLUMNS}
        if len(indexes) != len(IDEA_COLUMNS) or 0 in indexes.values():
            continue
        for row in rows[1:]:
            columns = cells(row)
            if len(columns) != len(headers):
                continue
            counts = {}
            for name, index in indexes.items():
                value = columns[index].text
                match = re.search(r"\(([\d,]+)\s*/\s*([\d,]+)\)", value)
                if match:
                    covered, total = (int(n.replace(",", "")) for n in match.groups())
                    counts[name] = metric(total - covered, covered)
                elif not value:
                    counts[name] = metric(0, 0)
            if len(counts) == len(indexes):
                anchor = next(descendants(columns[0], "a"), None)
                yield columns[0].text, counts, anchor.attrs.get("href") if anchor else None


def report_link(current, href, directory):
    """解析当前导出目录内可读的报告链接。"""
    if not href or ":" in href or href.startswith(("/", "\\")):
        return None
    candidate = (current.parent / href).resolve()
    return candidate if candidate.is_relative_to(directory) and candidate.is_file() else None


def idea_source_locations(path):
    """提取 IDEA 源码页上的未覆盖和部分覆盖标记。"""
    code = next((n for n in descendants(Document(path).root, "code")
                 if n.attrs.get("id") == "sourceCode"), None)
    if code is None:
        return []
    line_number = 1
    locations = []
    for child in code.children:
        if isinstance(child, str):
            line_number += child.count("\n")
        else:
            marker = child.attrs.get("class")
            if marker in ("nc", "pc"):
                locations.append({"line": line_number,
                                  "mark": "uncovered" if marker == "nc" else "partial"})
    return locations


def read_idea_native(index, directory, focus):
    """读取 IDEA 原生 HTML 统计，聚焦指定文件时展开源码标记。"""
    rows = list(idea_rows(index))
    overall = next((counts for label, counts, _ in rows if label.casefold() == "all classes"), None)
    if overall is None:
        return None
    packages, classes = [], []
    for package, counts, href in rows:
        package_page = report_link(index, href, directory)
        if package_page is None or package_page.name != "index.html":
            continue
        packages.append({"package": package, "counts": counts, "report": str(package_page)})
        for name, class_counts, child_href in idea_rows(package_page):
            source_page = report_link(package_page, child_href, directory)
            if source_page is None or source_page.suffix.lower() != ".html":
                continue
            source = package.replace(".", "/") + "/" + name + ".java"
            detail = {"source_file": source, "counts": class_counts, "report": str(source_page)}
            if focus and matches_source(source, focus):
                detail["locations"] = idea_source_locations(source_page)
            classes.append(detail)
    return {"format": "IDEA", "all_exported_counts": overall, "package_counts": packages,
            "class_counts": classes, "production_scope_complete": None,
            "scope_note": "Counts describe the manual export's selected classes. Confirm production scope before comparing."}


def read_idea(project, directory, latest_source, focus=None):
    """识别 IDEA 导出格式，并根据可用信息核对生产范围。"""
    index = directory / "index.html"
    if not index.is_file():
        return {"available": False, "reason": "Manual IDEA export not supplied."}
    result = {"available": True, **freshness(index, latest_source)}
    try:
        native = read_idea_native(index, directory, focus)
        if native:
            return {**result, **native}
        result["format"] = "JaCoCo"
        result["all_exported_counts"] = jacoco_footer(index)
    except (ValueError, OSError) as error:
        result["warning"] = str(error)
        return result
    classes = project / "target/classes"
    if not classes.is_dir():
        result["warning"] = "Compiled production class metadata missing; manual export scope unknown."
        return result
    pages, missing = [], []
    for compiled in sorted(classes.rglob("*.class")):
        if compiled.stem in ("module-info", "package-info"):
            continue
        relative = compiled.relative_to(classes)
        package = ".".join(relative.parts[:-1])
        page = directory / package / (compiled.stem + ".html")
        if not page.is_file():
            missing.append(relative.as_posix())
        else:
            pages.append(page)
    result["production_scope_complete"] = not missing and bool(pages)
    result["missing_production_pages"] = missing
    result["mapped_production_pages"] = len(pages)
    if result["production_scope_complete"]:
        aggregate = {}
        try:
            for page in pages:
                for name, count in jacoco_footer(page).items():
                    total = aggregate.setdefault(name, {"missed": 0, "covered": 0})
                    total["missed"] += count["missed"]
                    total["covered"] += count["covered"]
            result["production_counts"] = {name: metric(**count) for name, count in aggregate.items()}
        except (ValueError, OSError) as error:
            result["production_scope_complete"] = False
            result["warning"] = str(error)
    else:
        result["warning"] = "Do not treat partial production mapping as whole-project coverage."
    return result


def matches_source(source, focus):
    """标准化文件名、限定类名或路径后匹配聚焦条件。"""
    if not focus:
        return True
    qualified = source.replace("\\", "/").removesuffix(".java").replace("/", ".").casefold()
    requested = focus.replace("\\", "/").removesuffix(".java").replace("/", ".").casefold()
    return qualified == requested or qualified.endswith("." + requested)


def focus_reports(result, limit, focus, rank_by):
    """按目标指标裁剪缺口明细，同时保留报告总计。"""
    rank_metric = rank_by.upper()
    for key, details_key in (("jacoco", "uncovered"), ("idea_manual", "class_counts")):
        report = result[key]
        entries = report.get(details_key, [])
        entries = [entry for entry in entries if matches_source(entry["source_file"], focus)]
        def missed(entry, name):
            """取指定指标的缺口数，缺少该指标时按零参与排序。"""
            return entry.get("counts", {}).get(name, {}).get("missed", 0)

        priorities = sorted(
            (entry for entry in entries if focus or any(missed(entry, name) for name in IDEA_COLUMNS.values())),
            key=lambda entry: (-missed(entry, rank_metric), -missed(entry, "BRANCH"),
                               -missed(entry, "LINE"), entry["source_file"]))
        if limit is not None:
            priorities = priorities[:limit]
        if limit is not None or focus:
            report[details_key] = priorities if focus else [
                {key: value for key, value in entry.items() if key != "locations"} for entry in priorities]
            if key == "idea_manual" and limit is not None and not focus:
                packages = report.get("package_counts", [])
                report["package_counts"] = sorted(
                    packages, key=lambda entry: -missed(entry, rank_metric))[:limit]

    report = result["pit"]
    groups = {}
    for key in ("survivors", "other_non_killed"):
        entries = report.get(key, [])
        if not entries:
            continue
        matches = []
        for entry in entries:
            source = (entry["package"].replace(".", "/") + "/" +
                      entry["class_file"]).lstrip("/")
            if matches_source(source, focus):
                matches.append(entry)
                groups.setdefault(source, Counter())[entry["status"]] += 1
        if focus:
            report[key] = matches
        elif limit is not None:
            report.pop(key, None)
    if limit is not None or focus:
        groups = sorted(groups.items(), key=lambda item: (-sum(item[1].values()), item[0]))
        report["pending_by_source"] = [
            {"source_file": source, "status_counts": dict(counts)} for source, counts in groups[:limit]]


def read_surefire(project, latest_source):
    """汇总 Surefire 的用例、失败、错误和跳过数量。"""
    directory = project / "target/surefire-reports"
    paths = sorted(directory.glob("TEST-*.xml"))
    if not paths:
        return {"available": False, "reason": "Surefire XML missing."}
    suites = []
    for path in paths:
        root = ET.parse(path).getroot()
        suite = {"name": root.attrib["name"], **freshness(path, latest_source)}
        suite.update({name: int(root.attrib.get(name, 0))
                      for name in ("tests", "failures", "errors", "skipped")})
        suites.append(suite)
    counts = {name: sum(suite[name] for suite in suites)
              for name in ("tests", "failures", "errors", "skipped")}
    counts["passed"] = counts["tests"] - counts["failures"] - counts["errors"] - counts["skipped"]
    return {"available": True, "counts": counts, "suites": suites,
            "older_than_latest_source": any(suite["older_than_latest_source"] for suite in suites)}


def main():
    """解析命令行参数，收集证据并以 UTF-8 JSON 输出。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument("--idea-report", type=Path)
    parser.add_argument("--pit-report", type=Path, help="Read a PIT directory or a specific mutations XML.")
    parser.add_argument("--top", type=int, help="Show a compact ranking of at most N sources per report.")
    parser.add_argument("--rank-by", choices=("branch", "line", "method", "class"), default="branch")
    parser.add_argument("--focus", help="Expand details for one source, such as ExamGradingService.")
    args = parser.parse_args()
    if args.top is not None and args.top < 1:
        parser.error("--top must be positive")
    project = args.project.resolve()
    if not (project / "pom.xml").is_file():
        parser.error("--project must contain pom.xml")
    sources = list((project / "src").rglob("*.java"))
    latest = max(sources, key=lambda p: p.stat().st_mtime) if sources else None
    input_candidates = [project / "pom.xml", *([latest] if latest else [])]
    latest_input = max(input_candidates, key=lambda p: p.stat().st_mtime)
    result = {"project": str(project),
              "latest_source": str(latest) if latest else None,
              "latest_source_modified_utc": timestamp(latest) if latest else None,
              "latest_input": str(latest_input)}
    idea = args.idea_report.resolve() if args.idea_report else project / "htmlReport"
    pit = args.pit_report.resolve() if args.pit_report else None
    for name, action in (("jacoco", lambda: read_jacoco(project, latest_input)),
                         ("pit", lambda: read_pit(project, latest_input, pit)),
                         ("idea_manual", lambda: read_idea(project, idea, latest_input, args.focus)),
                         ("surefire", lambda: read_surefire(project, latest_input))):
        try:
            result[name] = action()
        except (ValueError, OSError, ET.ParseError, KeyError, StopIteration) as error:
            result[name] = {"available": False, "error": str(error), "requires_inspection": True}
    focus_reports(result, args.top, args.focus, args.rank_by)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
