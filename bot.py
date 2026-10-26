#!/usr/bin/env python3
"""USD/RUB новостной бот для Telegram-канала. Запускается по расписанию (GitHub Actions)."""
import calendar, hashlib, html, io, json, os, re, sys, time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
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
# Заседания по ставке в 2026: cbr.ru/dkp/cal_mp/. Опорные — с прогнозом.
# Ближайшее после 23.10.2026 — 18.12.2026 (подтверждено релизом ЦБ).
CBR_MEETINGS_2026 = (
    ("2026-02-13", True),
    ("2026-03-20", False),
    ("2026-04-24", True),
    ("2026-06-19", False),
    ("2026-07-24", True),
    ("2026-09-11", False),
    ("2026-10-23", True),
    ("2026-12-18", False),
)
# Решение 23.10.2026: сохранить 14%. Запас, если SOAP ЦБ молчит.
KEY_RATE_KNOWN = 14.0
# Второй день заседания FOMC, federalreserve.gov, календарь 2026.
FOMC_2026 = (
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
    "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09",
)
POLL_QUESTION = "Куда пойдёт доллар на неделе?"
POLL_OPTIONS = ("⬆️ Выше", "⬇️ Ниже", "↔️ Боковик")
MEETING_HINT = (
    "Сегодня день решения Банка России по ключевой ставке, пресс-релиз около 13:30 МСК. "
    "Если в тексте есть само решение, новая ставка или реакция курса — urgency=urgent, topic=cbr. "
    "Цифру ставки пиши только если она есть в тексте."
)

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
    if isinstance(st.get("day_news"), list):
        st["day_news"] = st["day_news"][-40:]
    if isinstance(st.get("rate_samples"), list):
        st["rate_samples"] = st["rate_samples"][-48:]
    if isinstance(st.get("recent_news"), list):
        st["recent_news"] = st["recent_news"][-40:]
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


def tg_photo(png, caption):
    """PNG графика. Сбой не роняет бота."""
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendPhoto", timeout=40,
            data={"chat_id": CHANNEL, "caption": (caption or "")[:1024], "parse_mode": "HTML"},
            files={"photo": ("cbr-week.png", png, "image/png")})
        ok = r.ok and r.json().get("ok")
        if not ok:
            print("Telegram photo error:", r.status_code, r.text[:300], file=sys.stderr)
        else:
            print("posted photo", r.json()["result"]["message_id"])
        time.sleep(1.5)
        return bool(ok)
    except Exception as e:
        print("Telegram photo error:", e, file=sys.stderr)
        return False


def tg_poll(question, options):
    """Анонимный опрос. В канале Telegram опросы и так анонимные."""
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendPoll", timeout=20,
            data={
                "chat_id": CHANNEL,
                "question": question,
                "options": json.dumps(list(options), ensure_ascii=False),
                "is_anonymous": "true",
                "allows_multiple_answers": "false",
            })
        ok = r.ok and r.json().get("ok")
        if not ok:
            print("Telegram poll error:", r.status_code, r.text[:300], file=sys.stderr)
        else:
            print("posted poll", r.json()["result"]["message_id"])
        time.sleep(1.5)
        return bool(ok)
    except Exception as e:
        print("Telegram poll error:", e, file=sys.stderr)
        return False


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


def get_moex_quote(secid):
    """LAST и изменение за день, %. (last, pct) или (None, None)."""
    try:
        url = (f"https://iss.moex.com/iss/engines/currency/markets/selt/securities/{secid}.json"
               "?iss.meta=off&iss.only=marketdata&marketdata.columns=LAST,CHANGE")
        md = requests.get(url, timeout=20, headers=UA).json()["marketdata"]
        cols = {name: i for i, name in enumerate(md.get("columns") or [])}
        if "LAST" not in cols:
            return None, None
        for row in md.get("data") or []:
            last = row[cols["LAST"]]
            if last in (None, ""):
                continue
            last = float(last)
            pct = None
            if "CHANGE" in cols and row[cols["CHANGE"]] not in (None, ""):
                change = float(row[cols["CHANGE"]])
                base = last - change
                if base:
                    pct = change / base * 100
            return last, pct
        return None, None
    except Exception as e:
        print("MOEX error:", e, file=sys.stderr)
        return None, None


def get_moex(secid):
    """Биржевой курс Мосбиржи (бесплатно, с задержкой ~15 мин)."""
    last, _pct = get_moex_quote(secid)
    return last


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


def _brent_bits(price, prev):
    """Цена Brent и изменение за день. Пустая строка, если цены нет."""
    if price is None:
        return ""
    try:
        text = fmt(float(price), 1)
    except (TypeError, ValueError):
        return ""
    diff = _diff(price, prev)
    icon = trend_icon(diff, 1)
    if icon and diff is not None:
        text += f" {icon}{fmt(abs(float(diff)), 1)}"
    return "🛢 " + text


def brent_digest_line(price, prev):
    """Строка Brent для итогов дня. Пусто, если котировки нет."""
    if price is None:
        return ""
    try:
        text = f"🛢 Brent {fmt(float(price), 1)} $"
    except (TypeError, ValueError):
        return ""
    diff = _diff(price, prev)
    icon = trend_icon(diff, 1)
    if icon and diff is not None:
        text += f" {icon}{fmt(abs(float(diff)), 1)}"
    return text


SHARP_PCT = 1.0
SHARP_COOLDOWN = 3 * 3600


def sharp_move_text(name, pct, price):
    icon = "📈" if pct > 0 else "📉"
    return f"⚡️ Резкое движение: {name} {icon} {signed(pct, 1)}% за час ({fmt(price)} ₽)"


def _sample_near(samples, now, target=3600):
    """Замер примерно час назад: от 45 до 90 минут, ближайший к часу."""
    best = None
    best_err = None
    for s in samples or []:
        if not isinstance(s, dict):
            continue
        try:
            age = now - float(s.get("ts"))
        except (TypeError, ValueError):
            continue
        if not (45 * 60 <= age <= 90 * 60):
            continue
        err = abs(age - target)
        if best is None or err < best_err:
            best, best_err = s, err
    return best


def _sharp_pair(usd, cny, old):
    """USD/RUB, а если доллара нет — CNY/RUB. None, если сравнить не с чем."""
    if usd is not None and old.get("usd") not in (None, ""):
        try:
            prev = float(old["usd"])
            if prev:
                return "доллар", (float(usd) - prev) / prev * 100, float(usd)
        except (TypeError, ValueError):
            pass
    if usd is None and cny is not None and old.get("cny") not in (None, ""):
        try:
            prev = float(old["cny"])
            if prev:
                return "юань", (float(cny) - prev) / prev * 100, float(cny)
        except (TypeError, ValueError):
            pass
    return None


def check_sharp_move(st, usd, cny, now=None):
    """>=1% к замеру ~60 минут назад. Кулдаун 3 часа. None — поста нет."""
    try:
        now = time.time() if now is None else float(now)
        samples = [s for s in (st.get("rate_samples") or []) if isinstance(s, dict)]
        samples.append({"ts": now, "usd": usd, "cny": cny})
        samples = [s for s in samples if now - float(s.get("ts") or now) <= 6 * 3600]
        st["rate_samples"] = samples[-48:]
        last_alert = st.get("sharp_ts")
        if last_alert is not None and now - float(last_alert) < SHARP_COOLDOWN:
            return None
        old = _sample_near(samples, now)
        if not old:
            return None
        pair = _sharp_pair(usd, cny, old)
        if not pair:
            return None
        name, pct, price = pair
        if abs(pct) < SHARP_PCT:
            return None
        return sharp_move_text(name, pct, price)
    except Exception as e:
        print("sharp error:", e, file=sys.stderr)
        return None


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
    brent_bit = _brent_bits(brent, brent_prev)
    if brent_bit:
        parts.append(brent_bit)
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
TOPICS = ("cbr", "oil", "yuan", "sanctions", "fx", "economy", "other")
TOPIC_EMOJI = {
    "cbr": "🏦", "oil": "🛢", "yuan": "🇨🇳", "sanctions": "⚖️",
    "fx": "💵", "economy": "📊", "other": "📰",
}
AI_SYSTEM = (
    "Ты живой аналитик курса в Telegram, не шаблонный бот. Пиши по-русски, "
    "простым языком для торговца электроникой, который закупает технику за рубежом. "
    "Каждый раз меняй слова и строй фразы иначе, не повторяй одни и те же обороты. "
    "Ответь одним JSON без markdown. Ключи: "
    "relevant (bool) — true, только если новость про рубль, доллар, ЦБ, нефть, санкции, "
    "экономику России или импорт, иначе false; "
    "urgency — urgent или normal; urgent только для решения ЦБ, новых санкций или резкого "
    "движения валюты, иначе normal; "
    "topic — одно из cbr, oil, yuan, sanctions, fx, economy, other; "
    "headline — короткий цепкий заголовок по-русски, не длиннее 60 символов, без кликбейта и без капса; "
    "summary — 2–3 предложения сути, без повтора заголовка; "
    "impact — up, down или neutral для курса USD/RUB "
    "(up — доллар дорожает, рубль слабеет; down — доллар дешевеет, рубль крепнет; neutral — явного влияния нет); "
    "analysis — 1–2 предложения, почему так для закупки техники. "
    "Если relevant=false, остальные строки пустые, impact=neutral, urgency=normal, topic=other. "
    "Не выдумывай факты. Без HTML, списков и ссылок."
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
    room = max(limit - 1, 1)
    cut = text[:room]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    cut = (cut or text[:room]).rstrip()
    if len(cut) > room:
        cut = cut[:room].rstrip()
    return cut + "…"


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
        return {"relevant": False, "summary": "", "impact": "neutral", "analysis": "",
                "urgency": "normal", "topic": "other", "headline": ""}
    summary = _plain(data.get("summary"), 800)
    if not summary:
        return None
    impact = str(data.get("impact") or "").strip().lower()
    if impact not in IMPACT_LABEL:
        impact = "neutral"
    urgency = str(data.get("urgency") or "").strip().lower()
    if urgency not in ("urgent", "normal"):
        urgency = "normal"
    topic = str(data.get("topic") or "").strip().lower()
    if topic not in TOPICS:
        topic = "other"
    return {"relevant": True, "summary": summary, "impact": impact,
            "analysis": _plain(data.get("analysis"), 500), "urgency": urgency,
            "topic": topic, "headline": _plain(data.get("headline"), 60)}


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

    def analyze(self, title, body, hint=""):
        if self.stop or self.used >= self.limit:
            return None
        token = os.environ.get("GITHUB_TOKEN")
        if not token:
            self.stop = True
            print("AI skipped: no GITHUB_TOKEN", file=sys.stderr)
            return None
        self.used += 1
        extra = (hint or "").strip()
        if not extra and getattr(self, "meeting_hint", "") and _RATE_NEWS.search(title or ""):
            extra = self.meeting_hint
        user = f"Заголовок: {title}\n\nТекст новости:\n{(body or '')[:4000]}"
        if extra:
            user = extra + "\n\n" + user
        messages = [
            {"role": "system", "content": AI_SYSTEM},
            {"role": "user", "content": user},
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

    def ask(self, system, user, temperature=0.3):
        """Короткий JSON для календаря и прогнозов. None — модель недоступна."""
        if self.stop or self.used >= self.limit:
            return None
        token = os.environ.get("GITHUB_TOKEN")
        if not token:
            self.stop = True
            print("AI skipped: no GITHUB_TOKEN", file=sys.stderr)
            return None
        self.used += 1
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": (user or "")[:6000]},
        ]
        try:
            content = self._complete(token, messages, json_mode=True, temperature=temperature)
            if content is None and not self.stop:
                content = self._complete(token, messages, json_mode=False, temperature=temperature)
            return content
        except Exception as e:
            print("AI error:", e, file=sys.stderr)
            return None

    def _complete(self, token, messages, json_mode, temperature=0.7):
        payload = {"model": AI_MODEL, "messages": messages, "temperature": temperature, "max_tokens": 700}
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


def impact_phrase(ai):
    label = IMPACT_LABEL.get((ai or {}).get("impact"), IMPACT_LABEL["neutral"])
    analysis = _plain((ai or {}).get("analysis"), 500)
    if analysis:
        return f"→ {label}: {html.escape(analysis)}"
    return f"→ {label}"


def ai_headline(ai, it):
    for raw in ((ai or {}).get("headline"), (it or {}).get("title"), (ai or {}).get("summary")):
        text = _plain(raw, 60)
        if text:
            return text
    return ""


def news_text(it, ctx=None, ai=None, variant=0):
    """Обычная новость без строки котировок. Сбой модели — короткий отрывок."""
    it = it or {}
    try:
        foot = news_footer(it)
    except Exception:
        foot = "#рынки"
    blocks = []
    try:
        if isinstance(ai, dict) and ai.get("relevant") and (ai.get("summary") or "").strip():
            summary = html.escape(_plain(ai["summary"], 800))
            headline = html.escape(ai_headline(ai, it))
            phrase = impact_phrase(ai)
            if ai.get("urgency") == "urgent":
                blocks = [f"🚨 <b>СРОЧНО: {headline}</b>", summary, phrase]
            else:
                emoji = TOPIC_EMOJI.get(ai.get("topic"), TOPIC_EMOJI["other"])
                head = f"{emoji} <b>{headline}</b>"
                mode = int(variant or 0) % 3
                if mode == 1:
                    blocks = [head, phrase, summary]
                elif mode == 2:
                    blocks = [head, f"{summary} {phrase}"]
                else:
                    blocks = [head, summary, phrase]
        else:
            raw = it.get("body") or it.get("summary") or ""
            text = excerpt(raw, 300)
            if text:
                blocks = [f"📰 {html.escape(text)}"]
    except Exception as e:
        print("format error:", e, file=sys.stderr)
        blocks = []
    if not blocks:
        return ""
    return "\n\n".join([*blocks, foot])


def _model_used(analyze):
    owner = getattr(analyze, "__self__", None)
    used = getattr(owner, "used", None)
    return used if isinstance(used, int) else None


def select_news_posts(news, ctx, analyze, get=None, seen_ids=None, ai_seen=None, layout_i=None):
    """До MAX_NEWS постов. Модель вызывается только для id, которых ещё не было."""
    chosen, posts, skipped = [], [], []
    seen = set(seen_ids or [])
    ai_list = ai_seen if isinstance(ai_seen, list) else None
    ai_set = set(ai_list or [])
    if not isinstance(layout_i, list) or not layout_i:
        layout_i = [0]
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
        normal = bool(use_ai and ai.get("urgency") != "urgent")
        try:
            variant = int(layout_i[0]) % 3 if normal else 0
            text = news_text(it, ctx, ai if use_ai else None, variant)
        except Exception as e:
            print("format error:", e, file=sys.stderr)
            text = ""
        if not text:
            skipped.append(it)
            continue
        if normal:
            layout_i[0] = int(layout_i[0]) + 1
        posts.append((it, text, ai if use_ai else None))
    return posts, skipped


def remember_day_news(st, it, ai, now):
    """Заголовки, которые уже ушли в канал, для вечернего итога."""
    day = now.strftime("%Y-%m-%d")
    items = [x for x in (st.get("day_news") or []) if isinstance(x, dict) and x.get("date") == day]
    if isinstance(ai, dict) and ai.get("relevant"):
        headline = _plain(ai.get("headline") or it.get("title") or "", 60)
        impact = ai.get("impact") if ai.get("impact") in IMPACT_LABEL else "neutral"
        urgent = ai.get("urgency") == "urgent"
    else:
        headline = _plain(it.get("title") or "", 60)
        impact, urgent = "neutral", False
    if not headline:
        return
    items.append({"date": day, "headline": headline, "impact": impact,
                  "urgent": urgent, "ts": time.time()})
    st["day_news"] = items[-30:]


def top_day_headlines(items, now, n=3):
    day = now.strftime("%Y-%m-%d")
    rows = [x for x in (items or []) if isinstance(x, dict) and x.get("date") == day and x.get("headline")]
    rows.sort(key=lambda x: (not x.get("urgent"), -float(x.get("ts") or 0)))
    picked = rows[:n]
    picked.sort(key=lambda x: float(x.get("ts") or 0))
    return picked


def digest_due(st, now):
    """Первый запуск в 20:00 МСК и позже (17:00 UTC), один раз за день."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=MSK)
    now = now.astimezone(MSK)
    now_utc = now.astimezone(timezone.utc)
    if now_utc.hour < 17 or now.hour < 20:
        return False
    return st.get("digest_date") != now.strftime("%Y-%m-%d")


def evening_digest(quotes, headlines, extras=None):
    lines = ["🌙 <b>Итоги дня</b>"]
    qlines = []
    for name, emoji, last, pct in quotes or []:
        if last is None:
            continue
        try:
            bit = f"{emoji} {name} {fmt(float(last))} ₽"
        except (TypeError, ValueError):
            continue
        if pct is not None:
            try:
                icon = trend_icon(float(pct), 1)
                pct_s = signed(float(pct), 1) + "%"
                bit += f" {icon} {pct_s}" if icon else f" {pct_s}"
            except (TypeError, ValueError):
                pass
        qlines.append(bit)
    for extra in extras or []:
        if extra:
            qlines.append(extra)
    if qlines:
        lines.append("")
        lines.extend(qlines)
    hlines = []
    for h in headlines or []:
        title = _plain(h.get("headline"), 60)
        if not title:
            continue
        em = {"up": "🔴", "down": "🟢", "neutral": "⚪️"}.get(h.get("impact"), "⚪️")
        hlines.append(f"{em} {html.escape(title)}")
    if hlines:
        lines.append("")
        lines.extend(hlines)
    if not qlines and not hlines:
        return ""
    return "\n".join(lines)


def mark_seen(st, posted, seen_ids, it):
    uid = it.get("id")
    if not uid:
        return
    if uid not in seen_ids:
        posted.append(uid)
        seen_ids.add(uid)
    remember_tg(st, it)


_RATE_NEWS = re.compile(r"ставк|ключев|\bцб\b|банк\s+россии", re.I)
_FORECAST_WORD = re.compile(r"прогноз", re.I)


def as_msk(now):
    if now.tzinfo is None:
        return now.replace(tzinfo=MSK)
    return now.astimezone(MSK)


def last_weekly_slot(now, weekday, hour, minute=0):
    """Последний уже наступивший слот: weekday пн=0 … вс=6, час МСК."""
    now = as_msk(now)
    delta = (now.weekday() - weekday) % 7
    day = now.date() - timedelta(days=delta)
    slot = datetime(day.year, day.month, day.day, hour, minute, tzinfo=MSK)
    if now < slot:
        slot -= timedelta(days=7)
    return slot


def slot_open(st, key, slot, now):
    """Слот ещё не выходил и сейчас не позже конца следующего дня."""
    now = as_msk(now)
    slot = as_msk(slot)
    if now < slot:
        return False
    end = datetime(slot.year, slot.month, slot.day, 23, 59, 59, tzinfo=MSK) + timedelta(days=1)
    if now > end:
        return False
    return st.get(key) != slot.strftime("%Y-%m-%d")


def _json_obj(content):
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
    return data if isinstance(data, dict) else None


def _nums(text):
    return set(re.findall(r"\d+(?:[.,]\d+)?", text or ""))


def _grounded(line, source):
    """Все числа строки встречаются в источнике. Иначе модель их выдумала."""
    src = set()
    for n in _nums(source):
        src.add(n)
        src.add(n.replace(",", "."))
        src.add(n.replace(".", ","))
    for n in _nums(line):
        if n not in src and n.replace(",", ".") not in src and n.replace(".", ",") not in src:
            return False
    return True


def _parse_xml_daily(content):
    if not content:
        return None
    root = ET.fromstring(content)
    raw = root.attrib.get("Date") or ""
    parts = raw.split(".")
    if len(parts) != 3:
        return None
    dd, mm, yy = parts
    iso = f"{yy}-{mm}-{dd}"
    out = {}
    for val in root.iter("Valute"):
        code = nom = value = None
        for child in val:
            name = child.tag.rsplit("}", 1)[-1]
            if name == "CharCode":
                code = (child.text or "").strip()
            elif name == "Nominal" and child.text:
                try:
                    nom = float(child.text.replace(",", ".")) or 1
                except ValueError:
                    nom = 1
            elif name == "Value" and child.text:
                try:
                    value = float(child.text.replace(",", "."))
                except ValueError:
                    value = None
        if code in ("USD", "CNY") and value is not None:
            out[code] = value / (nom or 1)
    return iso, out


def official_tomorrow(cbr, today, get=None):
    """Курс ЦБ с датой позже сегодня. None, если на завтра ещё не вышел."""
    try:
        today_s = today.isoformat()
    except AttributeError:
        today_s = str(today)[:10]
    if isinstance(cbr, dict) and str(cbr.get("date") or "")[:10] > today_s:
        return {"date": str(cbr["date"])[:10], "usd": cbr.get("usd"), "usd_prev": cbr.get("usd_prev"),
                "cny": cbr.get("cny"), "cny_prev": cbr.get("cny_prev")}
    get = get or requests.get
    try:
        r = get("https://www.cbr.ru/scripts/XML_daily.asp", timeout=20, headers=UA)
        parsed = _parse_xml_daily(getattr(r, "content", b"") or b"")
        if not parsed or parsed[0] <= today_s:
            return None
        iso, cur = parsed
        prev = {}
        try:
            day = today if isinstance(today, date) else date.fromisoformat(today_s)
            url = "https://www.cbr.ru/scripts/XML_daily.asp?date_req=" + day.strftime("%d/%m/%Y")
            r2 = get(url, timeout=20, headers=UA)
            parsed2 = _parse_xml_daily(getattr(r2, "content", b"") or b"")
            if parsed2 and parsed2[0] <= today_s:
                prev = parsed2[1]
        except Exception:
            prev = {}
        return {"date": iso, "usd": cur.get("USD"), "usd_prev": prev.get("USD"),
                "cny": cur.get("CNY"), "cny_prev": prev.get("CNY")}
    except Exception as e:
        print("CBR tomorrow error:", e, file=sys.stderr)
        return None


def tomorrow_due(st, now, rates):
    now = as_msk(now)
    if (now.hour, now.minute) < (15, 30):
        return False
    if not isinstance(rates, dict) or not rates.get("date"):
        return False
    if str(rates["date"])[:10] <= now.date().isoformat():
        return False
    return st.get("cbr_next") != str(rates["date"])[:10]


def _chip(symbol, value, prev):
    if value is None:
        return ""
    try:
        text = f"{symbol} {fmt(float(value))}"
    except (TypeError, ValueError):
        return ""
    diff = _diff(value, prev)
    icon = trend_icon(diff, 2)
    if icon and diff is not None:
        text += f" {icon}{fmt(abs(float(diff)), 2)}"
    return text


def cbr_tomorrow_text(rates):
    if not isinstance(rates, dict) or not rates.get("date"):
        return ""
    try:
        dt = datetime.fromisoformat(str(rates["date"])[:10])
        head = f"📊 <b>Курс ЦБ на завтра</b> · {dt.day:02d}.{dt.month:02d}"
    except ValueError:
        head = "📊 <b>Курс ЦБ на завтра</b>"
    bits = [b for b in (_chip("$", rates.get("usd"), rates.get("usd_prev")),
                        _chip("¥", rates.get("cny"), rates.get("cny_prev"))) if b]
    if not bits:
        return ""
    return head + "\n" + " · ".join(bits)


def _meeting_on(day):
    iso = day.isoformat()
    for when, anchor in CBR_MEETINGS_2026:
        if when == iso:
            return when, anchor
    return None, False


def reminder_due(st, now):
    """День накануне заседания, с 09:00 МСК, один раз."""
    now = as_msk(now)
    if (now.hour, now.minute) < (9, 0):
        return None, False
    when, anchor = _meeting_on(now.date() + timedelta(days=1))
    if when and st.get("rate_remind") != when:
        return when, anchor
    return None, False


def meeting_live(now):
    """День заседания после 13:30 МСК — новости про ставку идут в модель первыми."""
    now = as_msk(now)
    if (now.hour, now.minute) < (13, 30):
        return None, False
    return _meeting_on(now.date())


def known_key_rate(live, st):
    for raw in (live, (st or {}).get("key_rate"), KEY_RATE_KNOWN):
        if raw is None:
            continue
        try:
            return round(float(raw), 2)
        except (TypeError, ValueError):
            continue
    return KEY_RATE_KNOWN


def meeting_reminder_text(when, rate, anchor):
    dt = date.fromisoformat(when)
    lines = [
        "🏦 <b>Завтра ставка ЦБ</b>",
        f"{dt.day:02d}.{dt.month:02d} · сейчас {html.escape(fmt_pct(rate))}",
        "Пресс-релиз в 13:30 МСК",
    ]
    if anchor:
        lines.append("Опорное заседание — выйдет прогноз")
    return "\n".join(lines)


def _tax_day(year, month):
    """Крупные налоги около 28-го. С выходного срок сдвигается на будний."""
    d = date(year, month, 28)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def week_events(monday):
    """Известные события недели. Список жёсткий, модель ничего не дописывает."""
    if isinstance(monday, datetime):
        monday = monday.date()
    days = [monday + timedelta(days=i) for i in range(7)]
    have = set(days)
    events = []
    for d in days:
        if d.weekday() == 2:
            events.append(f"{d.day:02d}.{d.month:02d} ср — недельная инфляция Росстата")
    seen_tax = set()
    for d in days:
        tax = _tax_day(d.year, d.month)
        if tax in have and tax not in seen_tax:
            seen_tax.add(tax)
            events.append(f"{tax.day:02d}.{tax.month:02d} — налоги около 28-го, спрос на рубли")
    for when, anchor in CBR_MEETINGS_2026:
        d = date.fromisoformat(when)
        if d in have:
            tail = ", опорное: выйдет прогноз" if anchor else ""
            events.append(f"{d.day:02d}.{d.month:02d} — заседание ЦБ по ставке{tail}, релиз в 13:30")
    for when in FOMC_2026:
        d = date.fromisoformat(when)
        if d in have:
            events.append(f"{d.day:02d}.{d.month:02d} — решение FOMC")
    return events


CALENDAR_SYSTEM = (
    "Ты ведёшь короткий телеграм-канал про рубль. По списку событий напиши 3–6 строк по-русски, "
    "стилем трейдера, без кликбейта. Ничего не добавляй: ни дат, ни цифр, ни событий вне списка. "
    "Ответ — один JSON {\"lines\": [\"...\"]}. Без HTML."
)


def _ai_lines(raw, source, limit):
    data = _json_obj(raw) or {}
    lines = []
    for line in data.get("lines") or []:
        text = _plain(line, 180)
        if text and _grounded(text, source) and text not in lines:
            lines.append(text)
        if len(lines) >= limit:
            break
    return lines


def calendar_post(events, headlines, ask=None):
    events = [e for e in (events or []) if e]
    if not events:
        events = ["На этой неделе нет заседаний ЦБ и FOMC"]
    facts = "\n".join(f"- {e}" for e in events)
    lines = []
    if ask:
        try:
            news = "\n".join(f"- {h}" for h in (headlines or []) if h)[:800]
            user = "События:\n" + facts
            if news:
                user += "\n\nФон ленты, не источник новых дат:\n" + news
            lines = _ai_lines(ask(CALENDAR_SYSTEM, user), facts, 6)
        except Exception as e:
            print("calendar AI error:", e, file=sys.stderr)
            lines = []
    if not lines:
        lines = events[:6]
    body = "\n".join(f"• {html.escape(x)}" for x in lines)
    return "📅 <b>Неделя рубля</b>\n\n" + body


FORECAST_SYSTEM = (
    "Собери прогнозы курса доллара и юаня только из переданных источников. "
    "Коротко, по-русски, до 5 строк. Каждая строка — чей прогноз и какая цифра, если цифра есть в тексте. "
    "Запрещено придумывать числа, диапазоны и имена. Нет прогноза — {\"lines\": []}. "
    "Ответ — один JSON {\"lines\": [\"...\"]}. Без HTML."
)


def remember_sources(st, news):
    """Копим куски с словом «прогноз», чтобы пятничный пост опирался на неделю."""
    items = [x for x in (st.get("recent_news") or []) if isinstance(x, dict)]
    seen = {x.get("title") or "" for x in items}
    for it in news or []:
        if not isinstance(it, dict):
            continue
        blob = " ".join(str(it.get(k) or "") for k in ("title", "summary", "body"))
        if not _FORECAST_WORD.search(blob):
            continue
        title = _plain(it.get("title"), 160)
        if not title or title in seen:
            continue
        items.append({"title": title, "text": _plain(blob, 600), "ts": time.time()})
        seen.add(title)
    st["recent_news"] = items[-40:]


def forecast_sources(st, now, days=8):
    now_ts = time.time() if now is None else None
    try:
        if isinstance(now, datetime):
            now_ts = now.timestamp()
    except (OSError, ValueError):
        now_ts = time.time()
    if now_ts is None:
        now_ts = time.time()
    cutoff = now_ts - days * 86400
    rows = []
    for x in st.get("recent_news") or []:
        if not isinstance(x, dict):
            continue
        try:
            ts = float(x.get("ts") or 0)
        except (TypeError, ValueError):
            ts = 0
        blob = f"{x.get('title') or ''} {x.get('text') or ''}"
        if ts >= cutoff and _FORECAST_WORD.search(blob):
            rows.append(x)
    return rows[-12:]


def forecast_post(sources, ask=None):
    sources = list(sources or [])
    if not sources:
        return ""
    blob = "\n".join(f"- {s.get('title') or ''}. {s.get('text') or ''}" for s in sources)[:4000]
    lines = []
    if ask:
        try:
            lines = _ai_lines(ask(FORECAST_SYSTEM, blob), blob, 5)
        except Exception as e:
            print("forecast AI error:", e, file=sys.stderr)
            lines = []
    if not lines:
        for s in sources:
            line = _plain(s.get("title") or "", 180)
            if line and line not in lines:
                lines.append(line)
            if len(lines) >= 5:
                break
    if not lines:
        return ""
    body = "\n".join(f"• {html.escape(x)}" for x in lines)
    return "🔮 <b>Прогнозы</b>\n\n" + body


def parse_cbr_dynamic(content):
    """XML_dynamic.asp → список (YYYY-MM-DD, курс за 1 единицу)."""
    if not content:
        return []
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return []
    out = []
    for rec in root.iter("Record"):
        raw = rec.attrib.get("Date") or ""
        parts = raw.split(".")
        if len(parts) != 3:
            continue
        dd, mm, yy = parts
        nominal, value = 1.0, None
        for child in rec:
            name = child.tag.rsplit("}", 1)[-1]
            if name == "Nominal" and child.text:
                try:
                    nominal = float(child.text.replace(",", ".")) or 1
                except ValueError:
                    nominal = 1
            elif name == "Value" and child.text:
                try:
                    value = float(child.text.replace(",", "."))
                except ValueError:
                    value = None
        if value is None:
            continue
        out.append((f"{yy}-{mm}-{dd}", value / nominal))
    return out


def fetch_cbr_history(end, get=None, days=30):
    """Официальные USD и CNY за последние days дней. Пусто при любой ошибке."""
    get = get or requests.get
    try:
        end_d = end if isinstance(end, date) else end.date()
    except AttributeError:
        return []
    start = end_d - timedelta(days=days)

    def load(code):
        url = ("https://www.cbr.ru/scripts/XML_dynamic.asp"
               f"?date_req1={start.strftime('%d/%m/%Y')}"
               f"&date_req2={end_d.strftime('%d/%m/%Y')}&VAL_NM_RQ={code}")
        r = get(url, timeout=20, headers=UA)
        if getattr(r, "status_code", 200) >= 400:
            return []
        return parse_cbr_dynamic(getattr(r, "content", b"") or b"")

    try:
        usd, cny = load("R01235"), load("R01375")
    except Exception as e:
        print("history error:", e, file=sys.stderr)
        return []
    by = {}
    for d, v in usd:
        by.setdefault(d, {})["usd"] = v
    for d, v in cny:
        by.setdefault(d, {})["cny"] = v
    rows = [{"date": d, **by[d]} for d in sorted(by) if by[d].get("usd") is not None]
    return rows[-30:]


def chart_caption(rows):
    if not rows:
        return ""
    first, last = rows[0], rows[-1]

    def bit(symbol, key):
        series = [r for r in rows if r.get(key) is not None]
        if not series:
            return ""
        try:
            text = f"{symbol} {fmt(float(series[-1][key]))}"
            if len(series) < 2:
                return text
            diff = float(series[-1][key]) - float(series[0][key])
        except (TypeError, ValueError):
            return ""
        icon = trend_icon(diff, 2)
        if icon:
            text += f" {icon}{fmt(abs(diff))}"
        return text

    try:
        d0 = datetime.fromisoformat(first["date"]).strftime("%d.%m")
        d1 = datetime.fromisoformat(last["date"]).strftime("%d.%m")
        head = f"📈 <b>Курс ЦБ</b> · {d0}–{d1}"
    except (TypeError, ValueError):
        head = "📈 <b>Курс ЦБ</b>"
    parts = [p for p in (bit("$", "usd"), bit("¥", "cny")) if p]
    if not parts:
        return ""
    return head + "\n" + " · ".join(parts)


def render_rate_chart(rows):
    """PNG USD/RUB и CNY/RUB. None, если точек мало или matplotlib недоступен."""
    rows = [r for r in (rows or []) if r.get("usd") is not None]
    if len(rows) < 7:
        return None
    try:
        os.environ.setdefault("MPLCONFIGDIR", "/tmp/matplotlib")
        os.makedirs(os.environ["MPLCONFIGDIR"], exist_ok=True)
        import matplotlib
        matplotlib.use("Agg", force=True)
        import matplotlib.pyplot as plt
        from matplotlib.dates import DateFormatter
    except Exception as e:
        print("chart import error:", e, file=sys.stderr)
        return None
    try:
        dates = [datetime.fromisoformat(r["date"]) for r in rows]
        usd = [float(r["usd"]) for r in rows]
        cny = [float(r["cny"]) if r.get("cny") is not None else None for r in rows]
        fig, ax = plt.subplots(figsize=(8.2, 4.4), dpi=140)
        fig.patch.set_facecolor("#0f1419")
        ax.set_facecolor("#0f1419")
        ax.plot(dates, usd, color="#f5c542", lw=2.0, label="USD/RUB")
        ax.tick_params(colors="#d7dde3")
        ax.yaxis.label.set_color("#f5c542")
        ax.set_ylabel("USD/RUB", color="#f5c542")
        for spine in ax.spines.values():
            spine.set_color("#2c3640")
        ax.grid(True, color="#2c3640", lw=0.6)
        ax.xaxis.set_major_formatter(DateFormatter("%d.%m"))
        if any(v is not None for v in cny):
            ax2 = ax.twinx()
            xs = [d for d, v in zip(dates, cny) if v is not None]
            ys = [v for v in cny if v is not None]
            ax2.plot(xs, ys, color="#5ac8fa", lw=2.0, label="CNY/RUB")
            ax2.tick_params(colors="#5ac8fa")
            ax2.set_ylabel("CNY/RUB", color="#5ac8fa")
            for spine in ax2.spines.values():
                spine.set_color("#2c3640")
            h1, l1 = ax.get_legend_handles_labels()
            h2, l2 = ax2.get_legend_handles_labels()
            leg = ax.legend(h1 + h2, l1 + l2, facecolor="#0f1419", edgecolor="#2c3640", labelcolor="#d7dde3")
            leg.get_frame().set_alpha(0.9)
        else:
            leg = ax.legend(facecolor="#0f1419", edgecolor="#2c3640", labelcolor="#d7dde3")
            leg.get_frame().set_alpha(0.9)
        fig.autofmt_xdate(rotation=0, ha="center")
        fig.tight_layout()
        buf = io.BytesIO()
        fig.savefig(buf, format="png")
        plt.close(fig)
        return buf.getvalue()
    except Exception as e:
        print("chart error:", e, file=sys.stderr)
        return None


def main():
    if not TOKEN:
        sys.exit("Нет TELEGRAM_TOKEN")
    st = load_state()
    first_run = not st.get("posted")
    now = datetime.now(MSK)

    cbr = get_cbr()
    usd_mkt, usd_day = get_moex_quote("USD000UTSTOM")
    eur_mkt, eur_day = get_moex_quote("EUR_RUB__TOM")
    cny_mkt, cny_day = get_moex_quote("CNYRUB_TOM")
    key_rate = get_key_rate()
    brent, brent_prev = get_brent()

    # 1) резкое движение: ≥1% к замеру около часа назад, не чаще раза в 3 часа
    sharp = check_sharp_move(st, usd_mkt, cny_mkt)
    if sharp and tg_send(sharp):
        st["sharp_ts"] = time.time()

    # 2) утренняя сводка раз в день после 9:00 МСК
    today = now.strftime("%Y-%m-%d")
    if cbr and (FORCE_SUMMARY or (now.hour >= SUMMARY_HOUR and st.get("summary_date") != today)):
        if tg_send(morning_summary(cbr, key_rate, brent, brent_prev)):
            st["summary_date"] = today

    # изменение ключевой ставки — отдельным сообщением, один раз
    rate_msg = key_rate_update(st, key_rate)
    if rate_msg and tg_send(rate_msg):
        st["key_rate"] = round(float(key_rate), 2)

    # напоминание за день до заседания ЦБ
    try:
        when, anchor = reminder_due(st, now)
        if when:
            text = meeting_reminder_text(when, known_key_rate(key_rate, st), anchor)
            if text and tg_send(text):
                st["rate_remind"] = when
    except Exception as e:
        print("reminder error:", e, file=sys.stderr)

    # 3) новости: текст + разбор модели, не больше 5 постов и 8 вызовов
    news = collect_news(st, first_run)
    try:
        live, _anchor = meeting_live(now)
        if live:
            news.sort(key=lambda it: 0 if isinstance(it, dict) and _RATE_NEWS.search(
                f"{it.get('title') or ''} {it.get('summary') or ''}") else 1)
    except Exception as e:
        print("meeting sort error:", e, file=sys.stderr)
    posted = st.setdefault("posted", [])
    seen_ids = set(posted)
    ctx = quote_context(st, cbr, usd_mkt, brent, brent_prev, key_rate)
    client = ModelClient()
    try:
        if meeting_live(now)[0]:
            client.meeting_hint = MEETING_HINT
    except Exception:
        pass
    ai_seen = st.get("ai_seen")
    if not isinstance(ai_seen, list):
        ai_seen = []
        st["ai_seen"] = ai_seen
    layout = st.get("layout_i")
    layout_i = [layout if isinstance(layout, int) else 0]
    posts, skipped = select_news_posts(
        news, ctx, client.analyze, seen_ids=seen_ids, ai_seen=ai_seen, layout_i=layout_i)
    st["layout_i"] = int(layout_i[0])
    for it in skipped:
        mark_seen(st, posted, seen_ids, it)
    attempted = set()
    sent = 0
    for it, text, ai in posts:
        attempted.add(it.get("id"))
        if tg_send(text):
            mark_seen(st, posted, seen_ids, it)
            remember_day_news(st, it, ai, now)
            sent += 1
    if first_run:  # не заваливать канал при первом запуске: остальное помечаем как увиденное
        for it in news:
            if it.get("id") not in seen_ids and it.get("id") not in attempted:
                mark_seen(st, posted, seen_ids, it)
    try:
        remember_sources(st, news)
    except Exception as e:
        print("sources error:", e, file=sys.stderr)

    # курс ЦБ на завтра — после 15:30 МСК, один раз на дату курса
    try:
        rates = official_tomorrow(cbr, now.date())
        if tomorrow_due(st, now, rates):
            text = cbr_tomorrow_text(rates)
            if text and tg_send(text):
                st["cbr_next"] = str(rates["date"])[:10]
    except Exception as e:
        print("tomorrow error:", e, file=sys.stderr)

    try:
        if digest_due(st, now):
            digest = evening_digest(
                (("доллар", "💵", usd_mkt, usd_day),
                 ("евро", "💶", eur_mkt, eur_day),
                 ("юань", "🇨🇳", cny_mkt, cny_day)),
                top_day_headlines(st.get("day_news"), now),
                extras=[brent_digest_line(brent, brent_prev)])
            if digest and tg_send(digest):
                st["digest_date"] = now.strftime("%Y-%m-%d")
    except Exception as e:
        print("digest error:", e, file=sys.stderr)

    # недельные слоты: открыты до конца следующего дня, если этот запуск пропустил минуту
    try:
        cal_slot = last_weekly_slot(now, 0, 9)
        poll_slot = last_weekly_slot(now, 0, 10)
        fc_slot = last_weekly_slot(now, 4, 18)
        chart_slot = last_weekly_slot(now, 6, 19)
        need_ai = slot_open(st, "week_cal", cal_slot, now) or slot_open(st, "week_fc", fc_slot, now)
        sched = ModelClient(limit=2) if need_ai else None
        if slot_open(st, "week_cal", cal_slot, now):
            titles = [x.get("title") for x in (st.get("recent_news") or [])[-5:] if isinstance(x, dict)]
            text = calendar_post(week_events(cal_slot.date()), titles, sched.ask if sched else None)
            if text and tg_send(text):
                st["week_cal"] = cal_slot.strftime("%Y-%m-%d")
        if slot_open(st, "week_poll", poll_slot, now):
            if tg_poll(POLL_QUESTION, POLL_OPTIONS):
                st["week_poll"] = poll_slot.strftime("%Y-%m-%d")
        if slot_open(st, "week_fc", fc_slot, now):
            text = forecast_post(forecast_sources(st, now), sched.ask if sched else None)
            if text and tg_send(text):
                st["week_fc"] = fc_slot.strftime("%Y-%m-%d")
        if slot_open(st, "week_chart", chart_slot, now):
            rows = fetch_cbr_history(now.date())
            png = render_rate_chart(rows)
            caption = chart_caption(rows)
            if png and caption and tg_photo(png, caption):
                st["week_chart"] = chart_slot.strftime("%Y-%m-%d")
    except Exception as e:
        print("weekly error:", e, file=sys.stderr)
    print(f"news found: {len(news)}, posted {sent}, ai calls {client.used}")
    save_state(st)


if __name__ == "__main__":
    main()
