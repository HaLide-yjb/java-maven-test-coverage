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
    def __init__(self, tag="", attrs=()):
        self.tag, self.attrs, self.children = tag, dict(attrs), []

    @property
    def text(self):
        return " ".join(" ".join(c.text if isinstance(c, Node) else c
                                 for c in self.children).split())


class Document(HTMLParser):
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input",
            "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self, path):
        super().__init__(convert_charrefs=True)
        self.root = Node()
        self.stack = [self.root]
        self.feed(path.read_text(encoding="utf-8-sig"))

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs)
        self.stack[-1].children.append(node)
        if tag not in self.VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.stack[-1].children.append(Node(tag, attrs))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def descendants(node, tag=None):
    for child in node.children:
        if isinstance(child, Node):
            if tag is None or child.tag == tag:
                yield child
            yield from descendants(child, tag)


def cells(row):
    return [c for c in row.children if isinstance(c, Node) and c.tag in ("td", "th")]


def metric(missed, covered):
    missed, covered = int(missed), int(covered)
    if min(missed, covered) < 0:
        raise ValueError("Negative coverage count")
    total = missed + covered
    return {"missed": missed, "covered": covered, "total": total,
            "percent": round(100 * covered / total, 4) if total else None}


def jacoco_footer(path):
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
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()


def freshness(path, latest_source):
    return {"path": str(path), "modified_utc": timestamp(path),
            "older_than_latest_source": bool(latest_source and
                                             path.stat().st_mtime + 0.001 < latest_source.stat().st_mtime)}


def read_jacoco(project, latest_source):
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
                        "missed_branches": int(line.attrib.get("mb", 0))}
                       for line in source.findall("line")
                       if int(line.attrib.get("mi", 0)) or int(line.attrib.get("mb", 0))]
            if entries:
                missed.append({"source_file": package.attrib["name"] + "/" + source.attrib["name"],
                               "locations": entries})
    return {"available": True, **freshness(path, latest_source), "counts": counts, "uncovered": missed}


def read_pit(project, latest_source):
    directory = project / "target/pit-reports"
    if not (directory / "index.html").is_file() and directory.is_dir():
        candidates = [p for p in directory.iterdir() if p.is_dir()
                      and re.fullmatch(r"\d{8,14}", p.name) and (p / "index.html").is_file()]
        if candidates:
            directory = max(candidates, key=lambda p: (p / "index.html").stat().st_mtime)
    index = directory / "index.html"
    if not index.is_file():
        return {"available": False, "reason": "PIT HTML missing; inspect XML manually if configured."}
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
    status_counts = Counter(r["status"] for r in records)
    result = {"available": True, **freshness(index, latest_source), "summary": summary,
              "status_counts": {status: status_counts.get(status, 0) for status in sorted(STATUSES)},
              "mutation_entries": len(records),
              "survivors": [r for r in records if r["status"] == "SURVIVED"],
              "other_non_killed": [r for r in records if r["status"] not in ("SURVIVED", "KILLED")]}
    if summary.get("mutation", {}).get("total") != len(records):
        result["warning"] = "Mutation detail count differs from summary; inspect report format or incomplete generation."
    return result


def read_idea(project, directory, latest_source):
    index = directory / "index.html"
    if not index.is_file():
        return {"available": False, "reason": "Manual IDEA export not supplied."}
    result = {"available": True, **freshness(index, latest_source)}
    try:
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument("--idea-report", type=Path)
    args = parser.parse_args()
    project = args.project.resolve()
    if not (project / "pom.xml").is_file():
        parser.error("--project must contain pom.xml")
    sources = list((project / "src").rglob("*.java"))
    latest = max(sources, key=lambda p: p.stat().st_mtime) if sources else None
    result = {"project": str(project),
              "latest_source": str(latest) if latest else None,
              "latest_source_modified_utc": timestamp(latest) if latest else None}
    for name, action in (("jacoco", lambda: read_jacoco(project, latest)),
                         ("pit", lambda: read_pit(project, latest)),
                         ("idea_manual", lambda: read_idea(project, args.idea_report.resolve()
                                                          if args.idea_report else project / "htmlReport", latest))):
        try:
            result[name] = action()
        except (ValueError, OSError, ET.ParseError, KeyError, StopIteration) as error:
            result[name] = {"available": False, "error": str(error), "requires_inspection": True}
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
