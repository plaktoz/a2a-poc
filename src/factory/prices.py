"""Model prices for the calls whose provider reports no cost (FR-30). Codex is the one today.

The prices live in `docs/model-prices.md`, in the fenced block that starts with ```json prices.
The engine reads that block for each Codex call, so a price change is one file, reviewed in git.

`python -m factory.prices` refreshes the block from the public feed at https://www.llm-prices.com/
(its `current-v1.json`). It writes nothing when a model is missing from the feed. A model with no
entry in the table has an unknown cost, and the run total counts that call as unknown.

The feed gives USD per 1,000,000 tokens. The table uses the same unit.
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .paths import ROOT

PRICES_FILE = ROOT / "docs" / "model-prices.md"
FEED_URL = "https://www.llm-prices.com/current-v1.json"
PER_TOKENS = 1_000_000
BLOCK = re.compile(r"```json prices\n(.*?)\n```", re.S)
TABLE = re.compile(r"<!-- prices:start -->\n.*?<!-- prices:end -->", re.S)


class PriceError(ValueError):
    """The price file cannot be read, or the feed does not hold a model that the table needs."""


@dataclass(frozen=True)
class Price:
    model: str  # the name the table gives the model, after the alias is resolved
    input: float  # USD per 1M tokens
    cached_input: float  # USD per 1M tokens; the input price when the table has none
    output: float


def load_table(path: Path = PRICES_FILE) -> dict[str, Any]:
    """The parsed price block. Raises PriceError if the block is missing or an alias is broken."""
    try:
        text = path.read_text()
    except OSError as e:
        raise PriceError(f"cannot read the price file {path}: {e}") from None
    m = BLOCK.search(text)
    if not m:
        raise PriceError(f"{path} has no ```json prices block")
    try:
        table = json.loads(m.group(1))
    except ValueError as e:
        raise PriceError(f"{path}: the prices block is not valid JSON: {e}") from None
    models = table.get("models")
    if not isinstance(models, dict) or not models:
        raise PriceError(f"{path}: the prices block has no models")
    for name, entry in models.items():
        target = entry.get("alias_of")
        if target is None:
            continue
        if target not in models or "alias_of" in models[target]:
            raise PriceError(f"{path}: {name} is an alias of {target}, which is not a priced model")
    return table


def lookup(model: str, table: dict[str, Any]) -> Price | None:
    """The price of a model, following its alias. None when the table has no entry for it."""
    entry = table["models"].get(model)
    if entry is None:
        return None
    if "alias_of" in entry:
        model, entry = entry["alias_of"], table["models"][entry["alias_of"]]
    cached = entry.get("cached_input")
    return Price(model, float(entry["input"]),
                 float(entry["input"] if cached is None else cached), float(entry["output"]))


def cost_usd(model: str, input_tokens: int, cached_tokens: int, output_tokens: int,
             table: dict[str, Any] | None = None) -> float | None:
    """The cost of one call, in USD. `input_tokens` counts the cached ones too (as the workers do).

    None when the table has no price for the model, so the call counts as unknown.
    """
    price = lookup(model, table if table is not None else load_table())
    if price is None:
        return None
    fresh = max(input_tokens - cached_tokens, 0)
    total = (fresh * price.input + cached_tokens * price.cached_input + output_tokens * price.output)
    return round(total / PER_TOKENS, 6)


def fetch_feed(url: str = FEED_URL) -> dict[str, Any]:
    # The site refuses the default Python user agent (403), so the request names itself.
    req = urllib.request.Request(url, headers={"User-Agent": "factory-prices/1.0 (+https://www.llm-prices.com/)"})
    with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 - a fixed https URL
        data = json.loads(r.read().decode())
    if not isinstance(data, dict) or not isinstance(data.get("prices"), list):
        raise PriceError(f"{url} does not look like the price feed (no `prices` list)")
    return data


def refresh(table: dict[str, Any], feed: dict[str, Any], today: str) -> dict[str, Any]:
    """Copy each priced model's numbers from the feed. Raises PriceError if one is missing."""
    rows = {row["id"]: row for row in feed["prices"]}
    missing = []
    for name, entry in table["models"].items():
        if "alias_of" in entry:
            continue
        feed_id = entry.get("feed_id", name)
        row = rows.get(feed_id)
        if row is None:
            missing.append(f"{name} (feed id {feed_id!r})")
            continue
        entry.update(input=row["input"], cached_input=row["input_cached"], output=row["output"])
    if missing:
        raise PriceError("not in the feed, so the table was not changed: " + ", ".join(missing))
    table["source"] = FEED_URL
    table["feed_updated_at"] = feed.get("updated_at")
    table["fetched_on"] = today
    return table


def render_table(table: dict[str, Any]) -> str:
    lines = ["| Model | Input | Cached input | Output | Feed id or alias |",
             "|---|---:|---:|---:|---|"]
    for name, entry in table["models"].items():
        if "alias_of" in entry:
            lines.append(f"| `{name}` | | | | alias of `{entry['alias_of']}` |")
            continue
        cached = entry.get("cached_input")
        cached_txt = "not listed" if cached is None else f"{cached:g}"
        lines.append(f"| `{name}` | {entry['input']:g} | {cached_txt} | {entry['output']:g} "
                     f"| `{entry.get('feed_id', name)}` |")
    return "\n".join(lines)


def build(text: str, table: dict[str, Any]) -> str:
    """The file text with its generated table and its price block replaced. Prose is kept."""
    block = "```json prices\n" + json.dumps(table, indent=2) + "\n```"
    generated = "<!-- prices:start -->\n" + render_table(table) + "\n<!-- prices:end -->"
    if not BLOCK.search(text) or not TABLE.search(text):
        raise PriceError("the price file needs the markers <!-- prices:start --> and <!-- prices:end -->")
    text = TABLE.sub(lambda _: generated, text, count=1)
    return BLOCK.sub(lambda _: block, text, count=1)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m factory.prices",
                                 description="Refresh docs/model-prices.md from the public price feed.")
    ap.add_argument("--feed", type=Path, help="a saved copy of the feed (default: fetch it)")
    ap.add_argument("--check", action="store_true", help="show what would change; write nothing")
    args = ap.parse_args(argv)
    try:
        feed = json.loads(args.feed.read_text()) if args.feed else fetch_feed()
        text = PRICES_FILE.read_text()
        old = load_table(PRICES_FILE)
        new = refresh(json.loads(json.dumps(old)), feed, datetime.date.today().isoformat())
        for name, entry in new["models"].items():
            before = old["models"][name]
            if before != entry and "alias_of" not in entry:
                print(f"{name}: input {before.get('input')} -> {entry['input']}, "
                      f"output {before.get('output')} -> {entry['output']}")
        updated = build(text, new)
    except (PriceError, OSError, ValueError, KeyError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if args.check:
        print("no change" if updated == text else "the file would change")
        return 0
    PRICES_FILE.write_text(updated)
    print(f"wrote {PRICES_FILE.relative_to(ROOT)}, feed updated {feed.get('updated_at')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
