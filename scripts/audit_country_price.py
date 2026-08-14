from __future__ import annotations

import json
import logging
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

PER_URL_TIMEOUT_SECONDS = 120
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


URLS = """
https://www.ancien.co.uk/stock-vlwwO/p/crackle-ceramic-lamp
https://object88.com/product/abstract-organic-bronze-sculpture-by-cesar-bailleux-belgium-1980s/
https://www.rijpvintage.com/product-page/vintage-cinna-sandra-3-seater-sofa-in-beige-velvet-by-ligne-roset
https://mdrn.at/swedish-cut-glass-suspension-chandelier-circa-1950/
https://oblist.com/products/oju-chair
https://formesutiles.com/en/assises/tabourets-bancs/tabouret-en-chene-massif-travail-francais-annees-1950
https://www.pauletteintstad.com/shop-1TWbF/p/rare-modular-wall-unit-aggregabili-by-anonima-design-for-bonetto-italy-1969
https://studioalium.nl/product/pair-of-ceramic-palme-wall-lamps-by-georges-jouve-1960s/
https://www.galleria62.com/collection/sculptural-palm-trunk-chair/
https://erthouse.com/content/feature/69/artworks-254-multifunctional-sideboard-in-mahogany-after-eileen-gray-germany-1960s/
https://www.atkris.com/items/table-lamp-osso-by-mazzega-italy-1970
https://www.objekt-vintage.nl/portfolio/cabinet-with-drawers-by-afra-and-tobia-scarpa-for-maxalto-italy-1970s/
https://www.envanrijn.com/collection/brutalist-spanish-oak-sideboard
https://www.daddydeco.com/collection/italian-armchairs-zigzag-upholstery
https://modern-living.be/product/poul-kjaerholm-pk61-coffee-table-2/
https://www.reapproved-by-vaa.com/shop/p/set-of-two-le-bambole-lounge-chairs-by-mario-bellini
https://malataantwerp.com/product/hand-carved-wooden-pedestal/
https://www.spazioleone.com/products/toscana-chair-set
https://betonbrut.co.uk/pair-of-art-deco-armchairs-2/
https://shop.magazzino76.it/collections/frontpage/products/wall-perpetual-green-calendar-by-giorgio-della-beffa-for-ring-a-date-2000-2010s
https://ruevintage74.com/collections/whats-new/products/silla-tulip-pierre-guariche-para-steiner
https://auctionet.com/sv/5243361-hollandsk-byra-i-ek-med-fyra-lador
https://www.danke-galerie.com/produit/paire-dappliques-spots-suedoises-asea-rouges-annees-60-g107/
https://galleryk7.com/en/items/1640
https://www.childan.com/en/catalog/set-of-2-koala-armchairs-by-garouste-bonetti-ed-bgh-cN67dP5vJ1LBEnNdxKH3/
https://www.twopoems.co.uk/new-arrivals/p/d0cfa7tkq59es314wgbfaqvwcy4h8v
https://moltocollectibles.it/en/collectibles/credenza-in-legno-effetto-bambu-anni-80/
https://www.eliaselias.dk/handpicked/p/pair-of-danish-lounge-chairs-oak-lambswool-modern
https://www.studio125.co.uk/collections/antiques/objects/xl-gilt-iron-catalan-12-arm-chandelier-3a392b58ff
https://www.sauceldn.com/tables#/audouxminnet-tiletop-dining-table/
https://www.thepeanutvendor.co.uk/collections/seating/products/arched-bookcase
https://www.desuet.fr/produit/miroir-ovale/
https://www.roamantics.design/tables/table-de-repas-en-chene-travail-artisanal-vers-1980
https://massmoderndesign.com/gallery-detail/mario-marenco-sapporo-chairs-set-mobil-girgi-italy-1975/
https://www.wauw.be/nl/moving-table-1970s-163324605.html?tl=Moving%20table,%201970%27s
https://www.demosmobilia.ch/product/pair-of-large-wall-sconces/
https://galerieparadis.fr/products/banc-en-acier-inoxydable-moderniste-galerie-paradis
https://goldwoodbyboris.com/slatted-daybed-by-robert-anxionnat-france-circa-1960.html
"""


def check(index: int, url: str) -> dict[str, str | bool]:
    from app.main import run_scrape_url
    from app.schemas import ScrapeRequest
    from app.services.xianyu_pipeline import calculate_xianyu_price, extract_product_country

    payload = ScrapeRequest(url=url, render="auto", max_images=12, min_score=25)
    try:
        result, job_id, _ = run_scrape_url(
            url,
            payload,
            upload_images=False,
            request_id_value=f"audit-price-country-{index}",
        )
        country = extract_product_country(result)
        source_price = str(result.get("price") or "").strip()
        return {
            "index": str(index),
            "url": url,
            "ok": True,
            "name": str(result.get("name") or "").strip(),
            "country": country,
            "has_country": bool(country),
            "source_price": source_price,
            "has_price": bool(source_price),
            "currency": str(result.get("currency") or "").strip(),
            "xianyu_price": calculate_xianyu_price(source_price),
            "job_id": job_id,
        }
    except Exception as exc:
        return {
            "index": str(index),
            "url": url,
            "ok": False,
            "country": "",
            "has_country": False,
            "source_price": "",
            "has_price": False,
            "currency": "",
            "xianyu_price": "99999",
            "error": str(exc),
        }


def check_with_timeout(index: int, url: str) -> dict[str, str | bool]:
    try:
        completed = subprocess.run(
            [sys.executable, __file__, "--one", str(index), url],
            check=False,
            capture_output=True,
            text=True,
            timeout=PER_URL_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return {
            "index": str(index),
            "url": url,
            "ok": False,
            "country": "",
            "has_country": False,
            "source_price": "",
            "has_price": False,
            "currency": "",
            "xianyu_price": "99999",
            "error": f"timeout after {PER_URL_TIMEOUT_SECONDS}s",
        }

    for line in reversed(completed.stdout.splitlines()):
        if line.startswith("ONE_JSON "):
            return json.loads(line[len("ONE_JSON ") :])
    return {
        "index": str(index),
        "url": url,
        "ok": False,
        "country": "",
        "has_country": False,
        "source_price": "",
        "has_price": False,
        "currency": "",
        "xianyu_price": "99999",
        "error": (completed.stderr or completed.stdout or f"exit code {completed.returncode}")[-1000:],
    }


def main() -> None:
    logging.getLogger().setLevel(logging.ERROR)
    logging.getLogger("uvicorn.error").setLevel(logging.ERROR)
    if len(sys.argv) >= 4 and sys.argv[1] == "--one":
        item = check(int(sys.argv[2]), sys.argv[3])
        print("ONE_JSON " + json.dumps(item, ensure_ascii=False))
        return

    urls = list(dict.fromkeys(line.strip() for line in URLS.splitlines() if line.strip()))
    results: list[dict[str, str | bool] | None] = [None] * len(urls)
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(check_with_timeout, index, url) for index, url in enumerate(urls, 1)]
        for future in as_completed(futures):
            item = future.result()
            results[int(str(item["index"])) - 1] = item
            status = "OK" if item["ok"] else "ERR"
            country = item["country"] or "-"
            price = item["source_price"] or "-"
            print(f"{item['index']}/{len(urls)} {status} country={country} price={price} url={item['url']}", flush=True)
    print("RESULTS_JSON_START")
    print(json.dumps(results, ensure_ascii=False, indent=2))
    print("RESULTS_JSON_END")


if __name__ == "__main__":
    main()
