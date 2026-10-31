"""A read-only view over finished runs: `factory runs` sums apps/*/.factory/run.json.

It reads the run records and writes nothing. It shows how many runs there are by status, how often
a step item was accepted on its first attempt, how many rounds the loops needed, and what each
model cost. It does not change who plays a role: the owner decides routing.
"""

from __future__ import annotations

import json
from pathlib import Path

from .a2a_transport import Json


def load_runs(apps_dir: Path) -> tuple[list[Json], list[str]]:
    """The run records under apps_dir, and the paths of records that cannot be read."""
    runs, unreadable = [], []
    for path in sorted(apps_dir.glob("*/.factory/run.json")):
        try:
            record = json.loads(path.read_text())
            if not isinstance(record, dict):
                raise ValueError("not an object")
        except (OSError, ValueError):
            unreadable.append(str(path.parent.parent.name))
            continue
        runs.append({**record, "app": path.parent.parent.name})
    return runs, unreadable


def first_pass(record: Json) -> tuple[int, int]:
    """(items accepted on their first attempt, items accepted at all) in one run. A loop is not an item."""
    seen_rejected: set[tuple[str, str | None]] = set()
    first = total = 0
    for e in record.get("events", []):
        key = (e["step"], e["item"])
        if e["role"] == "loop":
            continue
        if e["status"] == "rejected":
            seen_rejected.add(key)
        elif e["status"] == "accepted":
            total += 1
            first += key not in seen_rejected
    return first, total


def summarize(runs: list[Json]) -> Json:
    out: Json = {"runs": len(runs), "by_status": {}, "by_runbook": {}, "first_pass": None,
                 "loop_rounds": {}, "usage": {"calls": 0, "cost_usd": 0.0, "calls_without_cost": 0},
                 "by_model": {}}
    first = total = 0
    for r in runs:
        out["by_status"][r.get("status", "unknown")] = out["by_status"].get(r.get("status", "unknown"), 0) + 1
        book = out["by_runbook"].setdefault(r.get("runbook", "unknown"), {"runs": 0, "accepted": 0, "cost_usd": 0.0})
        book["runs"] += 1
        book["accepted"] += r.get("status") == "accepted"
        book["cost_usd"] = round(book["cost_usd"] + (r.get("usage") or {}).get("cost_usd", 0.0), 6)
        f, t = first_pass(r)
        first, total = first + f, total + t
        for name, loop in (r.get("loops") or {}).items():
            agg = out["loop_rounds"].setdefault(name, {"loops": 0, "rounds": 0, "accepted": 0})
            agg["loops"] += 1
            agg["rounds"] += loop["rounds_used"]
            agg["accepted"] += bool(loop["accepted"])
        u = r.get("usage") or {}
        for k in ("calls", "calls_without_cost"):
            out["usage"][k] += u.get(k, 0)
        out["usage"]["cost_usd"] = round(out["usage"]["cost_usd"] + u.get("cost_usd", 0.0), 6)
        for model, m in (u.get("by_model") or {}).items():
            agg = out["by_model"].setdefault(model, {"calls": 0, "input_tokens": 0, "output_tokens": 0,
                                                     "cost_usd": 0.0, "calls_without_cost": 0})
            for k in ("calls", "input_tokens", "output_tokens", "calls_without_cost"):
                agg[k] += m.get(k, 0)
            agg["cost_usd"] = round(agg["cost_usd"] + m.get("cost_usd", 0.0), 6)
    if total:
        out["first_pass"] = {"first_attempt": first, "accepted_items": total, "rate": round(first / total, 3)}
    return out


def render(summary: Json, unreadable: list[str]) -> str:
    if not summary["runs"]:
        return "No runs yet. Start one with `factory run --brief \"...\"`."
    s = summary
    lines = [f"Runs: {s['runs']} (" + ", ".join(f"{n} {k}" for k, n in sorted(s["by_status"].items())) + ")"]
    for name, b in sorted(s["by_runbook"].items()):
        lines.append(f"  {name}: {b['runs']} runs, {b['accepted']} accepted, ${b['cost_usd']:.4f}")
    fp = s["first_pass"]
    lines.append("First-attempt acceptance: "
                 + (f"{fp['first_attempt']} of {fp['accepted_items']} items ({fp['rate']:.0%})" if fp else "no accepted items"))
    if s["loop_rounds"]:
        lines.append("Loop rounds:")
        for name, a in sorted(s["loop_rounds"].items()):
            lines.append(f"  {name}: {a['rounds'] / a['loops']:.1f} rounds on average over {a['loops']} loops "
                         f"({a['accepted']} accepted)")
    u = s["usage"]
    unknown = f"; {u['calls_without_cost']} calls report no cost" if u["calls_without_cost"] else ""
    lines.append(f"LLM calls: {u['calls']}, ${u['cost_usd']:.4f}{unknown}")
    for model, m in sorted(s["by_model"].items()):
        lines.append(f"  {model}: {m['calls']} calls, {m['input_tokens']:,} input and {m['output_tokens']:,} "
                     f"output tokens, ${m['cost_usd']:.4f}")
    if unreadable:
        lines.append(f"Could not read: {', '.join(unreadable)}")
    return "\n".join(lines)
