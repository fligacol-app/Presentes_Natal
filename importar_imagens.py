import argparse
import ipaddress
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse, unquote
import requests
from bs4 import BeautifulSoup

HOSTS = {"www.farmaciadaliga.pt", "farmaciadaliga.pt"}

def page_ok(url):
    u = urlparse(url)
    return u.scheme == "https" and u.hostname in HOSTS and not u.username and not u.password

def image_ok(url):
    u = urlparse(url)
    if u.scheme != "https" or not u.hostname or u.username or u.password:
        return False
    if u.hostname == "localhost" or u.hostname.endswith(".local"):
        return False
    try:
        if not ipaddress.ip_address(u.hostname).is_global:
            return False
    except ValueError:
        pass
    return not re.search(r"logo|placeholder|no[-_]?image|sem[-_]?imagem", u.path, re.I)

def get_page(session, url):
    for _ in range(5):
        if not page_ok(url):
            raise ValueError("Domínio da página não autorizado")
        r = session.get(url, timeout=(10, 20), allow_redirects=False)
        if r.status_code in (301, 302, 303, 307, 308):
            url = urljoin(url, r.headers.get("Location", ""))
            continue
        r.raise_for_status()
        if "html" not in r.headers.get("Content-Type", "").lower():
            raise ValueError("A página não devolveu HTML")
        return url, r.text
    raise ValueError("Demasiados redirecionamentos")

def product_nodes(node):
    if isinstance(node, list):
        for item in node:
            yield from product_nodes(item)
    elif isinstance(node, dict):
        types = node.get("@type", [])
        if isinstance(types, str):
            types = [types]
        if any(str(t).split("/")[-1] == "Product" for t in types):
            yield node
        for key in ("@graph", "mainEntity"):
            if key in node:
                yield from product_nodes(node[key])

def images(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from images(item)
    elif isinstance(value, dict):
        for key in ("contentUrl", "url", "@id"):
            if isinstance(value.get(key), str):
                yield value[key]
                break

def candidates(html, page_url, filename=None):
    soup = BeautifulSoup(html, "html.parser")
    result = []
    def add(value, source):
        url = urljoin(page_url, str(value).strip())
        if image_ok(url) and url not in [x[0] for x in result]:
            result.append((url, source))
    if filename:
        expected = Path(filename).name.lower()
        for img in soup.find_all("img"):
            for attr in ("data-src", "data-original", "src"):
                value = img.get(attr)
                if value and Path(unquote(urlparse(value).path)).name.lower() == expected:
                    add(value, "Nome IMAGENS confirmado no HTML")
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(s.string or s.get_text())
        except (ValueError, TypeError):
            continue
        for p in product_nodes(data):
            for value in images(p.get("image", [])):
                add(value, "JSON-LD Product.image")
    for prop in ("og:image:secure_url", "og:image"):
        for tag in soup.find_all("meta", attrs={"property": prop}):
            if tag.get("content"):
                add(tag["content"], prop)
    return result

def verify_image(session, url):
    for _ in range(5):
        if not image_ok(url):
            return False
        r = session.get(url, stream=True, timeout=(10, 20), allow_redirects=False)
        try:
            if r.status_code in (301, 302, 303, 307, 308):
                url = urljoin(url, r.headers.get("Location", ""))
                continue
            return r.status_code == 200 and r.headers.get("Content-Type", "").lower().startswith("image/")
        finally:
            r.close()
    return False

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--catalog", default="catalogo.json")
    ap.add_argument("--report", default="relatorio-imagens.json")
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()
    path = Path(args.catalog)
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, list):
        raise ValueError("O catálogo deve ser uma lista")
    session = requests.Session()
    session.headers["User-Agent"] = "FarmaciaDaLiga-CatalogImages/1.0 (product catalog maintenance)"
    report = {"updated": [], "kept": [], "pending": []}
    for p in data:
        ident = str(p.get("id", ""))
        if p.get("imageUrl") and image_ok(p["imageUrl"]) and not args.refresh:
            report["kept"].append(ident)
            continue
        url = p.get("url", "")
        if not page_ok(url):
            report["pending"].append({"id": ident, "reason": "Página fora do domínio autorizado"})
            continue
        try:
            final, body = get_page(session, url)
            choices = candidates(body, final, p.get("imageFile"))
            found = None
            for image, source in choices[:4]:
                if verify_image(session, image):
                    found = (image, source)
                    break
            if found:
                p["imageUrl"], p["imageSource"] = found
                p["imageCheckedAt"] = datetime.now(timezone.utc).isoformat()
                report["updated"].append({"id": ident, "url": found[0], "source": found[1]})
            else:
                report["pending"].append({"id": ident, "reason": "Não foi identificada uma imagem válida; manter alternativa neutra"})
        except (requests.RequestException, ValueError) as exc:
            report["pending"].append({"id": ident, "reason": str(exc)[:300]})
        time.sleep(0.5)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: len(v) for k, v in report.items()}, ensure_ascii=False))

if __name__ == "__main__":
    main()
