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
            grade_link = cells[4].find("a", href=True)
            if grade_link:
                exam_url = urljoin(final, grade_link["href"])
                exam_html, exam_final = await client.session.fetch_authenticated(exam_url)
                exam = BeautifulSoup(exam_html, "html.parser")
                print("Exam details loaded.", flush=True)
                seen = set()
                for item in exam.find_all(string=re.compile("einsicht|notenvorbehalt", re.I)):
                    parent = item.find_parent(["tr", "p", "li", "div"])
                    text = clean_text(parent.get_text(" ", strip=True) if parent else str(item))
                    if text not in seen:
                        seen.add(text)
                        print("Review metadata:", text[:1500], flush=True)
                if not seen: print("No exam review announcement in exam details.", flush=True)
        reader = client.session._browser
        if reader is not None:
            page = await reader._context.new_page()
            await page.goto("https://ilias.studium.kit.edu/login.php?target=crs_2918330&client_id=produktiv&cmd=force_login&lang=de", wait_until="domcontentloaded")
            button = page.locator("#button_shib_login")
            if await button.count():
                await button.click()
                await page.wait_for_load_state("domcontentloaded")
            await asyncio.sleep(3)
            await page.goto("https://ilias.studium.kit.edu/goto_produktiv_crs_2918330.html", wait_until="domcontentloaded")
            await asyncio.sleep(2)
            course = BeautifulSoup(await page.content(), "html.parser")
            print("ILIAS course access:", "Abmelden" in course.get_text() or "Logout" in course.get_text(), flush=True)
            seen = set()
            for item in course.find_all(string=re.compile("einsicht", re.I)):
                parent = item.find_parent(["p", "li", "div"])
                text = clean_text(parent.get_text(" ", strip=True) if parent else str(item))
                if text not in seen:
                    seen.add(text)
                    print("ILIAS review metadata:", text[:1500], flush=True)
            if not seen: print("No inspection notice visible in ILIAS course overview.", flush=True)
        print("Diagnostics complete; no Telegram messages or state writes.", flush=True)

asyncio.run(main())
