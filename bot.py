#!/usr/bin/env python3
"""USD/RUB новостной бот для Telegram-канала. Запускается по расписанию (GitHub Actions)."""
import calendar, hashlib, html, json, os, re, sys, time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
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
    "Investing.com": "https://ru.investing.com/rss/news.rss",
    "Investing.com FX": "https://ru.investing.com/rss/news_1.rss",
    "Smart-Lab": "https://smart-lab.ru/news/rss/",
    "Smart-Lab блог": "https://smart-lab.ru/rss/",
}
# Публичные каналы (превью t.me/s/…). @mmi нет — канал MMI это @russianmacro,
# «Твердые цифры» — @xtxixty.
TG_CHANNELS = ("markettwits", "russianmacro", "xtxixty", "banksta", "cbrstocks", "rbc_news", "interfaxonline")
TG_SEEN_LIMIT = 100
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
        cny = v["CNY"]
        nominal = cny.get("Nominal") or 1
        cny_prev = cny.get("Previous")
        return {"date": d["Date"][:10], "usd": v["USD"]["Value"], "usd_prev": v["USD"]["Previous"],
                "eur": v["EUR"]["Value"], "eur_prev": v["EUR"]["Previous"],
                "cny": cny["Value"] / nominal,
                "cny_prev": None if cny_prev is None else cny_prev / nominal}
    except Exception as e:
        print("CBR error:", e, file=sys.stderr)
        return None


def get_key_rate():
    """Текущая ключевая ставка ЦБ (SOAP KeyRate, без ключа). При сбое — None."""
    try:
        today = datetime.now(MSK).date()
        frm = today - timedelta(days=60)
        body = f"""<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema" xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <KeyRate xmlns="http://web.cbr.ru/">
      <fromDate>{frm.isoformat()}</fromDate>
      <ToDate>{today.isoformat()}</ToDate>
    </KeyRate>
  </soap:Body>
</soap:Envelope>"""
        r = requests.post(
            "https://www.cbr.ru/DailyInfoWebServ/DailyInfo.asmx",
            data=body.encode("utf-8"), timeout=20,
            headers={**UA, "Content-Type": "text/xml; charset=utf-8",
                     "SOAPAction": "http://web.cbr.ru/KeyRate"})
        r.raise_for_status()
        best = None
        today_s = today.isoformat()
        for kr in ET.fromstring(r.content).iter("KR"):
            dt = rate = None
            for child in kr:
                name = child.tag.rsplit("}", 1)[-1]
                if name == "DT" and child.text:
                    dt = child.text[:10]
                elif name == "Rate" and child.text:
                    rate = float(child.text)
            if dt and rate is not None and dt <= today_s and (best is None or dt > best[0]):
                best = (dt, rate)
        return round(best[1], 2) if best else None
    except Exception:
        return None


def key_rate_note(rate):
    if round(float(rate), 2) <= 12:
        return "Кредиты дешевеют — рубль может слабеть"
    return "Кредиты дорогие, вклады выгодные — рубль держится крепко 💪"


def key_rate_change_text(rate, prev):
    pct = fmt_pct(rate)
    if rate < prev:
        return (f"📉 <b>ЦБ снизил ставку до {pct}</b>\n"
                "Рубль может ослабнуть, а доллар подорожать. Техника из-за рубежа может стать дороже 📦⬆️")
    return (f"📈 <b>ЦБ повысил ставку до {pct}</b>\n"
            "Рубль может укрепиться, а доллар подешеветь. Хорошее время для закупки 📦⬇️")


def key_rate_update(st, rate):
    """Первый раз только запоминает ставку. При изменении возвращает текст поста (state не трогает)."""
    if rate is None:
        return None
    rate = round(float(rate), 2)
    prev = st.get("key_rate")
    if prev is None:
        st["key_rate"] = rate
        return None
    prev = round(float(prev), 2)
    if rate == prev:
        return None
    return key_rate_change_text(rate, prev)


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


def fmt_pct(x):
    """Проценты по-русски: 14% или 16,5%."""
    x = round(float(x), 2)
    if x == int(x):
        return f"{int(x)}%"
    return f"{x:.2f}".rstrip("0").replace(".", ",") + "%"


def arrow(diff):
    return "🔺" if diff > 0 else ("🔻" if diff < 0 else "▪️")


def signed(x, d=2):
    return ("+" if x > 0 else "") + fmt(x, d)


def _diff(now, prev):
    if now is None or prev is None:
        return None
    try:
        return float(now) - float(prev)
    except (TypeError, ValueError):
        return None


def trend_icon(diff, places=2):
    """📈 если выросло, 📉 если упало. Пусто, если изменения нет или оно не известно."""
    if diff is None:
        return ""
    try:
        value = float(diff)
    except (TypeError, ValueError):
        return ""
    if round(value, places) == 0:
        return ""
    return "📈" if value > 0 else "📉"


def _usd_bits(value, diff):
    """80,54 📉0,59 — сумма изменения только у доллара, без знака."""
    text = fmt(float(value), 2)
    icon = trend_icon(diff, 2)
    if icon:
        text += f" {icon}{fmt(abs(float(diff)), 2)}"
    return text


def _level(symbol, value, diff, digits):
    text = f"{symbol} {fmt(float(value), digits)}"
    icon = trend_icon(diff, digits)
    if icon:
        text += f" {icon}"
    return text


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


def morning_summary(cbr, key_rate=None, brent=None, brent_prev=None):
    """Одна строка котировок и короткая строка ставки с пояснением."""
    cbr = cbr or {}
    parts = []
    try:
        dt = datetime.fromisoformat(str(cbr.get("date", ""))[:10])
        parts.append(f"📊 {dt.day:02d}.{dt.month:02d}")
    except (TypeError, ValueError):
        parts.append("📊")
    if cbr.get("usd") is not None:
        try:
            parts.append("$ " + _usd_bits(cbr["usd"], _diff(cbr.get("usd"), cbr.get("usd_prev"))))
        except (TypeError, ValueError):
            pass
    if cbr.get("eur") is not None:
        try:
            parts.append(_level("€", cbr["eur"], _diff(cbr.get("eur"), cbr.get("eur_prev")), 2))
        except (TypeError, ValueError):
            pass
    if cbr.get("cny") is not None:
        try:
            parts.append(_level("¥", cbr["cny"], _diff(cbr.get("cny"), cbr.get("cny_prev")), 2))
        except (TypeError, ValueError):
            pass
    if brent is not None:
        try:
            parts.append(_level("🛢", brent, _diff(brent, brent_prev), 1))
        except (TypeError, ValueError):
            pass
    lines = [" · ".join(parts)]
    if key_rate is not None:
        try:
            lines.append(
                f"🏦 <b>Ставка ЦБ: {html.escape(fmt_pct(key_rate))}</b> — {html.escape(key_rate_note(key_rate))}")
        except (TypeError, ValueError):
            pass
    return "\n".join(lines)


def clean(text, limit=260):
    text = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    m = re.search(r"^(.+[.!?])\s", cut)
    return m.group(1) if m and len(m.group(1)) > 80 else cut.rsplit(" ", 1)[0] + "…"


def strip_links(text):
    text = re.sub(r"https?://\S+", " ", text or "")
    text = re.sub(r"(?<!\w)t\.me/\S+", " ", text)
    return text


def clip_text(text, limit=250):
    """Первые ~limit символов без обрыва слова."""
    text = clean(strip_links(text), 10**6)
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return (cut or text[:limit]) + "…"


class _TgParser(HTMLParser):
    """Посты со страницы https://t.me/s/<channel>."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.posts = []
        self._cur = None
        self._in_text = False
        self._depth = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "div" and a.get("data-post"):
            channel, _, msg_id = a["data-post"].partition("/")
            cls = a.get("class", "")
            # «X pinned …» — служебное сообщение, не новость. id всё равно запоминаем.
            self._cur = {"channel": channel, "msg_id": msg_id, "parts": [], "dt": None,
                         "service": "service_message" in cls}
            self.posts.append(self._cur)
            self._in_text = False
            return
        if self._cur is None or self._cur.get("service"):
            if self._cur is not None and tag == "time" and a.get("datetime"):
                self._cur["dt"] = a["datetime"]
            return
        cls = a.get("class", "")
        if tag == "div" and "tgme_widget_message_text" in cls:
            self._in_text = True
            self._depth = 1
            return
        if self._in_text and tag == "div":
            self._depth += 1
        if self._in_text and tag == "br":
            self._cur["parts"].append("\n")
        if tag == "time" and a.get("datetime"):
            self._cur["dt"] = a["datetime"]

    def handle_endtag(self, tag):
        if self._in_text and tag == "div":
            self._depth -= 1
            if self._depth <= 0:
                self._in_text = False

    def handle_data(self, data):
        if self._in_text and self._cur is not None:
            self._cur["parts"].append(data)


def _tg_remember(bucket, ids):
    have = set(bucket)
    for i in ids:
        if i not in have:
            bucket.append(i)
            have.add(i)
    if len(bucket) > TG_SEEN_LIMIT:
        del bucket[:-TG_SEEN_LIMIT]


def fetch_tg_posts(channel):
    r = requests.get(f"https://t.me/s/{channel}", timeout=20, headers=UA)
    r.raise_for_status()
    parser = _TgParser()
    parser.feed(r.text)
    posts = []
    for p in parser.posts:
        if not p["msg_id"].isdigit():
            continue
        ts = None
        if p["dt"]:
            try:
                ts = datetime.fromisoformat(p["dt"]).timestamp()
            except ValueError:
                ts = None
        text = re.sub(r"\s+", " ", "".join(p["parts"])).strip()
        posts.append({"id": f"tg:{p['channel'] or channel}:{p['msg_id']}",
                      "channel": p["channel"] or channel, "msg_id": p["msg_id"],
                      "text": text, "ts": ts})
    return posts


def collect_tg(st, posted, now, max_age, seen_titles):
    """Новые посты каналов. При первом знакомстве только запоминает id, ничего не отдаёт."""
    items = []
    if not isinstance(st.get("tg_seen"), dict):
        st["tg_seen"] = {}
    seen = st["tg_seen"]
    for channel in TG_CHANNELS:
        try:
            posts = fetch_tg_posts(channel)
        except Exception as e:
            print(f"TG @{channel} error:", e, file=sys.stderr)
            continue
        if not posts:
            continue
        bucket = seen.get(channel)
        if not isinstance(bucket, list):
            seen[channel] = [p["id"] for p in posts][-TG_SEEN_LIMIT:]
            continue
        known = set(bucket)
        skipped = []
        for p in posts:
            if p["id"] in known or p["id"] in posted:
                continue
            text = strip_links(p["text"])
            too_old = p["ts"] is not None and now - p["ts"] > max_age
            if not text or too_old or not KEYWORDS.search(text):
                skipped.append(p["id"])
                continue
            title = clean(text, 300)
            summary = clip_text(text, 250)
            if not summary:
                skipped.append(p["id"])
                continue
            norm = re.sub(r"\W+", "", title.lower())[:60]
            if norm in seen_titles:
                skipped.append(p["id"])
                continue
            seen_titles.add(norm)
            items.append({"id": p["id"], "src": f"@{p['channel']}", "title": title,
                          "summary": summary, "link": f"https://t.me/{p['channel']}/{p['msg_id']}",
                          "ts": p["ts"] if p["ts"] is not None else now,
                          "tg": p["channel"], "msg_id": p["msg_id"]})
        if skipped:
            _tg_remember(bucket, skipped)
    return items


def remember_tg(st, it):
    if not it.get("tg"):
        return
    seen = st.setdefault("tg_seen", {})
    bucket = seen.setdefault(it["tg"], [])
    if isinstance(bucket, list):
        _tg_remember(bucket, [it["id"]])


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
    try:
        items.extend(collect_tg(st, posted, now, max_age, seen_titles))
    except Exception as e:
        print("TG error:", e, file=sys.stderr)
    # приоритет: прямо про доллар/рубль, затем свежесть
    items.sort(key=lambda i: (not re.search(r"доллар|рубл|курс", i["title"], re.I), -i["ts"]))
    return items


_TAG_RULES = (
    ("#ЦБ", re.compile(r"ставк|\bцб\b|банк\s+россии", re.I)),
    ("#нефть", re.compile(r"нефт|brent|опек", re.I)),
    ("#санкции", re.compile(r"санкц", re.I)),
    ("#курс", re.compile(r"доллар|рубл|юан|(?<![а-яёa-z])курс", re.I)),
)


def news_tag(text):
    """Первое совпадение: #ЦБ, #нефть, #санкции, #курс, иначе #рынки."""
    for tag, rx in _TAG_RULES:
        if rx.search(text or ""):
            return tag
    return "#рынки"


def quote_context(st, cbr, usd_mkt, brent, brent_prev, key_rate):
    """Свежие котировки этого запуска, иначе то, что уже есть в state. Без записи state."""
    ctx = {}
    st = st if isinstance(st, dict) else {}
    cbr = cbr if isinstance(cbr, dict) else {}
    usd = usd_diff = None
    if usd_mkt is not None:
        try:
            usd = float(usd_mkt)
            usd_diff = _diff(usd, st.get("last_rate"))
        except (TypeError, ValueError):
            usd = None
    if usd is None and cbr.get("usd") is not None:
        try:
            usd = float(cbr["usd"])
            usd_diff = _diff(cbr.get("usd"), cbr.get("usd_prev"))
        except (TypeError, ValueError):
            usd = None
    if usd is None and st.get("last_rate") is not None:
        try:
            usd = float(st["last_rate"])
            usd_diff = None
        except (TypeError, ValueError):
            usd = None
    if usd is not None:
        ctx["usd"] = usd
        if usd_diff is not None:
            ctx["usd_diff"] = usd_diff
    try:
        if brent is not None:
            ctx["brent"] = float(brent)
            diff = _diff(brent, brent_prev)
            if diff is not None:
                ctx["brent_diff"] = diff
    except (TypeError, ValueError):
        ctx.pop("brent", None)
        ctx.pop("brent_diff", None)
    try:
        rate = key_rate if key_rate is not None else st.get("key_rate")
        if rate is not None:
            ctx["key_rate"] = float(rate)
    except (TypeError, ValueError):
        pass
    return ctx


def _brent_news(price, diff):
    text = f"Brent ${fmt(round(float(price)), 0)}"
    icon = trend_icon(diff, 0)
    if icon:
        text += f" {icon}"
    return text


def news_ticker(ctx):
    """⚡️ USD/RUB … · Brent … · ставка … — пропуск того, чего нет."""
    if not isinstance(ctx, dict):
        return ""
    parts = []
    if ctx.get("usd") is not None:
        try:
            parts.append("USD/RUB " + _usd_bits(ctx["usd"], ctx.get("usd_diff")))
        except (TypeError, ValueError):
            pass
    if ctx.get("brent") is not None:
        try:
            parts.append(_brent_news(ctx["brent"], ctx.get("brent_diff")))
        except (TypeError, ValueError):
            pass
    if ctx.get("key_rate") is not None:
        try:
            parts.append(f"ставка {fmt_pct(ctx['key_rate'])}")
        except (TypeError, ValueError):
            pass
    if not parts:
        return ""
    return "⚡️ " + " · ".join(parts)


def news_line2(it):
    title = (it.get("title") or "").strip()
    summary = (it.get("summary") or "").strip()
    text = (summary or title) if it.get("tg") else (title or summary)
    if not text:
        return ""
    return f"<b>{html.escape(text)}</b>"


def news_footer(it):
    tag = news_tag(f"{it.get('title') or ''} {it.get('summary') or ''}")
    src = html.escape(it.get("src") or "")
    link = (it.get("link") or "").strip()
    if link and src:
        return f'{tag} · <a href="{html.escape(link, quote=True)}">{src}</a>'
    if link:
        return f'{tag} · <a href="{html.escape(link, quote=True)}">Источник</a>'
    if src:
        return f"{tag} · {src}"
    return tag


def news_text(it, ctx=None):
    it = it or {}
    try:
        tick = news_ticker(ctx)
    except Exception:
        tick = ""
    try:
        body = news_line2(it)
    except Exception:
        body = ""
    try:
        foot = news_footer(it)
    except Exception:
        foot = "#рынки"
    return "\n".join(part for part in (tick, body, foot) if part)


def main():
    if not TOKEN:
        sys.exit("Нет TELEGRAM_TOKEN")
    st = load_state()
    first_run = not st.get("posted")
    now = datetime.now(MSK)

    cbr = get_cbr()
    usd_mkt = get_moex("USD000UTSTOM")
    get_moex("CNYRUB_TOM")  # источник оставляем; в сводке юань — курс ЦБ
    key_rate = get_key_rate()
    brent, brent_prev = get_brent()

    # 1) алерт о скачке (биржевой курс точнее, иначе ЦБ)
    if usd_mkt:
        check_alert(st, usd_mkt, "Мосбиржа")
    elif cbr:
        check_alert(st, cbr["usd"], "ЦБ РФ")

    # 2) утренняя сводка раз в день после 9:00 МСК
    today = now.strftime("%Y-%m-%d")
    if cbr and (FORCE_SUMMARY or (now.hour >= SUMMARY_HOUR and st.get("summary_date") != today)):
        if tg_send(morning_summary(cbr, key_rate, brent, brent_prev)):
            st["summary_date"] = today

    # изменение ключевой ставки — отдельным сообщением, один раз
    rate_msg = key_rate_update(st, key_rate)
    if rate_msg and tg_send(rate_msg):
        st["key_rate"] = round(float(key_rate), 2)

    # 3) новости
    news = collect_news(st, first_run)
    posted = st.setdefault("posted", [])
    ctx = quote_context(st, cbr, usd_mkt, brent, brent_prev, key_rate)
    if first_run:  # не заваливать канал при первом запуске: остальные помечаем как увиденные
        for it in news[MAX_NEWS:]:
            posted.append(it["id"])
            remember_tg(st, it)
    for it in news[:MAX_NEWS]:
        if tg_send(news_text(it, ctx)):
            posted.append(it["id"])
            remember_tg(st, it)
    print(f"news found: {len(news)}, posted up to {MAX_NEWS}")
    save_state(st)


if __name__ == "__main__":
    main()
