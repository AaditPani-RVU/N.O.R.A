"""MCP server — arXiv paper search via the public Atom API.

No credentials required. Uses export.arxiv.org/api/query (public, no auth).
"""

import sys
import json
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mcpforge import MCPServer

server = MCPServer("arxiv", version="1.0")

_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
}
_BASE = "https://export.arxiv.org/api/query"


def _fetch_xml(url: str) -> ET.Element:
    req = urllib.request.Request(url, headers={"User-Agent": "NORA-arXiv-MCP/1.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return ET.fromstring(resp.read())


def _entry_to_dict(entry: ET.Element, snippet_len: int = 300) -> dict:
    def txt(tag: str) -> str:
        el = entry.find(tag, _NS)
        return el.text.strip() if el is not None and el.text else ""

    title = txt("atom:title").replace("\n", " ")
    abstract = txt("atom:summary").replace("\n", " ")
    published = txt("atom:published")
    categories = [
        c.get("term", "")
        for c in entry.findall("atom:category", _NS)
    ]

    arxiv_id = ""
    pdf_link = ""
    abs_link = ""
    for link in entry.findall("atom:link", _NS):
        href = link.get("href", "")
        rel = link.get("rel", "")
        title_attr = link.get("title", "")
        if title_attr == "pdf":
            pdf_link = href
        elif rel == "alternate":
            abs_link = href
    # derive arXiv ID from the <id> element (URL form)
    id_el = entry.find("atom:id", _NS)
    if id_el is not None and id_el.text:
        arxiv_id = id_el.text.strip().split("/abs/")[-1]
    if not pdf_link and arxiv_id:
        pdf_link = f"https://arxiv.org/pdf/{arxiv_id}"

    authors = [
        (a.find("atom:name", _NS).text or "").strip()
        for a in entry.findall("atom:author", _NS)
        if a.find("atom:name", _NS) is not None
    ]

    return {
        "arxiv_id": arxiv_id,
        "title": title,
        "authors": authors,
        "abstract_snippet": abstract[:snippet_len] + ("…" if len(abstract) > snippet_len else ""),
        "pdf_link": pdf_link,
        "published": published,
        "categories": categories,
    }


@server.tool()
def search_arxiv(query: str, max_results: int = 5) -> str:
    """Search arXiv for research papers by keyword or topic.

    query: keyword or phrase to search for (e.g. 'diffusion models image generation')
    max_results: number of results to return (default 5, max 25)
    """
    max_results = max(1, min(int(max_results), 25))
    params = urllib.parse.urlencode({
        "search_query": f"all:{query}",
        "start": 0,
        "max_results": max_results,
        "sortBy": "relevance",
        "sortOrder": "descending",
    })
    root = _fetch_xml(f"{_BASE}?{params}")
    entries = root.findall("atom:entry", _NS)
    if not entries:
        return json.dumps({"results": [], "total_found": 0})

    total_el = root.find("{http://a9.com/-/spec/opensearch/1.1/}totalResults")
    total = int(total_el.text) if total_el is not None and total_el.text else len(entries)

    results = [_entry_to_dict(e) for e in entries]
    # drop full abstract and published/categories for search results (keep it concise)
    for r in results:
        del r["published"]
        del r["categories"]

    return json.dumps({"total_found": total, "results": results}, ensure_ascii=False, indent=2)


@server.tool()
def get_arxiv_paper(arxiv_id: str) -> str:
    """Fetch full details for a specific arXiv paper by its ID.

    arxiv_id: arXiv identifier (e.g. '2301.07041' or '2301.07041v2')
    """
    arxiv_id = arxiv_id.strip()
    params = urllib.parse.urlencode({"id_list": arxiv_id})
    root = _fetch_xml(f"{_BASE}?{params}")
    entries = root.findall("atom:entry", _NS)
    if not entries:
        return json.dumps({"error": f"No paper found for ID: {arxiv_id}"})

    entry = entries[0]
    # check for API-level error entry
    err_el = entry.find("atom:summary", _NS)
    id_el = entry.find("atom:id", _NS)
    if id_el is not None and "Error" in (id_el.text or ""):
        return json.dumps({"error": err_el.text.strip() if err_el is not None else "arXiv API error"})

    d = _entry_to_dict(entry, snippet_len=10000)
    d["abstract"] = d.pop("abstract_snippet")  # full text for single-paper fetch

    return json.dumps(d, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    server.run()
