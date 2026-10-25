#!/usr/bin/env python3
"""USD/RUB новостной бот для Telegram-канала. Запускается по расписанию (GitHub Actions)."""
import calendar, hashlib, html, json, os, re, sys, time
from datetime import datetime, timedelta, timezone
import feedparser, requests

TOKEN = os.environ.get("TELEGRAM_TOKEN") or os.environ.get("USDRUB_BOT_TOKEN")
CHANNEL = os.environ.get("CHANNEL_ID", "@dollar_rub_news")
STATE_FILE = os.environ.get("STATE_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json"))
FORCE_SUMMARY = os.environ.get("FORCE_SUMMARY") == "1"
MSK = timezone(timedelta(hours=3))
UA = {"User-Agent": "Mozilla/5.0 (usdrub-news-bot)"}
MAX_NEWS = 5
SUMMARY_HOUR = 9
ALERT_RUB, ALERT_PCT = 1.0, 1.0

FEEDS = {
    "РБК": "https://rssexport.rbc.ru/rbcnews/news/30/full.rss",
    "Интерфакс": "https://www.interfax.ru/rss.asp",
    "Финам": "https://www.finam.ru/analysis/conews/rsspoint/",
    "Банки.ру": "https://www.banki.ru/xml/news.rss",
    "ПРАЙМ": "https://1prime.ru/export/rss2/index.xml",
    "Ведомости": "https://www.vedomosti.ru/rss/news",
}
KEYWORDS = re.compile(r"доллар|рубл|курс\w* (валют|доллар|рубл|юан|евро)|\bЦБ\b|Центробанк|Банк России|ключев\w* ставк|цен\w* на нефть|нефть (подешев|подорож|дорож|дешев)|brent|санкц|валют|юан", re.I)


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(st):
    st["posted"] = st.get("posted", [])[-500:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)


def tg_send(text):
    r = requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", timeout=20, data={
        "chat_id": CHANNEL, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true"})
    ok = r.ok and r.json().get("ok")
    if not ok:
        print("Telegram error:", r.status_code, r.text[:300], file=sys.stderr)
    else:
        print("posted message_id", r.json()["result"]["message_id"])
    time.sleep(1.5)
    return bool(ok)


def get_cbr():
    try:
        d = requests.get("https://www.cbr-xml-daily.ru/daily_json.js", timeout=20, headers=UA).json()
        v = d["Valute"]
        return {"date": d["Date"][:10], "usd": v["USD"]["Value"], "usd_prev": v["USD"]["Previous"],
                "eur": v["EUR"]["Value"], "eur_prev": v["EUR"]["Previous"],
                "cny": v["CNY"]["Value"] / v["CNY"]["Nominal"]}
    except Exception as e:
        print("CBR error:", e, file=sys.stderr)
        return None


def get_moex(secid):
    """Биржевой курс Мосбиржи (бесплатно, с задержкой ~15 мин)."""
    try:
        url = (f"https://iss.moex.com/iss/engines/currency/markets/selt/securities/{secid}.json"
               "?iss.meta=off&iss.only=marketdata&marketdata.columns=LAST")
        rows = requests.get(url, timeout=20, headers=UA).json()["marketdata"]["data"]
        vals = [r[0] for r in rows if r[0]]
        return vals[0] if vals else None
    except Exception as e:
        print("MOEX error:", e, file=sys.stderr)
        return None


def get_brent():
    try:
        m = requests.get("https://query1.finance.yahoo.com/v8/finance/chart/BZ=F?range=5d&interval=1d",
                         timeout=20, headers=UA).json()["chart"]["result"][0]["meta"]
        return m["regularMarketPrice"], m.get("chartPreviousClose")
    except Exception as e:
        print("Brent error:", e, file=sys.stderr)
        return None, None


def fmt(x, d=2):
    return f"{x:,.{d}f}".replace(",", " ").replace(".", ",")


def arrow(diff):
    return "🔺" if diff > 0 else ("🔻" if diff < 0 else "▪️")


def signed(x, d=2):
    return ("+" if x > 0 else "") + fmt(x, d)


def check_alert(st, usd_now, source):
    last = st.get("last_rate")
    if last is None:
        st["last_rate"] = usd_now
        return
    diff = usd_now - last
    pct = diff / last * 100
    if abs(diff) >= ALERT_RUB or abs(pct) >= ALERT_PCT:
        word = "вырос" if diff > 0 else "упал"
        txt = (f"⚡️ <b>Резкое движение доллара!</b>\n\n{arrow(diff)} Доллар {word} до <b>{fmt(usd_now)} ₽</b> "
               f"({signed(diff)} ₽, {signed(pct, 1)}%) с прошлого уведомления ({fmt(last)} ₽).\n"
               f"Источник: {source}\n\n#алерт #USDRUB")
        if tg_send(txt):
            st["last_rate"] = usd_now


def morning_summary(cbr, usd_mkt, cny_mkt):
    du = cbr["usd"] - cbr["usd_prev"]
    de = cbr["eur"] - cbr["eur_prev"]
    d = datetime.fromisoformat(cbr["date"]).strftime("%d.%m.%Y")
    lines = [f"☀️ <b>Доброе утро! Курсы на {d}</b>\n",
             f"💵 Доллар ЦБ: <b>{fmt(cbr['usd'])} ₽</b> {arrow(du)} {signed(du)} ₽",
             f"💶 Евро ЦБ: <b>{fmt(cbr['eur'])} ₽</b> {arrow(de)} {signed(de)} ₽",
             f"💴 Юань ЦБ: <b>{fmt(cbr['cny'], 3)} ₽</b>"]
    if usd_mkt:
        lines.append(f"📊 Доллар на Мосбирже: <b>{fmt(usd_mkt)} ₽</b>")
    if cny_mkt:
        lines.append(f"📊 Юань на Мосбирже: <b>{fmt(cny_mkt, 3)} ₽</b>")
    brent, prev = get_brent()
    if brent:
        s = f"🛢 Нефть Brent: <b>${fmt(brent)}</b>"
        if prev:
            s += f" {arrow(brent - prev)} {signed((brent - prev) / prev * 100, 1)}%"
        lines.append(s)
    lines.append("\n#курс #USDRUB")
    return "\n".join(lines)


def clean(text, limit=260):
    text = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    m = re.search(r"^(.+[.!?])\s", cut)
    return m.group(1) if m and len(m.group(1)) > 80 else cut.rsplit(" ", 1)[0] + "…"


def collect_news(st, first_run):
    posted = set(st.get("posted", []))
    now = time.time()
    max_age = 24 * 3600 if first_run else 48 * 3600
    items, seen_titles = [], set()
    for src, url in FEEDS.items():
        try:
            r = requests.get(url, timeout=20, headers=UA)
            feed = feedparser.parse(r.content)
        except Exception as e:
            print(f"Feed {src} error:", e, file=sys.stderr)
            continue
        for e in feed.entries:
            title = clean(e.get("title"), 300)
            summary = clean(e.get("summary") or e.get("description") or "")
            if not title or not KEYWORDS.search(title + " " + summary):
                continue
            uid = hashlib.md5((e.get("link") or title).encode()).hexdigest()[:16]
            norm = re.sub(r"\W+", "", title.lower())[:60]
            if uid in posted or norm in seen_titles:
                continue
            tt = e.get("published_parsed") or e.get("updated_parsed")
            ts = calendar.timegm(tt) if tt else now
            if now - ts > max_age:
                continue
            seen_titles.add(norm)
            items.append({"id": uid, "src": src, "title": title, "summary": summary,
                          "link": e.get("link", ""), "ts": ts})
    # приоритет: прямо про доллар/рубль, затем свежесть
    items.sort(key=lambda i: (not re.search(r"доллар|рубл|курс", i["title"], re.I), -i["ts"]))
    return items


def news_text(it):
    t = f"📰 <b>{html.escape(it['title'])}</b>\n\n"
    if it["summary"] and it["summary"][:40] != it["title"][:40]:
        t += f"{html.escape(it['summary'])}\n\n"
    t += f"🔗 <a href=\"{html.escape(it['link'])}\">{it['src']}</a>"
    return t


def main():
    if not TOKEN:
        sys.exit("Нет TELEGRAM_TOKEN")
    st = load_state()
    first_run = not st.get("posted")
    now = datetime.now(MSK)

    cbr = get_cbr()
    usd_mkt = get_moex("USD000UTSTOM")
    cny_mkt = get_moex("CNYRUB_TOM")

    # 1) алерт о скачке (биржевой курс точнее, иначе ЦБ)
    if usd_mkt:
        check_alert(st, usd_mkt, "Мосбиржа")
    elif cbr:
        check_alert(st, cbr["usd"], "ЦБ РФ")

    # 2) утренняя сводка раз в день после 9:00 МСК
    today = now.strftime("%Y-%m-%d")
    if cbr and (FORCE_SUMMARY or (now.hour >= SUMMARY_HOUR and st.get("summary_date") != today)):
        if tg_send(morning_summary(cbr, usd_mkt, cny_mkt)):
            st["summary_date"] = today

    # 3) новости
    news = collect_news(st, first_run)
    posted = st.setdefault("posted", [])
    if first_run:  # не заваливать канал при первом запуске: остальные помечаем как увиденные
        for it in news[MAX_NEWS:]:
            posted.append(it["id"])
    for it in news[:MAX_NEWS]:
        if tg_send(news_text(it)):
            posted.append(it["id"])
    print(f"news found: {len(news)}, posted up to {MAX_NEWS}")
    save_state(st)


if __name__ == "__main__":
    main()
