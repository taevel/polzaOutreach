#!/usr/bin/env python3
"""
collect_base.py — сбор базы B2B-контактов с официальных сайтов (Polza Agency, задача 1).

Вход:  seeds.csv с колонкой `website` (домены российских B2B-компаний)
Выход: CSV в формате, который принимает personalize.py:
       company, website, contact_name, email, email_type, mx_ok, source_url, all_found

Принципы:
- Emails берутся ТОЛЬКО со страниц самого сайта (mailto + текст), ничего не угадывается.
- Принимается только адрес на домене компании (отсекаем чужие/рекламные).
- Для адреса проверяется наличие MX-записи домена. Это не гарантия, что ящик жив,
  финальную проверку делайте верификатором (Hunter, ZeroBounce и т.п.).
- Имя ЛПР ищется эвристикой («Генеральный директор Иванов Иван»); если не найдено,
  поле пустое. Не выдумываем.

Запуск:
    pip install requests beautifulsoup4 dnspython
    python collect_base.py seeds.csv base.csv
"""
import argparse
import csv
import re
import sys
import time
from urllib.parse import urljoin, urlparse

import dns.resolver
import requests
from bs4 import BeautifulSoup

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; polza-outreach/1.0)"}
CONTACT_PATHS = ["/contacts", "/contact", "/kontakty", "/about", "/company", "/o-kompanii"]
LINK_HINT = re.compile(r"контакт|contact|о компании|about|команда|team", re.I)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
BAD_SUFFIX = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".css", ".js")
BAD_PARTS = ("sentry", "example", "yourdomain", "noreply", "no-reply", "support-bot")
SALES_EXACT = {"sales", "sale", "zakaz", "order", "orders", "partner", "partners", "manager", "b2b", "opt"}
SALES_PREFIX = ("sale", "zakaz", "order", "partner", "tender", "manager")
GENERAL = {"info", "hello", "office", "mail", "contact", "contacts", "welcome", "team", "hi"}
# служебные ящики: для продаж не подходят (HR, PR, поддержка, бухгалтерия, абузы и т.п.)
SERVICE_EXACT = {"hr", "job", "jobs", "career", "careers", "rabota", "pr", "marketing", "marcom",
                 "abuse", "support", "care", "help", "compliance", "pretension", "claim", "claims",
                 "buh", "accounting", "legal", "privacy", "security", "noreply", "no-reply",
                 "writer", "affiliate", "media", "adv", "ads", "feedback", "reklama", "admin",
                 "webmaster", "postmaster", "founders", "present"}
SERVICE_PREFIX = ("anticor", "antikor", "press", "career", "support", "marketing", "compliance", "pretension", "abuse")
KIND_RANK = {"sales": 0, "general": 1, "other": 2}
NAME_RE = re.compile(
    r"(?:Генеральный директор|Директор|Основатель|CEO)[^А-ЯЁA-Z]{0,25}"
    r"([А-ЯЁ][а-яё]+ [А-ЯЁ][а-яё]+(?: [А-ЯЁ][а-яё]+)?)")


def root_domain(host: str) -> str:
    host = host.lower()
    if host.startswith("www."):  # совместимо с Python 3.7 (без str.removeprefix)
        host = host[4:]
    return host.strip("/")


def fetch(url: str):
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        r.encoding = r.apparent_encoding
        return r.text, r.url
    except requests.RequestException:
        return None, url


def pages_to_scan(site: str):
    """Главная + типовые страницы контактов + ссылки из меню (до 6 страниц)."""
    base = site if site.startswith("http") else f"https://{site}"
    html, final = fetch(base)
    if not html:
        return []
    pages, seen = [(final, html)], {final}
    candidates = [urljoin(final, p) for p in CONTACT_PATHS]
    for a in BeautifulSoup(html, "html.parser").find_all("a", href=True):
        if LINK_HINT.search(a.get_text(" ")) or LINK_HINT.search(a["href"]):
            candidates.append(urljoin(final, a["href"]).split("#")[0])
    for url in candidates:
        if len(pages) >= 6:
            break
        if url in seen or root_domain(urlparse(url).netloc) != root_domain(urlparse(final).netloc):
            continue
        seen.add(url)
        sub_html, sub_url = fetch(url)
        if sub_html:
            pages.append((sub_url, sub_html))
    return pages


def company_name(soup, domain: str) -> str:
    """og:site_name, иначе сегмент <title> с именем домена, иначе имя домена (title часто мусорный)."""
    label = domain.split(".")[0]
    og = soup.find("meta", property="og:site_name")
    if og and og.get("content") and 2 <= len(og["content"].strip()) <= 30:
        return og["content"].strip()
    title = soup.title.get_text(" ") if soup.title else ""
    for part in re.split(r"[|—–:•«»-]", title):
        part = part.strip()
        if 2 <= len(part) <= 30 and re.sub(r"[^a-z0-9]", "", label.lower()) in re.sub(r"[^a-z0-9]", "", part.lower()):
            return part
    return label.capitalize()


def extract(pages, domain: str):
    """Возвращает (emails{email: url}, company_name, contact_name)."""
    emails, company, person = {}, "", ""
    for url, html in pages:
        soup = BeautifulSoup(html, "html.parser")
        if not company:
            company = company_name(soup, domain)
        text = soup.get_text(" ")
        found = {m for a in soup.find_all("a", href=True) if a["href"].lower().startswith("mailto:")
                 for m in EMAIL_RE.findall(a["href"][7:].split("?")[0])}
        found |= set(EMAIL_RE.findall(text))
        for e in found:
            e = e.strip().lower()
            if e.endswith(BAD_SUFFIX) or any(b in e for b in BAD_PARTS):
                continue
            # только домен компании; допускаем другую зону (сайт .ru, почта на .com)
            if domain.split(".")[0] in root_domain(e.split("@")[1]).split("."):
                emails.setdefault(e, url)
        if not person:
            m = NAME_RE.search(re.sub(r"\s+", " ", text))
            if m:
                person = m.group(1)
    return emails, company, person


def classify(email: str, label: str) -> str:
    """sales / general / service / other. `label` = имя домена компании (например, uncomtech)."""
    local = email.split("@")[0].lower()
    if local in SERVICE_EXACT or local.startswith(SERVICE_PREFIX):
        return "service"
    if local in SALES_EXACT or local.startswith(SALES_PREFIX):
        return "sales"
    if local in GENERAL or local == label:
        return "general"
    return "other"  # возможно личный адрес сотрудника, проверить вручную


def pick_best(emails: dict, label: str):
    """Для аутрича: отдел продаж > общий адрес > прочий. Служебные (hr, pr, support...) отбрасываются."""
    usable = []
    for e in emails:
        k = classify(e, label)
        if k != "service":
            usable.append((KIND_RANK[k], e, k))
    if not usable:
        return None, None
    _, best, kind = min(usable)
    return best, kind


def has_mx(domain: str) -> bool:
    try:
        return bool(dns.resolver.resolve(domain, "MX", lifetime=5))
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("seeds_csv")
    ap.add_argument("output_csv")
    args = ap.parse_args()
    with open(args.seeds_csv, encoding="utf-8-sig") as f:
        sites = [r["website"].strip() for r in csv.DictReader(f) if r.get("website")]
    cols = ["company", "website", "contact_name", "email", "email_type", "mx_ok", "source_url", "all_found"]
    kept = 0
    with open(args.output_csv, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for i, site in enumerate(sites, 1):
            domain = root_domain(urlparse(site if "//" in site else f"//{site}").netloc)
            emails, company, person = extract(pages_to_scan(site), domain)
            if not emails:
                print(f"[{i}/{len(sites)}] {domain}: email не найден, пропуск", file=sys.stderr)
                continue
            email, kind = pick_best(emails, domain.split(".")[0])
            if not email:
                print(f"[{i}/{len(sites)}] {domain}: только служебные адреса ({', '.join(emails)}), пропуск",
                      file=sys.stderr)
                continue
            w.writerow({"company": company or domain, "website": domain, "contact_name": person,
                        "email": email, "email_type": kind,
                        "mx_ok": has_mx(email.split("@")[1]), "source_url": emails[email],
                        "all_found": ", ".join(sorted(emails))[:300]})
            kept += 1
            print(f"[{i}/{len(sites)}] {domain}: {email} ({kind})", file=sys.stderr)
            time.sleep(1)  # вежливая пауза
    print(f"Готово: {kept} из {len(sites)} компаний с найденным email", file=sys.stderr)


if __name__ == "__main__":
    main()
