#!/usr/bin/env python3
"""USD/RUB новостной бот для Telegram-канала. Запускается по расписанию (GitHub Actions)."""
import calendar, hashlib, html, json, os, re, sys, time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from html.parser import HTMLParser
import feedparser, requests

TOKEN = os.environ.get("TELEGRAM_TOKEN") or os.environ.get("USDRUB_BOT_TOKEN")
CHANNEL = os.environ.get("CHANNEL_ID", "@dollar_rub_news")
STATE_FILE = os.environ.get("STATE_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json"))
FORCE_SUMMARY = os.environ.get("FORCE_SUMMARY") == "1"
MSK = timezone(timedelta(hours=3))
UA = {"User-Agent": "Mozilla/5.0 (usdrub-news-bot)"}
MAX_NEWS = 5
MAX_AI = 8
BODY_SHORT = 300
AI_URL = "https://models.github.ai/inference/chat/completions"
AI_MODEL = "openai/gpt-4o-mini"
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
    if isinstance(st.get("ai_seen"), list):
        st["ai_seen"] = st["ai_seen"][-500:]
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


def excerpt(text, limit=300):
    """Первые ~limit символов тела новости, без обрыва слова."""
    return clip_text(text, limit)


_P_SKIP = {"script", "style", "noscript", "svg", "head", "nav", "footer", "aside", "form"}
_P_JUNK = re.compile(r"cookie|подпишит|реклам|все права|©|mailto:", re.I)


class _PText(HTMLParser):
    """Основной текст страницы: абзацы <p>, без меню и скриптов."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self._skip = 0
        self._p = 0
        self._buf = []

    def _flush(self):
        text = re.sub(r"\s+", " ", "".join(self._buf)).strip()
        self._buf = []
        if len(text) >= 40 and not _P_JUNK.search(text):
            self.parts.append(text)

    def handle_starttag(self, tag, attrs):
        if tag in _P_SKIP:
            self._skip += 1
        elif tag == "br" and self._p and not self._skip:
            self._buf.append(" ")
        elif tag == "p" and not self._skip:
            if self._p:
                self._flush()
            self._p += 1
            self._buf = []

    def handle_endtag(self, tag):
        if tag in _P_SKIP and self._skip:
            self._skip -= 1
        elif tag == "p" and self._p and not self._skip:
            self._p -= 1
            self._flush()

    def handle_data(self, data):
        if self._p and not self._skip:
            self._buf.append(data)


def extract_paragraphs(html_text, limit=5000):
    parser = _PText()
    try:
        parser.feed(html_text or "")
        parser.close()
    except Exception:
        return ""
    if parser._buf:
        parser._flush()
    text = re.sub(r"\s+", " ", " ".join(parser.parts)).strip()
    return text[:limit]


def fetch_article_text(url, get=None):
    """Скачать страницу и вытащить абзацы. При любой ошибке — пустая строка."""
    get = get or requests.get
    try:
        r = get(url, timeout=15, headers={**UA, "Accept": "text/html,application/xhtml+xml"})
        status = getattr(r, "status_code", 200)
        if status >= 400:
            return ""
        headers = getattr(r, "headers", {}) or {}
        ctype = (headers.get("Content-Type") or headers.get("content-type") or "").lower()
        if ctype and "html" not in ctype and "xml" not in ctype and "text/" not in ctype:
            return ""
        html_text = getattr(r, "text", "") or ""
        if not html_text and getattr(r, "content", None):
            html_text = r.content[:400000].decode("utf-8", errors="replace")
        return extract_paragraphs(html_text[:400000])
    except Exception as e:
        print("article error:", e, file=sys.stderr)
        return ""


def load_body(it, get=None):
    """RSS: описание, а если оно короче 300 символов — текст страницы. Telegram: весь пост."""
    it = it or {}
    try:
        if it.get("tg"):
            return (it.get("body") or it.get("summary") or it.get("title") or "").strip()
        text = (it.get("body") or it.get("summary") or "").strip()
        link = (it.get("link") or "").strip()
        if len(text) < BODY_SHORT and link.startswith(("http://", "https://")):
            extra = fetch_article_text(link, get=get)
            if extra and len(extra) > len(text):
                return extra
        return text or (it.get("title") or "").strip()
    except Exception as e:
        print("body error:", e, file=sys.stderr)
        return (it.get("body") or it.get("summary") or it.get("title") or "").strip()


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
            full = clean(text, 8000)
            title = clean(full, 300)
            summary = clip_text(full, 250)
            if not summary and not full:
                skipped.append(p["id"])
                continue
            norm = re.sub(r"\W+", "", title.lower())[:60]
            if norm in seen_titles:
                skipped.append(p["id"])
                continue
            seen_titles.add(norm)
            items.append({"id": p["id"], "src": f"@{p['channel']}", "title": title,
                          "summary": summary or title, "body": full or summary or title,
                          "link": f"https://t.me/{p['channel']}/{p['msg_id']}",
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
            body = clean(e.get("summary") or e.get("description") or "", 8000)
            if not title or not KEYWORDS.search(title + " " + body):
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
            items.append({"id": uid, "src": src, "title": title,
                          "summary": clip_text(body, 260) if body else "", "body": body,
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


def news_footer(it):
    blob = " ".join(str(it.get(k) or "") for k in ("title", "summary", "body"))
    tag = news_tag(blob)
    src = html.escape(it.get("src") or "")
    link = (it.get("link") or "").strip()
    if link and src:
        return f'{tag} · <a href="{html.escape(link, quote=True)}">{src}</a>'
    if link:
        return f'{tag} · <a href="{html.escape(link, quote=True)}">Источник</a>'
    if src:
        return f"{tag} · {src}"
    return tag


# impact — направление USD/RUB: up доллар дорожает, down рубль крепнет.
IMPACT_LABEL = {
    "up": "🔴 рубль слабеет",
    "down": "🟢 рубль крепнет",
    "neutral": "⚪️ нейтрально",
}
AI_SYSTEM = (
    "Ты редактор Telegram-канала о курсе доллара. Читатель — торговец электроникой, "
    "который закупает технику за рубежом. Ответь одним JSON-объектом без markdown и без пояснений. "
    "Ключи: relevant (bool) — true, только если новость про рубль, доллар, ЦБ, нефть, санкции, "
    "экономику России или импорт, иначе false; "
    "summary — 2–3 предложения, суть новости простым русским языком; "
    "impact — строго одно из up, down, neutral: влияние на курс USD/RUB "
    "(up — доллар к рублю растёт, рубль слабеет; down — доллар к рублю падает, рубль крепнет; "
    "neutral — явного влияния нет); "
    "analysis — 1–2 предложения, почему курс так отреагирует, простым языком для закупки техники. "
    "Если relevant=false, оставь summary и analysis пустыми строками, а impact поставь neutral. "
    "Не выдумывай факты, которых нет в тексте. Без HTML, списков и ссылок."
)


def _norm_title(title):
    text = (title or "").lower().replace("ё", "е")
    text = re.sub(r"[^0-9a-zа-я]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def titles_similar(a, b):
    """Почти одинаковые заголовки разных источников в одном запуске."""
    na, nb = _norm_title(a), _norm_title(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    short, long = (na, nb) if len(na) <= len(nb) else (nb, na)
    if len(short) >= 40 and short in long:
        return True
    if min(len(na), len(nb)) < 20:
        return False
    return SequenceMatcher(None, na, nb).ratio() >= 0.84


def _plain(text, limit):
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return (cut or text[:limit]) + "…"


def _as_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("true", "1", "yes", "да"):
            return True
        if v in ("false", "0", "no", "нет"):
            return False
    return None


def parse_ai_payload(content):
    """JSON модели. None — ответ битый, пост уйдёт запасным отрывком."""
    if isinstance(content, list):
        bits = []
        for part in content:
            if isinstance(part, str):
                bits.append(part)
            elif isinstance(part, dict):
                bits.append(str(part.get("text") or part.get("content") or ""))
        content = "".join(bits)
    text = str(content or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I)
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    if not isinstance(data, dict):
        return None
    relevant = _as_bool(data.get("relevant"))
    if relevant is None:
        return None
    if not relevant:
        return {"relevant": False, "summary": "", "impact": "neutral", "analysis": ""}
    summary = _plain(data.get("summary"), 800)
    if not summary:
        return None
    impact = str(data.get("impact") or "").strip().lower()
    if impact not in IMPACT_LABEL:
        impact = "neutral"
    return {"relevant": True, "summary": summary, "impact": impact,
            "analysis": _plain(data.get("analysis"), 500)}


def _message_content(data):
    try:
        msg = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return None
    if isinstance(msg, str):
        return msg
    if isinstance(msg, list):
        return msg
    return None


class ModelClient:
    """GitHub Models. Сбой и лимит не роняют бота: analyze возвращает None."""

    def __init__(self, limit=None, post=None):
        self.limit = MAX_AI if limit is None else limit
        self.used = 0
        self.stop = False
        self._post = post or requests.post

    def analyze(self, title, body):
        if self.stop or self.used >= self.limit:
            return None
        token = os.environ.get("GITHUB_TOKEN")
        if not token:
            self.stop = True
            print("AI skipped: no GITHUB_TOKEN", file=sys.stderr)
            return None
        self.used += 1
        messages = [
            {"role": "system", "content": AI_SYSTEM},
            {"role": "user", "content": f"Заголовок: {title}\n\nТекст новости:\n{(body or '')[:4000]}"},
        ]
        try:
            content = self._complete(token, messages, json_mode=True)
            if content is None and not self.stop:
                content = self._complete(token, messages, json_mode=False)
            if not content:
                return None
            return parse_ai_payload(content)
        except Exception as e:
            print("AI error:", e, file=sys.stderr)
            return None

    def _complete(self, token, messages, json_mode):
        payload = {"model": AI_MODEL, "messages": messages, "temperature": 0.2, "max_tokens": 500}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        r = self._post(
            AI_URL, headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/vnd.github+json",
            }, json=payload, timeout=30)
        status = getattr(r, "status_code", 0)
        if status == 429:
            self.stop = True
            print("AI rate limit", file=sys.stderr)
            return None
        if status == 400 and json_mode:
            return None
        if status == 400:
            self.stop = True
            print("AI HTTP", status, file=sys.stderr)
            return None
        if status in (401, 403, 404):
            self.stop = True
            print("AI HTTP", status, file=sys.stderr)
            return None
        if not getattr(r, "ok", 200 <= status < 300):
            print("AI HTTP", status, file=sys.stderr)
            return None
        try:
            data = r.json()
        except ValueError:
            return None
        return _message_content(data)


def news_text(it, ctx=None, ai=None):
    it = it or {}
    try:
        tick = news_ticker(ctx)
    except Exception:
        tick = ""
    try:
        foot = news_footer(it)
    except Exception:
        foot = "#рынки"
    blocks = []
    try:
        if isinstance(ai, dict) and ai.get("relevant") and (ai.get("summary") or "").strip():
            blocks.append("📰 " + html.escape(_plain(ai["summary"], 800)))
            label = IMPACT_LABEL.get(ai.get("impact"), IMPACT_LABEL["neutral"])
            analysis = _plain(ai.get("analysis"), 500)
            impact = f"📊 Влияние на курс: {label}"
            if analysis:
                impact += "\n" + html.escape(analysis)
            blocks.append(impact)
        else:
            raw = it.get("body") or it.get("summary") or ""
            text = excerpt(raw, 300)
            if text:
                blocks.append("📰 " + html.escape(text))
    except Exception as e:
        print("format error:", e, file=sys.stderr)
        blocks = []
    if not blocks:
        return ""
    return "\n\n".join(part for part in (tick, *blocks, foot) if part)


def _model_used(analyze):
    owner = getattr(analyze, "__self__", None)
    used = getattr(owner, "used", None)
    return used if isinstance(used, int) else None


def select_news_posts(news, ctx, analyze, get=None, seen_ids=None, ai_seen=None):
    """До MAX_NEWS постов. Модель вызывается только для id, которых ещё не было."""
    chosen, posts, skipped = [], [], []
    seen = set(seen_ids or [])
    ai_list = ai_seen if isinstance(ai_seen, list) else None
    ai_set = set(ai_list or [])
    for it in news or []:
        if len(posts) >= MAX_NEWS:
            break
        if not isinstance(it, dict):
            continue
        uid = it.get("id")
        if uid and uid in seen:
            skipped.append(it)
            continue
        title = it.get("title") or ""
        try:
            dup = any(titles_similar(title, prev) for prev in chosen)
        except Exception:
            dup = False
        if dup:
            skipped.append(it)
            continue
        chosen.append(title)
        try:
            body = load_body(it, get=get)
        except Exception as e:
            print("body error:", e, file=sys.stderr)
            body = (it.get("summary") or title or "").strip()
        it["body"] = body
        ai = None
        try:
            if uid and uid in ai_set:
                ai = None
            elif analyze:
                before = _model_used(analyze)
                ai = analyze(title, body)
                after = _model_used(analyze)
                if uid and before is not None and after is not None and after > before:
                    ai_set.add(uid)
                    if ai_list is not None and uid not in ai_list:
                        ai_list.append(uid)
            else:
                ai = None
        except Exception as e:
            print("AI error:", e, file=sys.stderr)
            ai = None
        if isinstance(ai, dict) and ai.get("relevant") is False:
            skipped.append(it)
            continue
        use_ai = isinstance(ai, dict) and ai.get("relevant") and (ai.get("summary") or "").strip()
        try:
            text = news_text(it, ctx, ai if use_ai else None)
        except Exception as e:
            print("format error:", e, file=sys.stderr)
            text = ""
        if not text:
            skipped.append(it)
            continue
        posts.append((it, text))
    return posts, skipped


def mark_seen(st, posted, seen_ids, it):
    uid = it.get("id")
    if not uid:
        return
    if uid not in seen_ids:
        posted.append(uid)
        seen_ids.add(uid)
    remember_tg(st, it)


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

    # 3) новости: текст + разбор модели, не больше 5 постов и 8 вызовов
    news = collect_news(st, first_run)
    posted = st.setdefault("posted", [])
    seen_ids = set(posted)
    ctx = quote_context(st, cbr, usd_mkt, brent, brent_prev, key_rate)
    client = ModelClient()
    ai_seen = st.get("ai_seen")
    if not isinstance(ai_seen, list):
        ai_seen = []
        st["ai_seen"] = ai_seen
    posts, skipped = select_news_posts(
        news, ctx, client.analyze, seen_ids=seen_ids, ai_seen=ai_seen)
    for it in skipped:
        mark_seen(st, posted, seen_ids, it)
    attempted = set()
    sent = 0
    for it, text in posts:
        attempted.add(it.get("id"))
        if tg_send(text):
            mark_seen(st, posted, seen_ids, it)
            sent += 1
    if first_run:  # не заваливать канал при первом запуске: остальное помечаем как увиденное
        for it in news:
            if it.get("id") not in seen_ids and it.get("id") not in attempted:
                mark_seen(st, posted, seen_ids, it)
    print(f"news found: {len(news)}, posted {sent}, ai calls {client.used}")
    save_state(st)


if __name__ == "__main__":
    main()
