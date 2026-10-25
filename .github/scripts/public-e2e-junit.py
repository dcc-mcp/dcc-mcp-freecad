"""Publish counts and stages without host names, tracebacks or captured output."""

import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def public_report(source, destination):
    root = ET.parse(source).getroot()
    for element in root.iter():
        for key in ("hostname", "file"):
            element.attrib.pop(key, None)
        for child in list(element):
            if child.tag in ("system-out", "system-err", "properties"):
                element.remove(child)
        if element.tag in ("failure", "error", "skipped"):
            element.attrib = {"message": "See sanitized stage report and workflow status"}
            element.text = "Native MCP workflow did not satisfy its required gate."
    Path(destination).parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(root).write(destination, encoding="utf-8", xml_declaration=True)


if __name__ == "__main__":
    public_report(sys.argv[1], sys.argv[2])
