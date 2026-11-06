"""Scrape every Bellhaven community from the public website.

How it finds communities:
  1. Walks the paginated directory at /communities by following "Next" links.
  2. Crawls every other page on the site (homepage, About, etc.) for /communities/<slug>
     links. New communities are sometimes announced before they reach the directory.
     Paths starting with /api are never crawled.
  3. Re-checks every community from the previous snapshot that steps 1 and 2 did not find,
     by fetching /communities/<slug> directly:
       - page loads (200): the community is kept, marked "unlisted (page live, no longer
         linked)". It is NOT treated as removed.
       - page not found (404): the community counts as removed.
       - anything else: the run stops.

Each community's detail page is the source of truth. The directory cards show only one
care-offering badge, so every detail page is fetched.

Retries: every page request is tried up to 3 times (waiting about 1s, then 2s) when it
times out, cannot connect, or the server returns a 5xx error. A 404 is not retried.

Safety checks (the run fails loudly instead of returning partial data):
  - A request that still fails after retries stops the run.
  - A 404 on a directory page or on a community found in the directory stops the run.
    A 404 on some other page found by the site crawl is skipped with a warning.
  - The directory states "N communities listed". If the directory walk finds a different
    number, the run stops.
  - Every detail page must have a name and a parseable address.
  - If the site crawl hits MAX_SITE_PAGES with pages still waiting to be visited, the run
    stops (the site may have grown, or a link loop appeared).
  - Removals are only counted when the community's own page returns 404.
  - Removal guard: if more than MAX_REMOVALS_PER_RUN communities were confirmed removed
    since the last snapshot, the run is flagged. Matching then skips "no longer on website"
    proposals until a person confirms the drop is real.

Output:
  data/locations.json                  latest run (read by the matching step)
  data/snapshots/YYYY-MM-DD.json       one snapshot per day, for evidence and diffs
"""
import datetime as dt
import json
import re
import time
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from config import DATA_DIR, SITE_URL

MAX_REMOVALS_PER_RUN = 3
MAX_SITE_PAGES = 50  # crawl limit, so a site bug can't cause an endless crawl
MAX_ATTEMPTS = 3  # tries per page request
RETRY_WAITS = [1, 2]  # seconds to wait before the 2nd and 3rd try
SNAPSHOT_DIR = DATA_DIR / "snapshots"
UNLISTED = "unlisted (page live, no longer linked)"

session = requests.Session()
DETAIL_PATH = re.compile(r"^/communities/[^/]+$")


class ScrapeError(Exception):
    pass


class PageNotFound(ScrapeError):
    """The site answered 404. Fatal unless the caller catches it on purpose."""


def is_api_path(path):
    return path == "/api" or path.startswith(("/api/", "/api?"))


def get_page(path):
    """Request one page, retrying on timeouts, connection errors and 5xx responses.

    Returns the response for anything below 500 (including 404) so the caller decides
    what the status means. Raises ScrapeError if every attempt fails.
    """
    if is_api_path(path):
        raise ScrapeError(f"{path}: refusing to request an /api path")
    url = urljoin(SITE_URL, path)
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            r = session.get(url, timeout=30)
            if r.status_code < 500:
                return r
            problem = f"HTTP {r.status_code}"
        except (requests.Timeout, requests.ConnectionError) as e:
            problem = type(e).__name__
        if attempt < MAX_ATTEMPTS:
            wait = RETRY_WAITS[attempt - 1]
            print(f"  Retrying {path} in {wait}s (attempt {attempt} of {MAX_ATTEMPTS}: {problem})")
            time.sleep(wait)
    raise ScrapeError(f"{path}: failed after {MAX_ATTEMPTS} attempts ({problem})")


def fetch(path):
    """Fetch a page and parse it. Raises PageNotFound on 404, ScrapeError on any other error."""
    r = get_page(path)
    if r.status_code == 404:
        raise PageNotFound(f"{path}: page not found (404)")
    if r.status_code != 200:
        raise ScrapeError(f"{path}: unexpected HTTP {r.status_code}")
    return BeautifulSoup(r.text, "html.parser")


def internal_path(href, base="/"):
    """Turn a link into a site-relative path (keeping ?query), or None if it's external."""
    url = urlparse(urljoin(urljoin(SITE_URL, base), href))
    if url.netloc != urlparse(SITE_URL).netloc:
        return None
    path = url.path.rstrip("/") or "/"
    return f"{path}?{url.query}" if url.query else path


def community_links(soup, base="/"):
    links = []
    for a in soup.select("a[href]"):
        path = internal_path(a["href"], base)
        if path and DETAIL_PATH.match(path.split("?")[0]):
            path = path.split("?")[0]
            if path not in links:
                links.append(path)
    return links


def walk_directory():
    """Follow the directory's Next links. Returns (links, count the site says it lists)."""
    links, page, visited, stated = [], "/communities", set(), None
    while page and page not in visited:
        visited.add(page)
        soup = fetch(page)
        for href in community_links(soup, page):
            if href not in links:
                links.append(href)
        m = re.search(r"(\d+)\s+communities listed", soup.get_text(" "))
        if m:
            stated = int(m.group(1))
        nxt = next((a for a in soup.select(".pager a[href]") if "next" in a.get_text().lower()), None)
        page = internal_path(nxt["href"], page) if nxt else None
    return links, stated


def crawl_site_for_communities():
    """Visit every non-directory page on the site and collect community links."""
    found, queue, visited = [], ["/"], set()
    while queue:
        page = queue.pop(0)
        if page in visited:
            continue
        if len(visited) >= MAX_SITE_PAGES:
            raise ScrapeError(f"Site crawl reached the {MAX_SITE_PAGES}-page limit with pages "
                              "still waiting; raise MAX_SITE_PAGES if the site really grew.")
        visited.add(page)
        try:
            soup = fetch(page)
        except PageNotFound:
            print(f"  Warning: {page} is linked on the site but returned 404; skipped.")
            continue
        for a in soup.select("a[href]"):
            path = internal_path(a["href"], page)
            if not path:
                continue
            bare = path.split("?")[0]
            if DETAIL_PATH.match(bare):
                if bare not in found:
                    found.append(bare)
            elif bare != "/communities" and not is_api_path(bare) \
                    and path not in visited and path not in queue:
                # The directory is walked separately, /api is never touched, and detail
                # pages are not crawled further.
                queue.append(path)
    return found


def parse_detail(path):
    soup = fetch(path)
    h1 = soup.select_one("h1")
    fields = {}
    for dt_tag in soup.select("dl.detail dt"):
        fields[dt_tag.get_text(strip=True).lower()] = dt_tag.find_next_sibling("dd")
    if not h1 or "address" not in fields:
        raise ScrapeError(f"{path}: page layout changed (no name or address found)")

    addr_lines = list(fields["address"].stripped_strings)
    m = re.match(r"^(.*?),\s*([A-Z]{2})\s+(\d{5})(?:-\d{4})?$", addr_lines[-1])
    if not m or len(addr_lines) < 2:
        raise ScrapeError(f"{path}: could not parse address {addr_lines!r}")
    city, state, zip_code = m.groups()

    def text(key):
        return fields[key].get_text(" ", strip=True) if key in fields else ""

    offerings = [b.get_text(strip=True) for b in fields["care offerings"].select(".badge")] \
        if "care offerings" in fields else []

    return {
        "slug": path.rsplit("/", 1)[-1],
        "url": urljoin(SITE_URL, path),
        "name": h1.get_text(strip=True),
        "street": " ".join(addr_lines[:-1]),
        "city": city,
        "state": state,
        "zip": zip_code,
        "care_offerings": offerings,
        "administrator": text("administrator"),
        "phone": text("phone"),
    }


def previous_snapshot(today):
    if not SNAPSHOT_DIR.exists():
        return None
    older = sorted(p for p in SNAPSHOT_DIR.glob("*.json") if p.stem < today)
    return json.loads(older[-1].read_text()) if older else None


def scrape():
    today = dt.date.today().isoformat()
    directory, stated = walk_directory()
    if stated is None:
        raise ScrapeError("Directory no longer shows 'N communities listed'; layout may have changed")
    if len(directory) != stated:
        raise ScrapeError(f"Directory says {stated} communities but {len(directory)} were found; "
                          "the site may have loaded partially. Nothing was saved.")

    elsewhere = [p for p in crawl_site_for_communities() if p not in directory]
    locations = []
    for path in directory + elsewhere:
        loc = parse_detail(path)
        loc["found_on"] = "directory" if path in directory else "elsewhere on site (not in directory)"
        locations.append(loc)

    prev = previous_snapshot(today)
    removed, added = [], []
    if prev:
        prev_slugs = {l["slug"]: l["name"] for l in prev["locations"]}
        found_slugs = {l["slug"] for l in locations}
        # A community that is no longer linked anywhere may still have a live page.
        # Check each one directly; only a 404 counts as removed. Any other error stops the run.
        for slug, name in prev_slugs.items():
            if slug in found_slugs:
                continue
            try:
                loc = parse_detail(f"/communities/{slug}")
            except PageNotFound:
                removed.append({"slug": slug, "name": name, "last_seen": prev["scraped_on"],
                                "page_status": 404})
                continue
            loc["found_on"] = UNLISTED
            locations.append(loc)
        added = [l["name"] for l in locations if l["slug"] not in prev_slugs]

    return {
        "scraped_on": today,
        "scraped_at": dt.datetime.now().isoformat(timespec="seconds"),
        "directory_stated_count": stated,
        "locations": locations,
        "added_since_last_run": added,
        "removed_since_last_run": removed,
        "removal_guard_tripped": len(removed) > MAX_REMOVALS_PER_RUN,
    }


def main():
    result = scrape()
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(result, indent=2)
    (SNAPSHOT_DIR / f"{result['scraped_on']}.json").write_text(payload)
    (DATA_DIR / "locations.json").write_text(payload)

    print(f"{len(result['locations'])} locations scraped "
          f"(directory lists {result['directory_stated_count']}).")
    for loc in result["locations"]:
        if loc["found_on"] == UNLISTED:
            print(f"  Unlisted (page still live, no longer linked): {loc['name']}")
        elif loc["found_on"] != "directory":
            print(f"  Not in directory: {loc['name']}")
    if result["added_since_last_run"]:
        print("  New since last run:", ", ".join(result["added_since_last_run"]))
    if result["removed_since_last_run"]:
        print("  Gone since last run:", ", ".join(r["name"] for r in result["removed_since_last_run"]))
    if result["removal_guard_tripped"]:
        print(f"  WARNING: more than {MAX_REMOVALS_PER_RUN} communities were removed at once. "
              "Removal proposals are paused until a person checks the site.")
    return result


if __name__ == "__main__":
    main()
