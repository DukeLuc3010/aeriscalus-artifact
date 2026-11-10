# /// script
# requires-python = ">=3.12"
# dependencies = ["numpy==2.5.4", "scipy==1.17.1", "gherkin-official==42.0.1"]
# ///
import json
import math
import re
import statistics
import sys
from collections import Counter
from decimal import ROUND_HALF_DOWN, ROUND_HALF_UP, Decimal
from pathlib import Path

import numpy as np
from gherkin.errors import CompositeParserException
from gherkin.parser import Parser
from gherkin.pickles.compiler import Compiler
from gherkin.token_scanner import TokenScanner
from scipy import stats

DATA = json.loads(Path(__file__).with_name("data.json").read_text(encoding="utf-8"))

FILLER = re.compile(r"\bTODO\b|\.{3,}|\bFIXME\b|\bTBD\b|\bplaceholder\b|\bXXX\b|\b(?:insert|add|fill)\s+(?:here|later|value)\b", re.I)
GENERIC_NAMES = {"test", "tests", "feature", "untitled", "example", "sample", "new feature", "test feature"}
PARAMETERS = [
    ("P2.1", 0, 30, r"(?:wind|gust|crosswind|headwind|tailwind)[^.]{0,40}?(\d+(?:\.\d+)?)\s*m/s"),
    ("P2.2", -40, 60, r"(-?\d+(?:\.\d+)?)\s*(?:°C|degrees?\s*(?:C(?:elsius)?))"),
    ("P2.3", 0, 10000, r"(?:visibility|fog)[^.]{0,40}?(\d+(?:\.\d+)?)\s*m(?:eters?)?(?:\b|$)(?!\s*/)"),
    ("P2.4", 0, 1000, r"(\d+(?:\.\d+)?)\s*(?:m(?:eters?)?\s*(?:AGL|above\s*ground))"),
    ("P2.5", 1, 50, r"(?:GPS|GNSS|RTK|position)[^.]{0,40}?(?:accuracy|precision|error)[^.]{0,20}?(?:±|to\s+)?(\d+(?:\.\d+)?)\s*(?:m(?:eters?)?(?:\b|$))"),
    ("P2.6", 0, 100, r"(?:battery|charge|remaining)[^.]{0,30}?(\d+(?:\.\d+)?)\s*%"),
    ("P2.7", 0, 300, r"(?:sensor|LiDAR|lidar|camera|detection|vision|infrared|ToF)[^.]{0,40}?(?:range|distance)[^.]{0,20}?(\d+(?:\.\d+)?)\s*(?:m(?:eters?)?(?:\b|$))"),
    ("P2.8", 0, 30, r"(?:(?:ground|air|flight|max(?:imum)?|cruise|forward|closing)\s*speed|velocity|flying\s*at)[^.]{0,30}?(\d+(?:\.\d+)?)\s*m/s"),
    ("P2.9", 0, 50000, r"(?:distance|range|radius|away|from\s*home)[^.]{0,30}?(\d+(?:\.\d+)?)\s*m(?:eters?)?(?:\b|$)(?!\s*/)"),
    ("P2.10", 0.1, 7200, r"(?:within|after|for|timeout|duration|elapsed|time)[^.]{0,30}?(\d+(?:\.\d+)?)\s*(?:seconds?|s(?:\b|$))"),
]
CONVERSIONS = {
    "P2.8": (r"(?:speed|velocity)[^.]{0,30}?(\d+(?:\.\d+)?)\s*(?:kph|km/h|km\s*/\s*h)", 1 / 3.6),
    "P2.9": (r"(?:distance|range|radius|away|from\s*home)[^\n.]{0,30}?(\d+(?:\.\d+)?)\s*km", 1000),
    "P2.10": (r"(?:within|after|for|timeout|duration|elapsed|time)[^.]{0,30}?(\d+(?:\.\d+)?)\s*(?:minutes?|min)", 60),
    "P2.3": (r"(?:visibility|fog)[^.]{0,40}?(\d+(?:\.\d+)?)\s*km", 1000),
    "P2.5": (r"(?:GPS|GNSS|RTK|position)[^.]{0,40}?(?:accuracy|precision|error)[^.]{0,20}?(?:±|to\s+)?(\d+(?:\.\d+)?)\s*cm", 0.01),
}
RULE_LINE = re.compile(r"\s*(-{3,}|\*{3,}|_{3,})\s*")
FENCE = re.compile(r"```[a-zA-Z]*\n(.*?)```", re.S)
GHERKIN_LINE = re.compile(r'^\s*(Given|When|Then|And|But|\*|\||Examples:|Scenario|Scenario Outline:|Background:|Feature:|Rule:|@|#|""")')
INCIDENT_NAMES = [r"\b(AAIB|ASRS|NTSB|ACN)\b", r"\bFAA\b.{0,40}\b(incident|accident)", r"@source|@incident", r"\b(incident|accident|occurrence)\s+report"]
CRITIC_CATEGORIES = {"V1": "gherkin_syntax", "V2": "parameter_validity", "V5": "completeness", "V6": "style"}
JUDGE_EVENTS = {"m3_score": "m3_judge_failed", "m4_grounding": "m4_judge_failed", "m6_traceable": "m6_judge_failed"}
SIX = [("naive", "optimized", "m1_score"), ("optimized", "rag_only", "m3_score"), ("rag_only", "full", "m1_score"),
       ("rag_only", "full", "m2_score"), ("rag_only", "full", "m3_score"), ("rag_only", "full", "m4_grounding")]


def parse(text: str):
    try:
        return Parser().parse(TokenScanner(text))
    except CompositeParserException:
        return None


def parses(text: str) -> bool:
    try:
        Parser().parse(TokenScanner(text))
        return True
    except Exception:
        return False


def sanitize(text: str) -> str:
    return re.sub(r"@\S+(?:[ \t]+(?!@)\S+)*", lambda m: m.group(0).replace(" ", "_"), text)


def effective_keywords(steps: list[dict]) -> set[str]:
    found, current = set(), None
    for step in steps:
        keyword = step.get("keyword", "").strip()
        if keyword in ("Given", "When", "Then"):
            current = keyword
            found.add(keyword)
        elif keyword in ("And", "But", "*") and current:
            found.add(current)
    return found


def ordered(steps: list[dict]) -> bool:
    phase = -1
    for step in steps:
        rank = {"Given": 0, "When": 1, "Then": 2}.get(step.get("keyword", "").strip())
        if rank is not None:
            if rank < phase:
                return False
            phase = rank
    return True


def outline_ok(scenario: dict) -> bool:
    if scenario.get("keyword", "") not in ("Scenario Outline", "Scenario Template"):
        return True
    examples = scenario.get("examples", [])
    placeholders = set(re.findall(r"<(\w+)>", " ".join(s.get("text", "") for s in scenario.get("steps", []))))
    return bool(examples) and all(placeholders <= {c.get("value", "") for c in e.get("tableHeader", {}).get("cells", [])} for e in examples)


def m1_criteria(text: str, background_aware: bool = False) -> dict[str, bool]:
    names = [f"S1.{i}" for i in range(1, 9)]
    if not text or not text.strip():
        return dict.fromkeys(names, False)
    document = parse(sanitize(text))
    if document is None or not document.get("feature"):
        return dict.fromkeys(names, False)
    feature = document["feature"]
    scenarios = [c["scenario"] for c in feature.get("children", []) if "scenario" in c]
    name = (feature.get("name") or "").strip()
    result = {
        "S1.1": True,
        "S1.2": bool(name) and name.lower() not in GENERIC_NAMES and len(name) > 3,
        "S1.3": len(scenarios) >= 1,
        "S1.4": all({"Given", "When", "Then"} <= effective_keywords(s.get("steps", [])) for s in scenarios),
        "S1.5": all(ordered(s.get("steps", [])) for s in scenarios),
        "S1.6": not any(s.get("steps") and s["steps"][0].get("keyword", "").strip() in ("And", "But", "*") for s in scenarios),
        "S1.7": all(outline_ok(s) for s in scenarios),
        "S1.8": not FILLER.findall(text),
    }
    if background_aware:
        types = [[step.get("type") for step in p["steps"]] for p in Compiler().compile({**document, "uri": "x.feature"})]
        result["S1.4"] = bool(types) and all({"Context", "Action", "Outcome"} <= set(t) for t in types)
    return result


def m1(text: str) -> float:
    return round(sum(m1_criteria(text).values()) / 8, 4)


def m2(text: str) -> float | None:
    if not text or not text.strip():
        return None
    checks = []
    for pid, low, high, pattern in PARAMETERS:
        values = [float(v) for v in re.findall(pattern, text, re.I)]
        if pid in CONVERSIONS:
            extra, factor = CONVERSIONS[pid]
            values += [float(v) * factor for v in re.findall(extra, text, re.I)]
        checks += [low <= v <= high for v in values]
    return round(sum(checks) / len(checks), 4) if checks else None


def cleanup(text: str) -> str:
    text = text.replace("**", "").replace("__", "")
    for fenced in FENCE.findall(text):
        if "Feature:" in fenced:
            text = fenced
            break
    lines = [line for line in text.split("\n") if not RULE_LINE.fullmatch(line)]
    start = next((i for i, line in enumerate(lines) if line.lstrip().startswith(("Feature:", "@", "#language"))), 0)
    lines = ("\n".join(lines[start:]).strip() + "\n").split("\n")
    last = max((i for i, line in enumerate(lines) if GHERKIN_LINE.match(line)), default=len(lines) - 1)
    return "\n".join(lines[: last + 1]).strip() + "\n"


def number(raw: str) -> float | None:
    return float(raw) if raw not in ("", None) else None


def grounding(total, hallucinated) -> float:
    return 0.0 if not total else (int(total) - int(hallucinated or 0)) / int(total)


def holm(pvalues: list[float]) -> list[float]:
    order = sorted(range(len(pvalues)), key=lambda i: pvalues[i])
    adjusted, running = [0.0] * len(pvalues), 0.0
    for rank, i in enumerate(order):
        running = max(running, min(1.0, (len(pvalues) - rank) * pvalues[i]))
        adjusted[i] = running
    return adjusted


def primary_test(before: list[float], after: list[float]) -> dict:
    d = np.asarray(after) - np.asarray(before)
    shapiro = float(stats.shapiro(d).pvalue)
    nonzero = d[d != 0]
    p = float(stats.ttest_rel(after, before).pvalue) if shapiro > 0.05 else float(stats.wilcoxon(nonzero).pvalue) if len(nonzero) else 1.0
    sd = d.std(ddof=1)
    half = stats.t.ppf(0.975, len(d) - 1) * sd / math.sqrt(len(d))
    return {"diff": float(d.mean()), "low": float(d.mean() - half), "high": float(d.mean() + half), "dz": float(d.mean() / sd), "p": p,
            "shapiro": shapiro, "before": float(np.mean(before)), "after": float(np.mean(after))}


def rank_biserial(before: list[float], after: list[float]) -> float:
    d = [b - a for a, b in zip(before, after) if b != a]
    ranks = stats.rankdata([abs(x) for x in d])
    plus = sum(r for r, x in zip(ranks, d) if x > 0)
    minus = sum(r for r, x in zip(ranks, d) if x < 0)
    return (plus - minus) / (plus + minus) if d else 0.0


def continuous(before: list[float], after: list[float]) -> tuple[float, float]:
    if all(a == b for a, b in zip(before, after)):
        return 1.0, 0.0
    return float(stats.wilcoxon(before, after, zero_method="wilcox").pvalue), rank_biserial(before, after)


def mcnemar(before: list[bool], after: list[bool]) -> float:
    gained = sum(1 for a, b in zip(before, after) if b and not a)
    lost = sum(1 for a, b in zip(before, after) if a and not b)
    return 1.0 if gained + lost == 0 else float(stats.binomtest(gained, gained + lost, 0.5).pvalue)


def pooled_d(after: list[float], before: list[float]) -> float:
    pooled = math.sqrt((statistics.stdev(after) ** 2 + statistics.stdev(before) ** 2) / 2)
    return 0.0 if pooled == 0 else (statistics.mean(after) - statistics.mean(before)) / pooled


def per_prompt(rows: list[dict], condition: str, metric: str) -> dict[str, float]:
    values: dict[str, list[float]] = {}
    for row in rows:
        if row["condition"] != condition or JUDGE_EVENTS.get(metric) in (row.get("judge_degraded") or []) or row.get(metric) is None:
            continue
        values.setdefault(row["prompt_id"], []).append(float(row[metric]))
    return {p: statistics.mean(v) for p, v in values.items()}


def paired_maps(rows: list[dict], before: str, after: str, metric: str) -> tuple[list[float], list[float]]:
    b, a = per_prompt(rows, before, metric), per_prompt(rows, after, metric)
    shared = sorted(set(b) & set(a))
    return [b[p] for p in shared], [a[p] for p in shared]


def oracle(text: str) -> set[str]:
    found = set()
    if m1(text) < 1.0:
        found.add("V1")
    score = m2(text)
    if score is not None and score < 1.0:
        found.add("V2")
    return found


class Check:
    def __init__(self):
        self.rows, self.failed = [], 0

    def __call__(self, section: str, quantity: str, printed: str, render) -> None:
        options: list[list[str]] = []

        def num(x: float, places: int) -> str:
            exact = Decimal(repr(float(x))).quantize(Decimal("1e-12"))
            step = Decimal(1).scaleb(-places)
            choices = {str(exact.quantize(step, rounding=mode)) for mode in (ROUND_HALF_UP, ROUND_HALF_DOWN)} | {f"{x:.{places}f}"}
            options.append(sorted({c.lstrip("-") if float(c) == 0 else c for c in choices}))
            return f"\x00{len(options) - 1}\x00"

        template = render(num).split("\x00")
        pattern = "".join(re.escape(part) if i % 2 == 0 else "(" + "|".join(map(re.escape, options[int(part)])) + ")" for i, part in enumerate(template))
        shown = "".join(part if i % 2 == 0 else "/".join(options[int(part)]) for i, part in enumerate(template))
        ok = re.fullmatch(pattern, printed) is not None
        self.failed += not ok
        self.rows.append((section, quantity, printed, shown, "ok" if ok else "MISMATCH"))

    def note(self, section: str, quantity: str, value: str, status: str) -> None:
        self.rows.append((section, quantity, "", value, status))


def main() -> int:
    check = Check()
    prompts = DATA["prompts"]
    primary = DATA["primary"]
    results = primary["results"]
    records = {(r["prompt_id"], r["condition"]): r for r in primary["records"]}
    ids = sorted({p["id"] for p in prompts})
    naive = [next(r for r in results if r["prompt_id"] == i and r["condition"] == "naive") for i in ids]
    system = [next(r for r in results if r["prompt_id"] == i and r["condition"] == "system") for i in ids]

    check("IV-A", "prompts", "40", lambda m: str(len(prompts)))
    check("IV-A", "risk categories x prompts", "4 x 10", lambda m: f"{len(Counter(p['category'] for p in prompts))} x {set(Counter(p['category'] for p in prompts).values()).pop()}")
    check("IV-A", "airframes x prompts", "2 x 20", lambda m: f"{len(Counter(p['drone_model'] for p in prompts))} x {set(Counter(p['drone_model'] for p in prompts).values()).pop()}")
    check("IV-A", "one-line / short / detailed prompts", "16/14/10", lambda m: "/".join(str(v) for v in sorted(Counter(p["detail_level"] for p in prompts).values(), reverse=True)))
    check("IV-A", "M1 / M3 binary criteria", "8/10", lambda m: f"{len(records[('C1-01', 'system')]['m1']['criteria'])}/{len(records[('C1-01', 'system')]['m3']['criteria'])}")
    setup = primary["setup"]
    check("IV-A", "generator / judge / temperature (recorded)", "gemini-2.5-flash/gemini-2.5-pro/0",
          lambda m: f"{setup['generation']['model']}/{setup['judge']['model']}/{max(setup['generation']['temperature'], setup['judge']['temperature']):g}")
    check("III-A", "critic passes (recorded)", "3", lambda m: str(setup["pipeline"]["max_inner_iterations"]))
    kb = setup["kb_counts"]
    check("III-B", "chunks: specs / incidents / examples / rules / total (recorded)", "46/747/3/31/827",
          lambda m: f"{kb['tech_specs']}/{kb['incidents']}/{kb['examples']}/{kb['rules']}/{sum(kb.values())}")

    tests = {}
    for metric in ("m1_score", "m2_score", "m3_score"):
        pairs = [(number(a[metric]), number(b[metric])) for a, b in zip(naive, system) if number(a[metric]) is not None and number(b[metric]) is not None]
        tests[metric] = primary_test([x for x, _ in pairs], [y for _, y in pairs]) | {"n": len(pairs)}
    adjusted = dict(zip(tests, holm([tests[k]["p"] for k in tests])))
    check("IV-A", "Shapiro-Wilk p, M1-M3", "all < 0.001", lambda m: "all < 0.001" if all(t["shapiro"] < 0.001 for t in tests.values()) else str([t["shapiro"] for t in tests.values()]))
    means = lambda rows, k: statistics.mean(float(r[k]) for r in rows)
    m4 = [grounding(r["m4_total_claims"], r["m4_hallucinated"]) for r in system]
    m6 = [r["m6_traceable"].lower() in ("true", "1") for r in system]
    for label, key, before, after in (("M1", "m1_score", means(naive, "m1_score"), means(system, "m1_score")),
                                       ("M2", "m2_score", tests["m2_score"]["before"], tests["m2_score"]["after"]),
                                       ("M3", "m3_score", means(naive, "m3_score"), means(system, "m3_score"))):
        t = tests[key]
        printed = {"M1": "0.562 / 0.941 / +0.378 / [0.226, 0.530] / 0.79", "M2": "0.989 / 0.971 / -0.018 / [-0.051, 0.016] / -0.20",
                   "M3": "0.810 / 0.892 / +0.083 / [0.045, 0.120] / 0.70"}[label]
        check("Table I", f"{label} baseline / system / diff / 95% CI / d_z", printed,
              lambda m, b=before, a=after, t=t: f"{m(b, 3)} / {m(a, 3)} / {'+' if t['diff'] > 0 else ''}{m(t['diff'], 3)} / "
                                                f"[{m(t['low'], 3)}, {m(t['high'], 3)}] / {m(t['dz'], 2)}")
    check("IV-B", "Holm-adjusted p, M1 and M3", "2.2e-04 each", lambda m: f"{adjusted['m1_score']:.1e} each" if f"{adjusted['m1_score']:.1e}" == f"{adjusted['m3_score']:.1e}" else "differ")
    check("Abstract", "Holm-adjusted p, M1 and M3", "< 0.001", lambda m: "< 0.001" if max(adjusted["m1_score"], adjusted["m3_score"]) < 0.001 else "no")
    check("IV-B", "M1 / M3 gain in pp", "37.8 / 8.3", lambda m: f"{m(100 * tests['m1_score']['diff'], 1)} / {m(100 * tests['m3_score']['diff'], 1)}")
    check("Table I", "M2 pairs where both outputs state a value", "29", lambda m: str(tests["m2_score"]["n"]))
    check("Table I", "outputs stating a checkable value", "30/40 / 39/40", lambda m: f"{sum(bool(r['m2_score']) for r in naive)}/40 / {sum(bool(r['m2_score']) for r in system)}/40")
    parsed_naive = [float(r["m1_score"]) for r in naive if float(r["m1_score"]) > 0]
    parsed_system = [float(r["m1_score"]) for r in system if float(r["m1_score"]) > 0]
    check("Table I", "conditional on parsing: baseline / system / diff", "0.938 / 0.965 / +0.027",
          lambda m: f"{m(statistics.mean(parsed_naive), 3)} / {m(statistics.mean(parsed_system), 3)} / +{m(statistics.mean(parsed_system) - statistics.mean(parsed_naive), 3)}")
    check("IV-B", "conditional difference in pp", "2.7", lambda m: m(100 * (statistics.mean(parsed_system) - statistics.mean(parsed_naive)), 1))
    check("Table I", "files failing to parse", "16/40 / 1/40", lambda m: f"{40 - len(parsed_naive)}/40 / {40 - len(parsed_system)}/40")
    check("Table I", "M4 grounding / M6 traceability (system)", "0.550 / 0.650", lambda m: f"{m(statistics.mean(m4), 3)} / {m(statistics.mean(m6), 3)}")
    check("Abstract", "M4 grounding in percent", "55%", lambda m: f"{m(100 * statistics.mean(m4), 0)}%")
    check("IV-B", "traceable scenarios", "26 of 40", lambda m: f"{sum(m6)} of {len(m6)}")
    check("IV-B", "scenarios without a checkable value (baseline / system)", "10 / 1", lambda m: f"{sum(r['m2_params_found'] in ('', '0') for r in naive)} / {sum(r['m2_params_found'] in ('', '0') for r in system)}")

    texts = {k: v["feature_text"] for k, v in records.items()}
    cleaned = {c: [m1(cleanup(texts[(i, c)])) for i in ids] for c in ("naive", "system")}
    gain = np.asarray(cleaned["system"]) - np.asarray(cleaned["naive"])
    check("IV-B", "after cleanup: unparseable baseline files / gain in pp / unadjusted p", "11 / 25.3 / 0.002",
          lambda m: f"{sum(s == 0 for s in cleaned['naive'])} / {m(100 * gain.mean(), 1)} / {m(stats.wilcoxon(cleaned['naive'], cleaned['system']).pvalue, 3)}")
    check("IV-A", "baseline outputs naming an incident report", "0 of 40", lambda m: f"{sum(any(re.search(p, texts[(i, 'naive')], re.I) for p in INCIDENT_NAMES) for i in ids)} of 40")
    tagged = []
    for i in ids:
        tag = re.search(r"@source:([^\n]*)", texts[(i, "system")])
        value = tag.group(1).strip() if tag else ""
        if not value or re.match(r"(N/A|no\b)", value, re.I) or "tech_spec" in value:
            continue
        index_reference = re.search(r"(incident|kb)\D{0,12}\d", value, re.I)
        context = records[(i, "system")]["retrieval_context"]
        agency_reference = re.search(r"AAIB|NTSB|ASRS|FAA", value) and any(t in context for t in re.findall(r"\d{4}-\d{2}-\d{2}|\d{6,}", value))
        if index_reference or agency_reference:
            tagged.append(i)
    check("IV-B", "system scenarios tagging a retrieved incident report", "7 of 40", lambda m: f"{len(tagged)} of 40")
    inner = [float(r["m5_inner_iters"]) for r in system]
    check("IV-B", "first-attempt pass / mean iterations / mean seconds", "87.5 / 1.15 / 30.5",
          lambda m: f"{m(100 * statistics.mean(v == 1 for v in inner), 1)} / {m(statistics.mean(inner), 2)} / {m(statistics.mean(float(r['m5_time_seconds']) for r in system), 1)}")
    figure = records[("C1-01", "system")]
    check("Fig. 2", "C3-01 baseline M1 criteria passed / C1-01 M1 / iterations / critic score", "0 of 8 / 1.0 / 1 / 1.00",
          lambda m: f"{sum(m1_criteria(texts[('C3-01', 'naive')]).values())} of 8 / {m1(figure['feature_text'])} / {figure['m5']['inner_iterations']} / "
                    f"{re.search(r'Critic score: ([0-9.]+)', figure['feature_text']).group(1)}")
    check("III-D", "12 m/s wind limit in the retrieved specification context of C1-01", "yes", lambda m: "yes" if re.search(r"Max Wind Resistance \| 12 m/s", figure["retrieval_context"]) else "no")
    extracted = DATA["extracted"]
    fallback_contexts = len({r["location"][2] for r in DATA["redactions"] if r["location"][0] == "primary" and r["location"][-1] == "retrieval_context"})
    check("III-B", "primary contexts containing fallback narrative text (reconstructed)", "27 of 40", lambda m: f"{fallback_contexts} of 40")
    chunks = extracted["knowledge_base_chunks"]
    check.note("III-B", "incident chunks: AAIB / FAA / NTSB / fallback narratives",
               f"{chunks['incidents/aaib']} / {chunks['incidents/faa']} / {chunks['incidents/ntsb']} / {chunks['incidents/fallback narratives']}", "extracted")
    check.note("III-B", "fallback narratives in the knowledge base", str(extracted["fallback_narratives"]), "extracted")

    second = {r["prompt_id"]: r for r in DATA["second_judge"]["records"]}
    subset = sorted(second)
    check("IV-B", "second-judge prompts", "20, first five per category", lambda m: f"{len(subset)}, " + ("first five per category" if subset == [f"C{c}-0{n}" for c in range(1, 5) for n in range(1, 6)] else str(subset)))
    gem3 = [records[(i, "system")]["m3"]["score"] for i in subset]
    gpt3 = [second[i]["m3"]["score"] for i in subset]
    check("IV-B", "M3 Pearson r (means gemini vs gpt-4o)", "0.38 (0.89 vs. 0.99)", lambda m: f"{m(stats.pearsonr(gem3, gpt3)[0], 2)} ({m(statistics.mean(gem3), 2)} vs. {m(statistics.mean(gpt3), 2)})")
    gem6 = [bool(records[(i, "system")]["m6"]["traceable"]) for i in subset]
    gpt6 = [bool(second[i]["m6"]["traceable"]) for i in subset]
    agree = sum(a == b for a, b in zip(gem6, gpt6)) / len(subset)
    chance = statistics.mean(gem6) * statistics.mean(gpt6) + (1 - statistics.mean(gem6)) * (1 - statistics.mean(gpt6))
    check("IV-B", "M6 kappa (rated traceable gemini vs gpt-4o)", "0.24 (0.65 vs. 0.20)",
          lambda m: f"{m((agree - chance) / (1 - chance), 2)} ({m(statistics.mean(gem6), 2)} vs. {m(statistics.mean(gpt6), 2)})")
    empty = [i for i in subset if not second[i]["m4"].get("total_claims")]
    rest = [i for i in subset if i not in empty]
    g4 = [1 - records[(i, "system")]["m4"]["hallucination_rate"] for i in rest]
    o4 = [1 - second[i]["m4"]["hallucination_rate"] for i in rest]
    check("IV-B", "M4: no-claim scenarios; others; r; levels", "4; 16; 0.71; 0.47 vs. 0.62",
          lambda m: f"{len(empty)}; {len(rest)}; {m(stats.pearsonr(g4, o4)[0], 2)}; {m(statistics.mean(g4), 2)} vs. {m(statistics.mean(o4), 2)}")
    all_g = [grounding(records[(i, "system")]["m4"]["total_claims"], records[(i, "system")]["m4"]["hallucinated_claims"]) for i in subset]
    all_o = [grounding(second[i]["m4"]["total_claims"], second[i]["m4"]["hallucinated_claims"]) for i in subset]
    check("IV-B", "M4 r over all 20, no-claim scored zero", "-0.011", lambda m: m(stats.pearsonr(all_g, all_o)[0], 3))

    ablation = {(r["prompt_id"], r["condition"]): r for r in DATA["ablation"]["records"]}
    abl_ids = sorted({p for p, _ in ablation})

    def ablation_value(record: dict, metric: str):
        if metric == "M4":
            return grounding(record["m4"].get("total_claims"), record["m4"].get("hallucinated_claims"))
        if metric == "M6":
            return 1.0 if record["m6"].get("traceable") is True else 0.0
        return record[metric.lower()].get("score")

    table = {c: {m: [ablation_value(ablation[(i, c)], m) for i in abl_ids] for m in ("M1", "M2", "M3", "M4", "M6")} for c in ("naive", "rag_only", "system")}
    avg = lambda c, m: statistics.mean(v for v in table[c][m] if v is not None)
    tests_ab = {}
    for metric in ("M1", "M3", "M4", "M6"):
        for a, b in (("naive", "rag_only"), ("rag_only", "system"), ("naive", "system")):
            pairs = [(x, y) for x, y in zip(table[a][metric], table[b][metric]) if x is not None and y is not None]
            xs, ys = [float(x) for x, _ in pairs], [float(y) for _, y in pairs]
            p = float("nan") if all(x == y for x, y in pairs) else float(stats.wilcoxon(xs, ys, zero_method="wilcox").pvalue)
            tests_ab[(metric, a, b)] = {"p": p, "d": pooled_d(ys, xs), "means": (statistics.mean(xs), statistics.mean(ys))}
    family = sorted((v["p"], k) for k, v in tests_ab.items() if not math.isnan(v["p"]))
    significant = {k for rank, (p, k) in enumerate(family) if p < 0.05 / (len(family) - rank)}
    a1 = tests_ab[("M3", "naive", "rag_only")]
    check("IV-C", "ablation prompts", "20", lambda m: str(len(abl_ids)))
    check("IV-C", "M3 baseline -> retrieval-only; d; Holm; tests", "0.860 to 0.970, d = 2.26, significant, 12 tests",
          lambda m: f"{m(a1['means'][0], 3)} to {m(a1['means'][1], 3)}, d = {m(a1['d'], 2)}, "
                    f"{'significant' if ('M3', 'naive', 'rag_only') in significant else 'not significant'}, {len(family)} tests")
    critic_ab = [k for k in tests_ab if k[1:] == ("rag_only", "system")]
    check("IV-C", "retrieval-only vs full: significant; max |d|", "0; 0.20",
          lambda m: f"{sum(k in significant for k in critic_ab)}; {m(max(abs(tests_ab[k]['d']) for k in critic_ab), 2)}")
    check("IV-C", "M1 per condition; max M2; max M3", "0.98/0.98/0.98; 1.00; 0.97",
          lambda m: "/".join(m(avg(c, "M1"), 2) for c in ("naive", "rag_only", "system")) + f"; {m(max(avg(c, 'M2') for c in table), 2)}; {m(max(avg(c, 'M3') for c in table), 2)}")
    check("Table I", "ablation M3 / M4 / M6", "0.860/0.970/0.965; (0)/0.500/0.433; (0)/0.150/0.200",
          lambda m: "; ".join(("/".join(m(avg(c, k), 3) for c in ("naive", "rag_only", "system")) if k == "M3" else
                               f"({avg('naive', k):g})/" + "/".join(m(avg(c, k), 3) for c in ("rag_only", "system"))) for k in ("M3", "M4", "M6")))

    runs = DATA["replication"]["runs"]
    rows = [r for run in runs for r in run["rows"]]
    check("IV-C", "scored artifacts per run", "160/160", lambda m: "/".join(str(len(run["rows"])) for run in runs))
    table2 = {c: {k: per_prompt(rows, c, k) for k in ("m1_score", "m3_score", "m4_grounding", "m6_traceable")} for c in ("naive", "optimized", "rag_only", "full")}
    cell = lambda c, k, m: m(round(statistics.mean(table2[c][k].values()), 4), 3) if table2[c][k] and c in ("rag_only", "full") or k in ("m1_score", "m3_score") else "-"
    for c, printed in (("naive", "0.823 0.799 - -"), ("optimized", "0.986 0.850 - -"), ("rag_only", "0.975 0.843 0.689 0.600"), ("full", "0.981 0.848 0.760 0.688")):
        check("Table II", f"{c}: M1 M3 M4 M6", printed, lambda m, c=c: " ".join(cell(c, k, m) for k in ("m1_score", "m3_score", "m4_grounding", "m6_traceable")))
    six = [continuous(*paired_maps(rows, a, b, k)) for a, b, k in SIX]
    six_adjusted = holm([p for p, _ in six])
    survivors = [SIX[i] for i, q in enumerate(six_adjusted) if q < 0.05]
    check("IV-C", "pre-specified family: survivors; p; adjusted; r_rb", "1 (naive->optimized M1); 0.0029; 0.017; 0.87",
          lambda m: f"{len(survivors)} ({'->'.join(survivors[0][:2])} M1); {m(six[0][0], 4)}; {m(six_adjusted[0], 3)}; {m(six[0][1], 2)}" if len(survivors) == 1 and survivors[0] == SIX[0] else str(survivors))
    m3_means = [statistics.mean(table2[c]["m3_score"].values()) for c in ("optimized", "rag_only", "full")]
    check("IV-C", "M3 range optimized and retrieval conditions; optimized vs retrieval-only p", "0.843-0.850 (< 1 pp); 0.55",
          lambda m: f"{m(min(m3_means), 3)}-{m(max(m3_means), 3)} ({'< 1 pp' if max(m3_means) - min(m3_means) < 0.01 else '>= 1 pp'}); {m(six[1][0], 2)}")
    intervals = []
    for metric in ("m1_score", "m3_score"):
        o, f = paired_maps(rows, "optimized", "full", metric)
        d = np.asarray(f) - np.asarray(o)
        half = stats.t.ppf(0.975, len(d) - 1) * d.std(ddof=1) / math.sqrt(len(d))
        intervals.append((d.mean(), d.mean() - half, d.mean() + half))
    check("IV-C", "full minus optimized: M1, M3 with 95% CI", "-0.005 [-0.018, 0.008], -0.002 [-0.033, 0.028]",
          lambda m: ", ".join(f"{m(x, 3)} [{m(lo, 3)}, {m(hi, 3)}]" for x, lo, hi in intervals))
    retrieval_rows = [r for r in rows if r["condition"] in ("rag_only", "full")]
    check("IV-C", "retrieval-condition scenarios citing a source", "0 of 160", lambda m: f"{sum(bool(re.search(r'@source|NTSB|AAIB|ASRS|FAA', r.get('feature_text') or '')) for r in retrieval_rows)} of {len(retrieval_rows)}")
    per_run = [continuous(*paired_maps(run["rows"], "rag_only", "full", "m4_grounding"))[0] for run in runs]
    check("IV-C", "critic M4: two-run p; per run; Holm-adjusted", "0.017; 0.30 and 0.033; 0.084",
          lambda m: f"{m(six[5][0], 3)}; {m(per_run[0], 2)} and {m(per_run[1], 3)}; {m(six_adjusted[5], 3)}")
    check("IV-C", "naive M1, replication vs primary", "0.823 vs. 0.562", lambda m: f"{cell('naive', 'm1_score', m)} vs. {m(means(naive, 'm1_score'), 3)}")
    degraded = [r for r in rows if r.get("judge_degraded")]
    check.note("IV-C", "rows with unparseable judge output, excluded per metric", f"{len(degraded)} of {len(rows)}", "recorded")
    check.note("IV-C", "prompts with a retrieval-only M4 value", str(len(table2["rag_only"]["m4_grounding"])), "recorded")

    eleven = []
    for a, b in (("naive", "optimized"), ("optimized", "rag_only"), ("rag_only", "full")):
        for k in ("m1_score", "m2_score", "m3_score", "m4_grounding"):
            before, after = paired_maps(rows, a, b, k)
            if len(before) >= 2:
                eleven.append((f"{a}->{b} {k}", continuous(before, after)[0]))
        before, after = paired_maps(rows, a, b, "m6_traceable")
        if len(before) >= 2:
            eleven.append((f"{a}->{b} m6_traceable", mcnemar([bool(x) for x in before], [bool(x) for x in after])))
    eleven_adjusted = dict(zip([n for n, _ in eleven], holm([p for _, p in eleven])))
    check.note("sensitivity", f"Holm over all {len(eleven)} comparisons: naive->optimized M1 / retrieval-only->full M4 adjusted p",
               f"{eleven_adjusted['naive->optimized m1_score']:.3f} / {eleven_adjusted['rag_only->full m4_grounding']:.3f}", "not in the paper")

    full = [r for r in rows if r["condition"] == "full" and r.get("trajectory")]
    capped = [r for r in full if len(r["trajectory"]["iterations"]) == 3]
    check("IV-C", "trajectories; at the three-pass cap; still failing", "80; 41; 31",
          lambda m: f"{len(full)}; {len(capped)}; {sum((r['trajectory']['iterations'][-1].get('critic_report') or {}).get('passed') is False for r in capped)}")
    present = detected = false_issue = repaired = attempts = 0
    first_issues: Counter = Counter()
    severities = 0
    for r in full:
        draft = r["trajectory"]["initial_draft"]
        iterations = r["trajectory"]["iterations"]
        report = iterations[0]["critic_report"] if iterations else {"issues": []}
        issues = report.get("issues") or []
        first_issues.update(i.get("category") for i in issues)
        severities += len(issues)
        raised = {i.get("category") for i in issues}
        has = "V2" in oracle(draft)
        flagged = CRITIC_CATEGORIES["V2"] in raised
        present += has
        detected += has and flagged
        false_issue += (not has) and flagged
        if has and flagged and iterations and iterations[0].get("regenerated") is not None:
            attempts += 1
            repaired += "V2" not in oracle(iterations[0]["regenerated"])
    check("IV-C", "parameter problems detected / found by the checker; repaired", "8 of 13; 3", lambda m: f"{detected} of {present}; {repaired}")
    check("IV-C", "critic parameter flags with vs without a checker-found problem", "62% vs. 64%",
          lambda m: f"{m(100 * detected / present, 0)}% vs. {m(100 * false_issue / (len(full) - present), 0)}%")
    check("IV-C", "first-pass issues on completeness or style", "181 of 291", lambda m: f"{first_issues['completeness'] + first_issues['style']} of {severities}")
    drafts = [r["trajectory"]["initial_draft"] or "" for r in full]
    flagged_drafts = [d for d in drafts if m1(d) < 1 and m1_criteria(d)["S1.1"]]
    plain = Counter("+".join(k for k, v in m1_criteria(d).items() if not v) for d in flagged_drafts)
    background = Counter("+".join(k for k, v in m1_criteria(d, background_aware=True).items() if not v) or "none" for d in flagged_drafts)
    check("IV-C", "first drafts parsing; missing an M1 criterion = Background only + repeated When/Then", "80 of 80; 14 = 9 + 5",
          lambda m: f"{sum(parses(d) for d in drafts)} of {len(drafts)}; {len(flagged_drafts)} = {background['none']} + {plain['S1.4+S1.5']}")

    def power(dz: float) -> float:
        c = stats.t.ppf(0.975, 39)
        return 1 - stats.nct.cdf(c, 39, dz * math.sqrt(40)) + stats.nct.cdf(-c, 39, dz * math.sqrt(40))
    low, high = 0.1, 1.0
    for _ in range(60):
        low, high = ((low + high) / 2, high) if power((low + high) / 2) < 0.8 else (low, (low + high) / 2)
    check("V-B", "d_z with 80% power at n = 40, alpha = 0.05", "0.45", lambda m: m(high, 2))

    consistency = []
    recorded_m1 = [(r["feature_text"], r["m1"]["score"]) for r in list(records.values()) + list(ablation.values())] + [(r["feature_text"], r["m1_score"]) for r in rows if r.get("feature_text") is not None]
    consistency.append(("M1 recomputed from every output equals the recorded score", sum(abs(m1(t) - s) < 1e-9 for t, s in recorded_m1), len(recorded_m1)))
    recorded_m2 = [(r["feature_text"], r["m2"]["score"]) for r in list(records.values()) + list(ablation.values())] + [(r["feature_text"], r["m2_score"]) for r in rows if r.get("feature_text") is not None]
    consistency.append(("M2 recomputed from every output equals the recorded score", sum((m2(t) is None and s is None) or (m2(t) is not None and s is not None and abs(m2(t) - s) < 1e-9) for t, s in recorded_m2), len(recorded_m2)))
    judged = [r for r in list(records.values()) + list(ablation.values()) + list(second.values()) if r.get("m3", {}).get("criteria")]
    consistency.append(("M3 score equals the share of passed criteria", sum(abs(sum(c["passed"] for c in r["m3"]["criteria"]) / len(r["m3"]["criteria"]) - r["m3"]["score"]) < 1e-9 for r in judged), len(judged)))
    with_claims = [r for r in list(records.values()) + list(ablation.values()) + list(second.values()) if r.get("m4", {}).get("claims")]
    consistency.append(("M4 counts equal the judged claims", sum(len(r["m4"]["claims"]) == r["m4"]["total_claims"] and sum(not c["grounded"] for c in r["m4"]["claims"]) == r["m4"]["hallucinated_claims"] for r in with_claims), len(with_claims)))
    traced = [r for r in list(records.values()) + list(ablation.values()) + list(second.values()) if r.get("m6", {}).get("criteria")]
    verdict = lambda r: {c["id"]: c["passed"] for c in r["m6"]["criteria"]}
    consistency.append(("M6 verdict equals T6.1 and any of T6.2-T6.4", sum(bool(r["m6"]["traceable"]) == (verdict(r)["T6.1"] and any(verdict(r).get(k) for k in ("T6.2", "T6.3", "T6.4"))) for r in traced), len(traced)))
    overwritten = [i for i in ids if abs(records[(i, "naive")]["m3"]["score"] - float(next(r for r in naive if r["prompt_id"] == i)["m3_score"])) > 1e-9]
    for label, good, total in consistency:
        check.note("records", label, f"{good} of {total}", "ok" if good == total else "MISMATCH")
        check.failed += good != total
    check.note("records", "baseline M3 records differing from the analysed score (second-judge overwrite, see data.json notes)", f"{len(overwritten)} of 40", "expected")

    print("Recomputation of the reported RQ1 and RQ2 analyses from the archived records in data.json. No model is called.\n")
    width = max(len(r[1]) for r in check.rows)
    for section, quantity, printed, value, status in check.rows:
        print(f"{section:<12} {quantity:<{width}}  {printed:<34} {value:<34} {status}")
    printed = [r for r in check.rows if r[2]]
    print(f"\n{sum(r[4] == 'ok' for r in printed)} of {len(printed)} printed values recomputed; "
          f"{sum(r[4] == 'ok' for r in check.rows if r[0] == 'records')} record consistency checks passed; {check.failed} mismatches.")
    return 1 if check.failed else 0


if __name__ == "__main__":
    sys.exit(main())
