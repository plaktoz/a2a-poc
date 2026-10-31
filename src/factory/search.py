"""Web search the factory owns, for researcher agents.

Native web search is not available through the gateway (Claude's WebSearch is rejected by
Bedrock, and Codex has no search tool there), so researchers search with this instead:

  python -m factory.search "query" [--sources wikipedia,openalex,...] [--limit N]

It queries public APIs that need no key, in parallel, and prints one JSON result per line:
{"source", "title", "url", "snippet", "year"}. The agent then opens the pages it wants.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

TIMEOUT = 12
UA = {"User-Agent": "factory-research/0.1 (+https://github.com/)", "Accept": "application/json"}

Result = dict[str, object]


def _get(url: str, params: dict[str, object]) -> object:
    req = urllib.request.Request(f"{url}?{urllib.parse.urlencode(params)}", headers=UA)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _plain(text: object, n: int = 300) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", str(text or "")).split())[:n]


def wikipedia(q: str, n: int) -> list[Result]:
    data = _get("https://en.wikipedia.org/w/api.php",
                {"action": "query", "list": "search", "srsearch": q, "srlimit": n, "format": "json"})
    return [{"title": h["title"], "snippet": _plain(h["snippet"]),
             "url": "https://en.wikipedia.org/wiki/" + urllib.parse.quote(h["title"].replace(" ", "_"))}
            for h in data["query"]["search"]]


def openalex(q: str, n: int) -> list[Result]:
    data = _get("https://api.openalex.org/works", {"search": q, "per-page": n})
    out = []
    for w in data["results"]:
        url = (w.get("primary_location") or {}).get("landing_page_url") or w.get("doi") or w["id"]
        out.append({"title": w.get("display_name"), "url": url, "year": w.get("publication_year"),
                    "snippet": f"cited by {w.get('cited_by_count', 0)}"})
    return out


def hackernews(q: str, n: int) -> list[Result]:
    data = _get("https://hn.algolia.com/api/v1/search", {"query": q, "hitsPerPage": n, "tags": "story"})
    return [{"title": h.get("title"), "year": (h.get("created_at") or "")[:4] or None,
             "url": h.get("url") or f"https://news.ycombinator.com/item?id={h['objectID']}",
             "snippet": f"{h.get('points', 0)} points, discussion: https://news.ycombinator.com/item?id={h['objectID']}"}
            for h in data["hits"]]


def github(q: str, n: int) -> list[Result]:
    data = _get("https://api.github.com/search/repositories", {"q": q, "per_page": n, "sort": "stars"})
    return [{"title": r["full_name"], "url": r["html_url"], "year": (r.get("pushed_at") or "")[:4] or None,
             "snippet": f"{r.get('stargazers_count', 0)} stars. {_plain(r.get('description'))}"}
            for r in data["items"]]


def stackoverflow(q: str, n: int) -> list[Result]:
    data = _get("https://api.stackexchange.com/2.3/search/advanced",
                {"q": q, "site": "stackoverflow", "order": "desc", "sort": "relevance", "pagesize": n})
    return [{"title": _plain(i["title"]), "url": i["link"],
             "snippet": f"score {i.get('score', 0)}, {'answered' if i.get('is_answered') else 'unanswered'}"}
            for i in data["items"]]


def npm(q: str, n: int) -> list[Result]:
    data = _get("https://registry.npmjs.org/-/v1/search", {"text": q, "size": n})
    return [{"title": o["package"]["name"], "snippet": _plain(o["package"].get("description")),
             "url": (o["package"].get("links") or {}).get("npm") or f"https://www.npmjs.com/package/{o['package']['name']}",
             "year": (o["package"].get("date") or "")[:4] or None}
            for o in data["objects"]]


SOURCES: dict[str, Callable[[str, int], list[Result]]] = {
    "wikipedia": wikipedia, "openalex": openalex, "hackernews": hackernews,
    "github": github, "stackoverflow": stackoverflow, "npm": npm,
}
DEFAULT_SOURCES = ("wikipedia", "openalex", "hackernews")


def search(q: str, sources: tuple[str, ...] = DEFAULT_SOURCES, limit: int = 5) -> tuple[list[Result], dict[str, str]]:
    """Results from every source that answered, and the error of each that did not."""
    results: list[Result] = []
    errors: dict[str, str] = {}
    with ThreadPoolExecutor(len(sources)) as pool:
        futures = {s: pool.submit(SOURCES[s], q, limit) for s in sources}
        for s, f in futures.items():
            try:
                results += [{"source": s, **r} for r in f.result()]
            except Exception as e:  # one source failing must not hide the others
                errors[s] = f"{type(e).__name__}: {e}"[:200]
    return results, errors


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("query")
    ap.add_argument("--sources", default=",".join(DEFAULT_SOURCES),
                    help=f"comma-separated, from: {', '.join(SOURCES)}")
    ap.add_argument("--limit", type=int, default=5, help="results per source")
    args = ap.parse_args(argv)
    sources = tuple(s.strip() for s in args.sources.split(",") if s.strip())
    if unknown := [s for s in sources if s not in SOURCES]:
        ap.error(f"unknown sources: {', '.join(unknown)}")
    results, errors = search(args.query, sources, max(1, min(args.limit, 20)))
    for r in results:
        print(json.dumps(r, ensure_ascii=False))
    for s, e in errors.items():
        print(f"# {s} failed: {e}", file=sys.stderr)
    return 0 if results else 1


if __name__ == "__main__":
    sys.exit(main())
