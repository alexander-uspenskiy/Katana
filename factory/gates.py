"""Katana quality gates: plan steps that Katana measures itself, with no LLM call.

A gate step has conditions such as `coverage >= 80`. Each condition reads one metric:

- test_pass_rate     runs the project's test command and parses the JUnit XML it writes
- coverage           reads coverage.py JSON (or an istanbul summary or lcov file)
- security_findings  counts findings at or above a severity, from bandit / semgrep / pip-audit when
                     installed, otherwise from the Security Engineer's findings table
- acceptance_pass    the share of acceptance criteria the Tester's report marks PASS
- review_must_fix    the Code Reviewer's must-fix count
- run_tokens         tokens the run has spent so far

A metric that can't be measured (no test command, coverage.py not installed) is `unavailable`; the
`when_unavailable` setting decides whether that warns, blocks or is ignored. Used by the orchestrator
(evaluate) and by settings validation (the tables below).
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

METRICS = {
    "test_pass_rate": {"label": "Test pass rate", "unit": "%", "op": ">=", "value": 100},
    "coverage": {"label": "Coverage", "unit": "%", "op": ">=", "value": 80},
    "security_findings": {"label": "Security findings", "unit": "", "op": "<=", "value": 0, "level": "high"},
    "acceptance_pass": {"label": "Acceptance criteria passing", "unit": "%", "op": ">=", "value": 100},
    "review_must_fix": {"label": "Review must-fix items", "unit": "", "op": "<=", "value": 0},
    "run_tokens": {"label": "Run tokens", "unit": "tokens", "op": "<=", "value": 5_000_000},
}
OPS = {">=": lambda a, b: a >= b, ">": lambda a, b: a > b, "<=": lambda a, b: a <= b,
       "<": lambda a, b: a < b, "==": lambda a, b: a == b}
SEVERITIES = ["block", "warn"]
LEVELS = ["critical", "high", "medium", "low"]  # a security condition counts findings at this level or worse
UNAVAILABLE = ["warn", "block", "ignore"]
SECURITY_SOURCES = ["auto", "tools", "report", "both"]
REPORT = "docs/gate-report.md"
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache"}

TOOLS_DIR = Path(__file__).resolve().parent / "tools"
AC_ROW = re.compile(r"^\|\s*\**(AC-\d+(?:\.\d+)?)\**\s*\|(.+)$", re.M)
SEVERITY_CELL = re.compile(r"^\**(critical|high|medium|low|info)\**$", re.I)
MUST_FIX_LINE = re.compile(r"^\**MUST_FIX:\**\s*(\d+)", re.M)
SUMMARY_PATTERNS = (  # test runners' own summary lines, when no JUnit XML was written
    ("pytest", re.compile(r"=+ (.*?(?:passed|failed|error).*?) in [\d.]+s")),
    ("unittest", re.compile(r"^Ran (\d+) tests? in", re.M)),
    ("node", re.compile(r"^# tests (\d+)", re.M)),
)


# ---------------------------------------------------------------- report parsing

def parse_acceptance(text: str) -> dict:
    """The acceptance table in the Tester's report: `| AC-1.1 | test name | PASS |`."""
    items = {}
    for m in AC_ROW.finditer(text or ""):
        cells = [c.strip(" *`") for c in m.group(2).split("|")]
        result = next((c.upper() for c in cells if c.upper() in ("PASS", "FAIL")), None)
        test = next((c for c in cells if c and c.upper() not in ("PASS", "FAIL")), "")
        if result:
            items[m.group(1)] = {"id": m.group(1), "test": test, "result": result}
    items = list(items.values())
    return {"total": len(items), "pass": sum(i["result"] == "PASS" for i in items), "items": items}


def section_items(text: str, heading: str) -> list:
    """Numbered items under a `**Must fix**` / `## Must fix` heading, up to the next heading."""
    out, inside = [], False
    for line in (text or "").splitlines():
        bare = line.strip().strip("#*: ").lower()
        if re.match(r"^(#+\s|\*\*|__)", line.strip()):
            inside = bare.startswith(heading)
            continue
        if inside:
            m = re.match(r"^\s*\d+[.)]\s+(.+)", line)
            if m:
                out.append(m.group(1).strip())
    return out


def parse_review(text: str) -> dict:
    items = section_items(text, "must fix")
    m = MUST_FIX_LINE.findall(text or "")
    count = int(m[-1]) if m else len(items)
    return {"count": count, "items": items}


def parse_security_report(text: str) -> list:
    """Rows of the Security Engineer's findings table: any table row with a severity cell."""
    findings = []
    for line in (text or "").splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        sev = next((c for c in cells if SEVERITY_CELL.match(c)), None)
        if sev:
            rest = [c for c in cells if c is not sev and c and not re.fullmatch(r"[-: ]+", c)]
            findings.append({"severity": sev.strip("*").lower(), "text": " · ".join(rest)[:300],
                             "source": "security report"})
    return findings


# ---------------------------------------------------------------- measuring

def run_cmd(cmd: str, cwd: Path, log: Path, timeout: int, env: dict) -> tuple:
    """Runs a shell command from settings. Returns (exit code, output). 127 means not found."""
    try:
        proc = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env)
        out, code = (proc.stdout or "") + (proc.stderr or ""), proc.returncode
    except subprocess.TimeoutExpired as e:
        out, code = f"{e.stdout or ''}\ntimeout after {timeout}s", -1
    with open(log, "a") as f:
        f.write(f"$ {cmd}\n{out}\n[exit {code}]\n\n")
    return code, out


def has_module(name: str) -> bool:
    return subprocess.run([sys.executable, "-c", f"import {name}"], capture_output=True).returncode == 0


def project_files(workspace: Path):
    """The project's own files: no git data, dependencies or virtualenvs."""
    for p in workspace.rglob("*"):
        if not SKIP_DIRS.intersection(p.relative_to(workspace).parts) and p.is_file():
            yield p


def detect(workspace: Path) -> dict:
    """Default test and coverage commands for the project in the workspace."""
    py = sys.executable
    if any(p.suffix == ".py" for p in project_files(workspace)):
        runner = f"-m pytest -q --junitxml={{junit}}" if has_module("pytest") \
            else f"{shlex.quote(str(TOOLS_DIR / 'unittest_junit.py'))} {{junit}}"
        if has_module("coverage"):
            return {"stack": "python", "test": f"{py} -m coverage run --source=. --omit='test*,*/test*' {runner}",
                    "coverage": f"{py} -m coverage json -q -o {{coverage}}"}
        return {"stack": "python", "test": f"{py} {runner}", "coverage": ""}
    if (workspace / "package.json").exists() and shutil.which("node"):
        return {"stack": "node", "test": "node --test --experimental-test-coverage --test-reporter=junit "
                "--test-reporter-destination={junit} --test-reporter=lcov --test-reporter-destination={coverage}",
                "coverage": ""}
    return {"stack": None, "test": "", "coverage": ""}


def parse_junit(path: Path) -> dict | None:
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return None
    cases = list(root.iter("testcase"))
    failures, skipped = [], 0
    for c in cases:
        bad = c.find("failure") if c.find("failure") is not None else c.find("error")
        if c.find("skipped") is not None:
            skipped += 1
        elif bad is not None:
            name = ".".join(x for x in (c.get("classname"), c.get("name")) if x)
            msg = bad.get("message") or ((bad.text or "").strip().splitlines() or [""])[-1]
            failures.append({"name": name, "message": msg[:300]})
    return {"total": len(cases), "failed": len(failures), "skipped": skipped, "failures": failures}


def parse_summary(output: str) -> dict | None:
    """Fallback for runners that printed a summary but wrote no JUnit XML."""
    m = SUMMARY_PATTERNS[0][1].search(output)
    if m:
        n = {k: int(v) for v, k in re.findall(r"(\d+) (passed|failed|errors?|skipped)", m.group(1))}
        failed = n.get("failed", 0) + n.get("error", 0) + n.get("errors", 0)
        return {"total": n.get("passed", 0) + failed + n.get("skipped", 0), "failed": failed,
                "skipped": n.get("skipped", 0), "failures": []}
    m = SUMMARY_PATTERNS[1][1].search(output)
    if m:
        bad = re.search(r"FAILED \((.*?)\)", output)
        counts = dict((k, int(v)) for k, v in re.findall(r"(\w+)=(\d+)", bad.group(1))) if bad else {}
        return {"total": int(m.group(1)), "failed": counts.get("failures", 0) + counts.get("errors", 0),
                "skipped": counts.get("skipped", 0), "failures": []}
    m = SUMMARY_PATTERNS[2][1].search(output)
    if m:
        fail = re.search(r"^# fail (\d+)", output, re.M)
        skip = re.search(r"^# skipped (\d+)", output, re.M)
        return {"total": int(m.group(1)), "failed": int(fail.group(1)) if fail else 0,
                "skipped": int(skip.group(1)) if skip else 0, "failures": []}
    return None


def parse_coverage(path: Path) -> dict | None:
    """coverage.py JSON, an istanbul json-summary, or lcov. Returns percent and the least-covered files."""
    try:
        text = path.read_text()
    except OSError:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict) and "totals" in data:  # coverage.py
        files = sorted(((f, v["summary"]["percent_covered"]) for f, v in (data.get("files") or {}).items()),
                       key=lambda x: x[1])
        return {"percent": round(data["totals"]["percent_covered"], 1), "files": files}
    if isinstance(data, dict) and "total" in data:  # istanbul json-summary
        files = sorted(((f, v["lines"]["pct"]) for f, v in data.items() if f != "total"), key=lambda x: x[1])
        return {"percent": round(data["total"]["lines"]["pct"], 1), "files": files}
    found = hit = 0
    files, current = [], None
    for line in text.splitlines():  # lcov
        if line.startswith("SF:"):
            current = [line[3:], 0, 0]
        elif line.startswith(("LF:", "LH:")) and current:
            current[1 if line.startswith("LF:") else 2] = int(line[3:])
        elif line == "end_of_record" and current:
            found, hit = found + current[1], hit + current[2]
            files.append((current[0], round(100 * current[2] / current[1], 1) if current[1] else 100.0))
            current = None
    if not files:
        return None
    return {"percent": round(100 * hit / found, 1) if found else 100.0, "files": sorted(files, key=lambda x: x[1])}


def security_tools(workspace: Path, out: Path, log: Path, timeout: int, env: dict) -> tuple:
    """Runs whichever of bandit, semgrep and pip-audit are installed. Returns (tools run, findings)."""
    ran, findings = [], []
    has_py = any(p.suffix == ".py" for p in project_files(workspace))
    if has_py and shutil.which("bandit"):
        code, _ = run_cmd(f"bandit -r . -q -f json -o {shlex.quote(str(out / 'bandit.json'))} -x ./.git",
                          workspace, log, timeout, env)
        data = _json(out / "bandit.json")
        if data is not None:
            ran.append("bandit")
            for r in data.get("results") or []:
                findings.append({"severity": str(r.get("issue_severity", "low")).lower(), "source": "bandit",
                                 "text": f"{r.get('filename')}:{r.get('line_number')} {r.get('issue_text')}"})
    if shutil.which("semgrep"):
        code, _ = run_cmd(f"semgrep scan --config auto --quiet --json -o {shlex.quote(str(out / 'semgrep.json'))}",
                          workspace, log, timeout, env)
        data = _json(out / "semgrep.json")
        if data is not None:
            ran.append("semgrep")
            sev = {"ERROR": "high", "WARNING": "medium", "INFO": "low"}
            for r in data.get("results") or []:
                extra = r.get("extra") or {}
                findings.append({"severity": sev.get(extra.get("severity"), "medium"), "source": "semgrep",
                                 "text": f"{r.get('path')}:{(r.get('start') or {}).get('line')} "
                                         f"{(extra.get('message') or r.get('check_id', ''))[:200]}"})
    reqs = workspace / "requirements.txt"
    if reqs.exists() and shutil.which("pip-audit"):
        run_cmd(f"pip-audit -r requirements.txt -f json -o {shlex.quote(str(out / 'pip-audit.json'))}",
                workspace, log, timeout, env)
        data = _json(out / "pip-audit.json")
        if data is not None:
            ran.append("pip-audit")
            deps = data.get("dependencies", data) if isinstance(data, dict) else data
            for d in deps or []:
                for v in d.get("vulns") or []:  # pip-audit reports no severity: treat a known CVE as high
                    findings.append({"severity": "high", "source": "pip-audit",
                                     "text": f"{d.get('name')} {d.get('version')}: {v.get('id')} "
                                             f"(fixed in {', '.join(v.get('fix_versions') or []) or 'no release'})"})
    return ran, findings


def _json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


class Measure:
    """Measures metrics for one gate run, each at most once."""

    def __init__(self, ctx: dict):
        self.ctx, self.cache = ctx, {}
        self.conf = ctx.get("conf") or {}
        self.workspace, self.out = Path(ctx["workspace"]), Path(ctx["out_dir"])
        self.log = self.out / "gate.log"
        self.timeout = int(self.conf.get("timeout_min") or 10) * 60
        self.env = dict(ctx.get("env") or os.environ, COVERAGE_FILE=str(self.out / ".coverage"))
        self.paths = {"junit": str(self.out / "junit.xml"), "coverage": str(self.out / "coverage.json")}

    def get(self, metric: str) -> dict:
        if metric not in self.cache:
            self.cache[metric] = getattr(self, metric)()
        return self.cache[metric]

    def source(self, role: str, what: str) -> tuple:
        """(text, problem) for the latest report of an agent role; problem is a result dict."""
        src = (self.ctx.get("sources") or {}).get(role)
        if not src:
            return None, {"status": "unavailable", "detail": f"no {what} in this run yet"}
        if src.get("stale"):
            return None, {"status": "stale", "detail": f"{src['stage']} has not run since the latest build"}
        try:
            return Path(src["path"]).read_text(errors="replace"), None
        except OSError:
            return None, {"status": "unavailable", "detail": f"{src['path']} is missing"}

    def tests(self) -> dict:
        if "tests" in self.cache:
            return self.cache["tests"]
        found = detect(self.workspace)
        cmd = self.conf.get("test_command") or found["test"]
        self.cache["auto_coverage"] = "" if self.conf.get("test_command") else found["coverage"]
        if not cmd:
            res = {"status": "unavailable", "detail": "no test command set and none detected"}
        else:
            code, output = run_cmd(cmd.format(**self.paths), self.workspace, self.log, self.timeout, self.env)
            parsed = parse_junit(Path(self.paths["junit"])) or parse_summary(output)
            if code == 127 or (code != 0 and re.search(r"No module named (pytest|coverage)\b|command not found",
                                                       output) and not parsed):
                res = {"status": "unavailable", "detail": f"test tool not installed: {output.strip()[-160:]}"}
            elif parsed is None and code != 0:
                tail = "\n".join(output.strip().splitlines()[-12:])
                res = {"status": "measured", "value": 0.0, "total": 0, "failed": 0,
                       "detail": f"the test command failed (exit {code}) before reporting results",
                       "findings": [{"severity": "high", "source": "tests",
                                     "text": f"Test command `{cmd}` failed (exit {code}):\n{tail}"}]}
            elif parsed is None:
                res = {"status": "unavailable", "detail": "the test command wrote no JUnit XML and printed no summary"}
            else:
                ran = parsed["total"] - parsed["skipped"]
                rate = round(100.0 * (ran - parsed["failed"]) / ran, 1) if ran else 0.0
                findings = [{"severity": "high", "source": "tests", "text": f"{f['name']}: {f['message']}"}
                            for f in parsed["failures"]]
                if parsed["failed"] and not findings:
                    findings.append({"severity": "high", "source": "tests",
                                     "text": f"{parsed['failed']} test(s) failed; see {self.log.name}"})
                if not ran:
                    findings.append({"severity": "high", "source": "tests", "text": "No tests ran."})
                res = {"status": "measured", "value": rate, "total": parsed["total"], "failed": parsed["failed"],
                       "skipped": parsed["skipped"], "detail": f"{ran - parsed['failed']} of {ran} tests passed",
                       "findings": findings}
            res["command"] = cmd
        self.cache["tests"] = res
        return res

    def test_pass_rate(self) -> dict:
        return self.tests()

    def coverage(self) -> dict:
        tests = self.tests()
        cmd = self.conf.get("coverage_command") or self.cache.get("auto_coverage")
        if cmd:
            run_cmd(cmd.format(**self.paths), self.workspace, self.log, self.timeout, self.env)
        cov = parse_coverage(Path(self.paths["coverage"]))
        if cov is None:
            if tests["status"] == "unavailable":
                why = tests["detail"]
            elif Path(self.paths["coverage"]).exists():
                why = "the coverage report lists no source files"
            elif not cmd and not has_module("coverage"):
                why = "coverage.py is not installed"
            else:
                why = "no coverage report was written"
            return {"status": "unavailable", "detail": why}
        low = [f"{name}: {round(pct, 1)}%" for name, pct in cov["files"][:8] if pct < 100]
        return {"status": "measured", "value": cov["percent"], "detail": f"{cov['percent']}% of lines covered",
                "findings": [{"severity": "medium", "source": "coverage", "text": f"Least covered: {x}"} for x in low]}

    def security_findings(self) -> dict:
        mode = self.conf.get("security_source") or "auto"
        ran, found = ([], [])
        if mode in ("auto", "tools", "both"):
            ran, found = security_tools(self.workspace, self.out, self.log, self.timeout, self.env)
        sources = list(ran)
        if mode in ("report", "both") or (mode == "auto" and not ran):
            text, problem = self.source("security", "security audit")
            if text is None and not ran:
                return problem
            if text is not None:
                found += parse_security_report(text)
                sources.append("security report")
        if mode == "tools" and not ran:
            return {"status": "unavailable", "detail": "none of bandit, semgrep or pip-audit is installed"}
        counts = {lvl: sum(f["severity"] == lvl for f in found) for lvl in LEVELS}
        return {"status": "measured", "counts": counts, "findings": found,
                "detail": ", ".join(f"{n} {lvl}" for lvl, n in counts.items() if n) or "no findings",
                "sources": sources}

    def acceptance_pass(self) -> dict:
        text, problem = self.source("tester", "test report")
        if text is None:
            return problem
        ac = parse_acceptance(text)
        if not ac["total"]:
            return {"status": "unavailable", "detail": "the test report has no acceptance table"}
        return {"status": "measured", "value": round(100.0 * ac["pass"] / ac["total"], 1),
                "detail": f"{ac['pass']} of {ac['total']} criteria pass",
                "findings": [{"severity": "high", "source": "acceptance", "text": f"{i['id']} fails ({i['test']})"}
                             for i in ac["items"] if i["result"] != "PASS"]}

    def review_must_fix(self) -> dict:
        text, problem = self.source("reviewer", "code review")
        if text is None:
            return problem
        review = parse_review(text)
        return {"status": "measured", "value": review["count"], "detail": f"{review['count']} must-fix",
                "findings": [{"severity": "high", "source": "review", "text": t} for t in review["items"]]}

    def run_tokens(self) -> dict:
        n = int(self.ctx.get("run_tokens") or 0)
        return {"status": "measured", "value": n, "detail": f"{n:,} tokens so far", "findings": []}


def at_least(level: str) -> list:
    return LEVELS[:LEVELS.index(level) + 1] if level in LEVELS else LEVELS[:2]


def evaluate(gate: dict, ctx: dict) -> dict:
    """Measures every condition of a gate step. Returns the verdict, per-condition results and findings."""
    Path(ctx["out_dir"]).mkdir(parents=True, exist_ok=True)
    m = Measure(ctx)
    when_missing = (ctx.get("conf") or {}).get("when_unavailable") or "warn"
    results, findings = [], []
    for cond in gate.get("conditions") or []:
        meta = METRICS.get(cond["metric"], {})
        got = m.get(cond["metric"])
        value = got.get("value")
        cond_findings = got.get("findings") or []
        if cond["metric"] == "security_findings" and got["status"] == "measured":
            levels = at_least(cond.get("level") or "high")
            cond_findings = [f for f in cond_findings if f["severity"] in levels]
            value = len(cond_findings)
        status = got["status"]
        if status == "measured":
            status = "pass" if OPS[cond["op"]](value, cond["value"]) else "fail"
        elif status == "unavailable" and when_missing == "block":
            status = "fail"
        outcome = "ok" if status in ("pass", "unavailable") else cond["severity"]
        if status == "unavailable" and when_missing == "ignore":
            outcome = "ignored"
        results.append({"metric": cond["metric"], "label": meta.get("label", cond["metric"]), "op": cond["op"],
                        "threshold": cond["value"], "level": cond.get("level"), "severity": cond["severity"],
                        "unit": meta.get("unit", ""), "value": value, "status": status, "outcome": outcome,
                        "detail": got.get("detail", ""), "sources": got.get("sources")})
        if status in ("fail", "stale"):
            findings += [dict(f, metric=cond["metric"], gate_severity=cond["severity"]) for f in cond_findings] or \
                [{"metric": cond["metric"], "gate_severity": cond["severity"], "severity": "high",
                  "source": cond["metric"], "text": got.get("detail", "")}]
    blocking = sum(r["outcome"] == "block" for r in results)
    return {"verdict": "FAIL" if blocking else "PASS", "results": results, "findings": findings,
            "blocking": blocking, "warnings": sum(r["outcome"] == "warn" for r in results) +
            sum(r["status"] == "unavailable" and r["outcome"] == "ok" for r in results),
            "tests": {k: v for k, v in m.cache.get("tests", {}).items() if k != "findings"} or None}


def describe(cond: dict) -> str:
    """A condition from the plan, e.g. 'Coverage >= 80% (block)'."""
    meta = METRICS.get(cond["metric"], {})
    level = f" ({cond['level']} or worse)" if cond.get("level") else ""
    unit = "%" if meta.get("unit") == "%" else ""
    return f"{meta.get('label', cond['metric'])}{level} {cond['op']} {cond['value']:,}{unit} ({cond['severity']})"


def fmt_value(r: dict) -> str:
    if r["value"] is None:
        return "n/a"
    v = r["value"]
    return f"{v:,}{' ' + r['unit'] if r['unit'] and r['unit'] != '%' else r['unit']}"


def condition_text(r: dict) -> str:
    level = f" ({r['level']}+)" if r.get("level") else ""
    unit = "%" if r["unit"] == "%" else ""
    return f"{r['label']}{level} {r['op']} {r['threshold']:,}{unit}"


def render_report(gate: dict, result: dict, visit: int, back_to: str | None) -> str:
    """docs/gate-report.md: every finding of a gate run, merged, for the Developer."""
    lines = [f"# {gate['title']}: run {visit}", "",
             f"Measured by Katana. Verdict **{result['verdict']}**: {result['blocking']} blocking, "
             f"{result['warnings']} warning(s).", "",
             "| Condition | Measured | Result | Severity | Detail |", "|---|---|---|---|---|"]
    for r in result["results"]:
        lines.append(f"| {condition_text(r)} | {fmt_value(r)} | {r['status'].upper()} | {r['severity']} | "
                     f"{r['detail'].replace('|', '/')} |")
    def section(items: list, n: int = 0) -> tuple:
        out, by = [], {}
        for f in items:
            by.setdefault(f["metric"], []).append(f)
        for metric, group in by.items():
            out += [f"### {METRICS.get(metric, {}).get('label', metric)}", ""]
            for f in group:
                n += 1
                out.append(f"{n}. [{f['severity'].upper()}] ({f['source']}) " + f["text"].replace("\n", "\n   "))
            out.append("")
        return out, n

    must = [f for f in result["findings"] if f.get("gate_severity") == "block"]
    warn = [f for f in result["findings"] if f.get("gate_severity") != "block"]
    if result["verdict"] == "FAIL":
        body, n = section(must)
        lines += ["", "## Findings to fix", "",
                  "Fix every item below, then commit. The gate measures again after the next build."
                  + (f" Failing this gate sends the work back to {back_to}." if back_to else ""), ""] + body
    else:
        lines += ["", "Every blocking condition is met."]
        n = 0
    if warn:
        lines += ["", "## Warnings", "", "Reported only; these do not block.", ""] + section(warn, n)[0]
    lines += ["", f"VERDICT: {result['verdict']}"]
    return "\n".join(lines) + "\n"
