"""Print passed / failed / skipped counts and every skip reason from a pytest JUnit XML file (Markdown).

A skipped test still reads as "passed" in a short log; this keeps the number visible. Usage: ci_summary.py X.xml"""
import sys
import xml.etree.ElementTree as ET

root = ET.parse(sys.argv[1]).getroot()
suites = [root] if root.tag == "testsuite" else list(root)
n = {k: sum(int(s.get(k, 0)) for s in suites) for k in ("tests", "failures", "errors", "skipped")}
print(f"**pytest:** {n['tests'] - n['failures'] - n['errors'] - n['skipped']} passed, {n['failures']} failed, "
      f"{n['errors']} errors, **{n['skipped']} skipped**\n")
for case in root.iter("testcase"):
    sk = case.find("skipped")
    if sk is not None:
        print(f"- skipped `{case.get('classname')}.{case.get('name')}`: {sk.get('message', '')}")
