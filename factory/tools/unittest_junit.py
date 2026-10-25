#!/usr/bin/env python3
"""Runs `unittest discover` in the current directory and writes the results as JUnit XML.

    python3 factory/tools/unittest_junit.py junit.xml [start dir]

Cortana's default test command for Python projects when pytest is not installed. Stdlib only.
"""
import sys
import time
import unittest
import xml.etree.ElementTree as ET


class Result(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cases, self.started = [], {}

    def startTest(self, test):
        self.started[test.id()] = time.time()
        super().startTest(test)

    def _record(self, test, kind=None, detail=""):
        self.cases.append((test.id(), time.time() - self.started.get(test.id(), time.time()), kind, detail))

    def addSuccess(self, test):
        super().addSuccess(test)
        self._record(test)

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._record(test, "failure", self._exc_info_to_string(err, test))

    def addError(self, test, err):
        super().addError(test, err)
        self._record(test, "error", self._exc_info_to_string(err, test))

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self._record(test, "skipped", reason)

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self._record(test)

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self._record(test, "failure", "unexpected success")


def main() -> int:
    out = sys.argv[1] if len(sys.argv) > 1 else "junit.xml"
    start = sys.argv[2] if len(sys.argv) > 2 else "."
    sys.path.insert(0, start)
    suite = unittest.defaultTestLoader.discover(start)
    result = unittest.TextTestRunner(resultclass=Result, verbosity=1).run(suite)
    root = ET.Element("testsuite", name="unittest", tests=str(len(result.cases)),
                      failures=str(sum(c[2] == "failure" for c in result.cases)),
                      errors=str(sum(c[2] == "error" for c in result.cases)),
                      skipped=str(sum(c[2] == "skipped" for c in result.cases)))
    for test_id, seconds, kind, detail in result.cases:
        cls, _, name = test_id.rpartition(".")
        case = ET.SubElement(root, "testcase", classname=cls, name=name, time=f"{seconds:.3f}")
        if kind:
            lines = detail.strip().splitlines()
            child = ET.SubElement(case, kind, message=(lines[-1] if lines else kind)[:500])
            child.text = detail
    ET.ElementTree(root).write(out, encoding="utf-8", xml_declaration=True)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
