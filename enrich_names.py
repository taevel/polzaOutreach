#!/usr/bin/env python3
"""
enrich_names.py — поиск имён ЛПР на официальных сайтах компаний (для колонки «Имя контакта»).

Вход:  CSV с колонкой `website` (например, base_final.csv)
Выход: тот же CSV + contact_name, first_name, contact_title, name_source_url

Что делает:
- открывает страницы «О компании / Руководство / Команда» (до 6 штук на сайт);
- ищет блоки, где рядом стоят должность (коммерческий директор, директор по продажам/маркетингу,
  генеральный директор, основатель, CEO) и ФИО;
- выбирает самого «закупающего» по приоритету: коммерческий/продажи > маркетинг > развитие > генеральный > основатель.
Используются только публичные данные с официального сайта компании. Имя не угадывается: нет блока = поле пустое.
Результат нужно просмотреть глазами (имя могло попасть не то), поэтому всегда указан URL источника.

Запуск:
    pip install requests beautifulsoup4
    python enrich_names.py base_final.csv base_names.csv
"""
import argparse
import csv
import re
import sys
import time
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; polza-outreach/1.0)"}
PATHS = ["/about", "/about/management", "/management", "/company", "/company/management",
         "/team", "/o-kompanii", "/rukovodstvo", "/about-us", "/leadership"]
LINK_HINT = re.compile(r"руковод|менеджмент|управлен|команда|management|leadership|team|о компании|about", re.I)

# (подстрока должности, приоритет: меньше = лучше)
TITLES = [("коммерческий директор", 1), ("директор по продажам", 1), ("директор по маркетингу", 2),
          ("директор по развитию", 3), ("генеральный директор", 4), ("основатель", 5), ("ceo", 5),
          ("директор", 6)]
CYR = r"[А-ЯЁ][а-яё]{2,}(?:-[А-ЯЁ][а-яё]{2,})?"
NAME2 = re.compile(r"\b(%s) (%s)\b" % (CYR, CYR))
NAME3 = re.compile(r"\b(%s) (%s) (%s)\b" % (CYR, CYR, CYR))
PATR = re.compile(r"(ович|евич|ьич|овна|евна|ична)$")
SURN = re.compile(r"(ов|ова|ев|ева|ин|ина|ын|ына|ский|ская|цкий|цкая|ой|ых|юк|ук|енко|ко)$")
STOP = {"Компания", "Россия", "Москва", "Руководство", "Отдел", "Группа", "Санкт", "Петербург", "Российская", "Федерации"}


def fetch(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        r.encoding = r.apparent_encoding
        return r.text, r.url
    except requests.RequestException:
        return None, url


def first_name_of(full):
    """Определяет имя для обращения из ФИО (порядок слов на сайтах разный)."""
    w = full.split()
    if len(w) == 3:
        if PATR.search(w[1]):          # Иван Иванович Иванов
            return w[0]
        if PATR.search(w[2]):          # Иванов Иван Иванович
            return w[1]
    if len(w) >= 2:
        if SURN.search(w[0]) and not SURN.search(w[1]):
            return w[1]                # Иванов Иван
        return w[0]                    # Иван Иванов
    return w[0] if w else ""


def extract_people(html):
    """[(приоритет, ФИО, должность)] из блоков, где рядом стоят должность и ФИО."""
    soup = BeautifulSoup(html or "", "html.parser")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    found = []
    for el in soup.find_all(["div", "li", "p", "td", "article", "section", "span", "h3", "h4"]):
        text = re.sub(r"\s+", " ", el.get_text(" ")).strip()
        if not 10 <= len(text) <= 220:
            continue
        low = text.lower()
        title = next(((t, p) for t, p in TITLES if t in low), None)
        if not title:
            continue
        cleaned = re.sub("|".join(re.escape(t) for t, _ in TITLES), " ", text, flags=re.I)
        m = NAME3.search(cleaned) or NAME2.search(cleaned)
        if not m:
            continue
        name = " ".join(m.groups())
        if any(word in STOP for word in name.split()):
            continue
        found.append((el, title[1], name, title[0]))
    # оставляем только самые вложенные блоки: общий контейнер «Команда» не должен приписывать должность первому имени
    ids = set(id(f[0]) for f in found)
    result = [f[1:] for f in found if not any(id(d) in ids for d in f[0].descendants)]
    return sorted(set(result))


def pages_for(site):
    base = site if site.startswith("http") else "https://" + site
    html, final = fetch(base)
    if not html:
        return []
    pages, seen = [(final, html)], {final}
    cands = [urljoin(final, p) for p in PATHS]
    for a in BeautifulSoup(html, "html.parser").find_all("a", href=True):
        if LINK_HINT.search(a.get_text(" ")) or LINK_HINT.search(a["href"]):
            cands.append(urljoin(final, a["href"]).split("#")[0])
    for url in cands:
        if len(pages) >= 6:
            break
        if url in seen or urlparse(url).netloc.replace("www.", "") != urlparse(final).netloc.replace("www.", ""):
            continue
        seen.add(url)
        h, u = fetch(url)
        if h:
            pages.append((u, h))
    return pages


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input_csv")
    ap.add_argument("output_csv")
    args = ap.parse_args()
    with open(args.input_csv, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    cols = list(rows[0].keys())
    for c in ("first_name", "contact_title", "name_source_url"):
        if c not in cols:
            cols.append(c)
    with open(args.output_csv, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for i, row in enumerate(rows, 1):
            best = None
            for url, html in pages_for(row["website"]):
                people = extract_people(html)
                if people and (best is None or people[0][0] < best[0][0]):
                    best = (people[0], url)
            if best:
                (prio, name, title), url = best
                row.update(contact_name=name, first_name=first_name_of(name), contact_title=title, name_source_url=url)
                print("[%d/%d] %s: %s (%s)" % (i, len(rows), row["website"], name, title), file=sys.stderr)
            else:
                for c in ("first_name", "contact_title", "name_source_url"):
                    row.setdefault(c, "")
                print("[%d/%d] %s: имя не найдено" % (i, len(rows), row["website"]), file=sys.stderr)
            w.writerow(row)
            time.sleep(1)


if __name__ == "__main__":
    main()
