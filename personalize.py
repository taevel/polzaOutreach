#!/usr/bin/env python3
"""
personalize.py — персонализация B2B-базы для email-аутрича (Polza Agency, задачи 2 и 4).

Вход:  CSV с колонками company, website, contact_name, email
Выход: тот же CSV + personalization, evidence, source_url, flags, status

Режимы: с ключом ANTHROPIC_API_KEY факт пишет LLM; без ключа факт берётся из HTML (свежая новость
или описание сайта), статус ok_extract / ok_extract_description.

Принципы:
1. Факт берётся ТОЛЬКО из текста сайта компании (главная + «О компании»/новости).
2. LLM обязан вернуть дословную цитату-доказательство. Скрипт проверяет, что цитата
   реально есть в загруженном тексте. Нет в тексте = факт выброшен (защита от выдумок).
3. Перед вызовом LLM строка проверяется на «перепутанные» данные: домен почты не
   совпадает с сайтом, название компании не встречается на сайте, не РФ-домен.
   Такие строки не персонализируются, а получают флаг для ручной проверки.

Запуск:
    pip install requests beautifulsoup4
    export ANTHROPIC_API_KEY=...
    python personalize.py companies.csv result.csv
"""
import argparse
import csv
import json
import os
import re
import sys
import time
import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

MODEL = os.getenv("PERSONALIZE_MODEL", "claude-sonnet-4-6")
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; polza-outreach/1.0)"}
RU_TLDS = (".ru", ".su", ".рф", ".xn--p1ai")
FREE_MAIL = {"gmail.com", "mail.ru", "yandex.ru", "126.com", "163.com", "qq.com", "outlook.com"}
GENERIC_LOCALS = {"sales", "info", "office", "mail", "zakaz", "contact", "hello"}
LINK_HINT = re.compile(r"о компании|о нас|about|новости|news|проекты|кейс", re.I)
CHARS_PER_PAGE = 2500
BLOCKING_FLAGS = {"email_domain_mismatch", "name_not_on_site", "not_ru_domain"}


def norm(s: str) -> str:
    """Нормализация для сравнения: нижний регистр, только буквы/цифры."""
    return re.sub(r"\W+", " ", s.lower()).strip()


def domain_of(url_or_email: str) -> str:
    s = url_or_email.split("@")[-1]
    if "//" in s:
        s = urlparse(s).netloc
    s = s.lower()
    if s.startswith("www."):  # совместимо с Python 3.7
        s = s[4:]
    return s.strip("/")


def fetch(url: str):
    """Скачивает страницу, возвращает (html, final_url) или (None, url)."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        r.encoding = r.apparent_encoding
        return r.text, r.url
    except requests.RequestException:
        return None, url


def extract_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    meta = soup.find("meta", attrs={"name": "description"})
    parts = [soup.title.get_text(" ") if soup.title else "",
             meta["content"] if meta and meta.get("content") else "",
             soup.get_text(" ")]
    return re.sub(r"\s+", " ", " ".join(parts)).strip()[:CHARS_PER_PAGE]


def collect_site_text(site: str):
    """Главная + до 3 внутренних страниц («О компании», «Новости»). Возвращает [(url, text)]."""
    base = site if site.startswith("http") else f"https://{site}"
    html, final = fetch(base)
    if not html:
        return []
    RAW[final] = html
    pages = [(final, extract_text(html))]
    seen = {final}
    for a in BeautifulSoup(html, "html.parser").find_all("a", href=True):
        if len(pages) >= 4:
            break
        if LINK_HINT.search(a.get_text(" ")) or LINK_HINT.search(a["href"]):
            link = urljoin(final, a["href"]).split("#")[0]
            if link not in seen and domain_of(link) == domain_of(final):
                seen.add(link)
                sub_html, sub_url = fetch(link)
                if sub_html:
                    RAW[sub_url] = sub_html
                    pages.append((sub_url, extract_text(sub_html)))
    return pages


# ---------- Режим без LLM: факт берётся прямо из HTML (свежая новость или описание сайта) ----------
MONTHS = {"января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6, "июля": 7,
          "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12}
DATE_RE = re.compile(
    r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b|\b(\d{1,2})\s+(" + "|".join(MONTHS) + r")\s+(\d{4})\b", re.I)
RAW = {}  # url -> исходный HTML (заполняется в collect_site_text)


def parse_date(m):
    try:
        if m.group(1):
            return datetime.date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        return datetime.date(int(m.group(6)), MONTHS[m.group(5).lower()], int(m.group(4)))
    except ValueError:
        return None


def news_items(html: str):
    """Пары (дата, заголовок): для каждой даты берём ближайший контейнер, где есть текст и только одна дата."""
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    items = []
    for node in soup.find_all(string=DATE_RE):
        d = parse_date(DATE_RE.search(node))
        cont = node.parent
        for _ in range(4):
            if d is None or cont is None:
                break
            txt = re.sub(r"\s+", " ", cont.get_text(" ")).strip()
            if len(DATE_RE.findall(txt)) != 1:  # контейнер захватил несколько новостей
                break
            head = DATE_RE.sub("", txt).strip(" -–—|•:.,")
            if len(head) >= 20:
                if len(head) <= 150:
                    items.append((d, head))
                break
            cont = cont.parent
    return items


def extractive(out: dict, pages: list) -> dict:
    today = datetime.date.today()
    items = [(d, h, u) for u, _ in pages for d, h in news_items(RAW.get(u, ""))]
    fresh = [i for i in items if 0 <= (today - i[0]).days <= 365]
    if fresh:
        d, head, url = max(fresh)
        out.update(personalization=f"Заметил на вашем сайте новость от {d:%d.%m.%Y}: «{head}».",
                   evidence=head, source_url=url, status="ok_extract")
        return out
    soup = BeautifulSoup(RAW.get(pages[0][0], ""), "html.parser")
    meta = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", property="og:description")
    desc = re.sub(r"\s+", " ", meta["content"]).strip() if meta and meta.get("content") else ""
    sent = re.split(r"(?<=[.!?])\s", desc)[0][:160] if desc else ""
    if len(sent) >= 30:
        out.update(personalization=f"Прочитал на вашем сайте: «{sent.rstrip('.')}».", evidence=sent,
                   source_url=pages[0][0], status="ok_extract_description")
    else:
        out["status"] = "no_fact"
    return out


def check_flags(row: dict, site_text: str) -> list:
    """Ищет признаки того, что данные в строке перепутаны."""
    flags = []
    site_dom, mail_dom = domain_of(row["website"]), domain_of(row["email"])
    if mail_dom in FREE_MAIL:
        flags.append("free_mail")
    elif mail_dom and site_dom and mail_dom.split(".")[0] not in site_dom:
        flags.append("email_domain_mismatch")
    letters = re.findall(r"[A-Za-zА-Яа-яЁё]", site_text or "")
    cyr_share = sum(ch > "\u0400" for ch in letters) / len(letters) if letters else 0
    if not site_dom.endswith(RU_TLDS) and cyr_share < 0.2:  # не .ru и сайт не на русском
        flags.append("not_ru_domain")
    if row["email"].split("@")[0].lower() in GENERIC_LOCALS:
        flags.append("generic_email")
    tokens = [t for t in norm(row["company"]).split() if len(t) >= 4]
    if site_text and tokens and not any(t in norm(site_text) or t in site_dom for t in tokens):
        flags.append("name_not_on_site")
    return flags


PROMPT = """Ты помогаешь писать холодные письма. Ниже текст с сайта компании «{company}».
Найди ОДИН конкретный проверяемый факт (продукт, запуск, новость, выставка, масштаб, география),
который можно упомянуть в письме. Не выдумывай и не додумывай.

Верни только JSON: {{"fact": "1-2 предложения по-русски, без лести", "evidence": "дословная цитата из текста, подтверждающая факт", "source_url": "URL страницы"}}
Если подходящего факта нет, верни {{"fact": null}}.

{pages}"""


API_URL = "https://api.anthropic.com/v1/messages"


def ask_llm(api_key: str, company: str, pages: list):
    """Прямой вызов Messages API через requests (без SDK)."""
    blob = "\n\n".join(f"[{u}]\n{t}" for u, t in pages)
    r = requests.post(
        API_URL, timeout=60,
        headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json={"model": MODEL, "max_tokens": 400,
              "messages": [{"role": "user", "content": PROMPT.format(company=company, pages=blob)}]})
    r.raise_for_status()
    m = re.search(r"\{.*\}", r.json()["content"][0]["text"], re.S)
    return json.loads(m.group(0)) if m else {"fact": None}


def process(api_key: str, row: dict) -> dict:
    out = {"personalization": "", "evidence": "", "source_url": "", "flags": "", "status": ""}
    pages = collect_site_text(row["website"])
    if not pages:
        out["status"] = "fetch_failed"
        return out
    all_text = " ".join(t for _, t in pages)
    flags = check_flags(row, all_text)
    out["flags"] = ",".join(flags)
    if BLOCKING_FLAGS & set(flags):
        out["status"] = "skipped_needs_review"  # данные перепутаны, персонализацию не придумываем
        return out
    if not api_key:
        return extractive(out, pages)
    try:
        res = ask_llm(api_key, row["company"], pages)
    except Exception as e:  # сеть/JSON: не роняем весь прогон
        out["status"] = f"llm_error: {e}"[:80]
        return out
    if not res.get("fact"):
        out["status"] = "no_fact"
    elif norm(res.get("evidence", "")) not in norm(all_text):
        out["status"] = "rejected_unverified_evidence"  # цитаты нет на сайте = выдумка
    else:
        out.update(personalization=res["fact"], evidence=res["evidence"],
                   source_url=res.get("source_url", ""), status="ok")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input_csv")
    ap.add_argument("output_csv")
    args = ap.parse_args()
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ANTHROPIC_API_KEY не задан: режим без LLM (факт берётся прямо из HTML сайта)", file=sys.stderr)
    with open(args.input_csv, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    cols = list(rows[0].keys()) + ["personalization", "evidence", "source_url", "flags", "status"]
    with open(args.output_csv, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for i, row in enumerate(rows, 1):
            row.update(process(api_key, row))
            w.writerow(row)
            print(f"[{i}/{len(rows)}] {row['company']}: {row['status']} {row['flags']}", file=sys.stderr)
            time.sleep(1)  # вежливая пауза между сайтами


if __name__ == "__main__":
    main()
