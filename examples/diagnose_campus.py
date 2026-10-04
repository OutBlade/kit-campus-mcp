"""Read-only inspection of SSO readiness and exam-review metadata."""
import asyncio
import re
from urllib.parse import urljoin, urlparse, parse_qsl
from bs4 import BeautifulSoup
from kit_campus_mcp.client import KitCampusClient, _url
from kit_campus_mcp.parsers import clean_text

async def main():
    async with KitCampusClient() as client:
        guid = await client._program_guid(None)
        html, final = await client.session.fetch_authenticated(_url("study_tree", pguid=guid))
        soup = BeautifulSoup(html, "html.parser")
        table = soup.find("table", id="specific-contract-tree")
        assert table is not None, "Study tree was not read"
        print("Authenticated study tree read successfully.", flush=True)
        for row in table.find_all("tr", class_="brick"):
            cells = row.find_all("td")
            if len(cells) < 5 or not clean_text(cells[4].get_text()).startswith("("):
                continue
            for idx, cell in enumerate(cells):
                for tag in [cell, *cell.find_all(True)]:
                    for key in ("title", "data-content", "data-original-title", "aria-label"):
                        text = clean_text(str(tag.get(key) or ""))
                        if text:
                            text = re.sub(r"0x[0-9A-Fa-f]+", "[id]", text)
                            print(f"Cell {idx} {key}: {text[:700]}", flush=True)
                for link in cell.find_all("a"):
                    href = link.get("href", "")
                    parsed = urlparse(href)
                    onclick = link.get("onclick", "")
                    functions = re.findall(r"([a-zA-Z_][\w]*)\s*\(", onclick)
                    print(f"Cell {idx} link path={parsed.path[:100]} query_keys={[k for k,v in parse_qsl(parsed.query)]} functions={functions}", flush=True)
            for text in row.stripped_strings:
                if "einsicht" in text.lower(): print("Review metadata:", clean_text(text)[:700], flush=True)
        print("Diagnostics complete; no Telegram messages or state writes.", flush=True)

asyncio.run(main())
