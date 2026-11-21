# -*- coding: utf-8 -*-
"""
وكيل خدمة عملاء ذكي لمتجر (إنستغرام + واتساب) — يعمل 24/7
- يفهم: نص، صوت، صور، فيديو، موقع، جهة اتصال، منشور/ريل مشارَك، رد على ستوري، ملصق
- دارجة جزائرية / فرنسية / عربية: يرد بلغة الزبون
- يسجّل الطلبات ويرسل تنبيهاً لصاحب المتجر على تيليغرام
- صندوق وارد دائم (SQLite): لا تضيع أي رسالة عند انقطاع أو إعادة تشغيل

الأوامر:  python shop_agent.py run | learn --dir chats | orders | resume <ukey>
"""
import os, re, sys, json, hmac, base64, hashlib, sqlite3, time, logging, threading, glob, argparse, unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo
import requests
from flask import Flask, request, abort, jsonify

E = os.getenv
ENV           = E("ENV", "dev")
VERIFY_TOKEN  = E("VERIFY_TOKEN", "change-me")
WA_TOKEN      = E("WA_ACCESS_TOKEN", "")
WA_PHONE_ID   = E("WA_PHONE_ID", "")
WA_SECRET     = E("WA_APP_SECRET", "")
IG_TOKEN      = E("IG_PAGE_ACCESS_TOKEN", "")
IG_SECRET     = E("IG_APP_SECRET", WA_SECRET)
ANTHROPIC_KEY = E("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL  = E("CLAUDE_MODEL", "claude-sonnet-5-5")
GEMINI_KEY    = E("GEMINI_API_KEY", "")
GEMINI_MODEL  = E("GEMINI_MODEL", "gemini-2.5-flash")
TG_TOKEN      = E("TELEGRAM_BOT_TOKEN", "")
TG_CHAT       = E("TELEGRAM_CHAT_ID", "")
DB_PATH       = E("DB_PATH", "shop.db")
SHOP_FILE     = E("SHOP_FILE", "shop.json")
CATALOG_FILE  = E("CATALOG_FILE", "catalog.json")
LEARNED_FILE  = E("LEARNED_FILE", "learned.txt")
DEBOUNCE      = float(E("DEBOUNCE_SECONDS", "8"))     # ينتظر العميل حتى يكمل رسائله
HUMAN_HOURS   = float(E("HUMAN_HOURS", "6"))          # مدة صمت البوت بعد تدخل بشري
WORKERS       = int(E("WORKERS", "8"))
GRAPH         = "https://graph.facebook.com/v21.0"
TZ            = ZoneInfo("Africa/Algiers")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("shop")
app = Flask(__name__)
lock = threading.Lock()
START = time.time()

WILAYAS = ["Adrar","Chlef","Laghouat","Oum El Bouaghi","Batna","Béjaïa","Biskra","Béchar","Blida","Bouira","Tamanrasset",
 "Tébessa","Tlemcen","Tiaret","Tizi Ouzou","Alger","Djelfa","Jijel","Sétif","Saïda","Skikda","Sidi Bel Abbès","Annaba",
 "Guelma","Constantine","Médéa","Mostaganem","M'Sila","Mascara","Ouargla","Oran","El Bayadh","Illizi","Bordj Bou Arréridj",
 "Boumerdès","El Tarf","Tindouf","Tissemsilt","El Oued","Khenchela","Souk Ahras","Tipaza","Mila","Aïn Defla","Naâma",
 "Aïn Témouchent","Ghardaïa","Relizane","Timimoun","Bordj Badji Mokhtar","Ouled Djellal","Béni Abbès","In Salah",
 "In Guezzam","Touggourt","Djanet","El M'Ghair","El Menia"]

def jload(path, default=None):
    try: return json.load(open(path, encoding="utf-8"))
    except FileNotFoundError: return default if default is not None else {}

def read_file(path, default=""):
    try: return open(path, encoding="utf-8").read().strip()
    except FileNotFoundError: return default

def fold(t):
    t = unicodedata.normalize("NFKD", str(t).lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"[\u064B-\u065F\u0640]", "", t).translate(str.maketrans("أإآةى", "ااااي"))
    return re.sub(r"[^\w\s]", " ", t).strip()

# ---------- قاعدة البيانات (صندوق وارد دائم) ----------
def db():
    c = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30); c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL"); return c

with db() as c:
    c.executescript("""
    CREATE TABLE IF NOT EXISTS inbox(id INTEGER PRIMARY KEY AUTOINCREMENT, ukey TEXT, channel TEXT, uid TEXT, name TEXT,
        mid TEXT UNIQUE, kind TEXT, data TEXT, status TEXT DEFAULT 'pending', attempts INTEGER DEFAULT 0, ts REAL);
    CREATE TABLE IF NOT EXISTS convos(ukey TEXT PRIMARY KEY, channel TEXT, uid TEXT, name TEXT, human_until REAL DEFAULT 0,
        last_in REAL, last_out REAL, burst_n INTEGER DEFAULT 0, burst_ts REAL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS msgs(id INTEGER PRIMARY KEY AUTOINCREMENT, ukey TEXT, role TEXT, content TEXT, ts REAL);
    CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY AUTOINCREMENT, ukey TEXT, items TEXT, total INTEGER,
        customer TEXT, phone TEXT, wilaya TEXT, address TEXT, status TEXT DEFAULT 'new', created TEXT);
    CREATE TABLE IF NOT EXISTS sent(mid TEXT PRIMARY KEY, ts REAL);
    CREATE TABLE IF NOT EXISTS flags(id INTEGER PRIMARY KEY AUTOINCREMENT, ukey TEXT, draft TEXT, fixed TEXT, issue TEXT, ts REAL);
    UPDATE inbox SET status='pending' WHERE status='processing';  -- استعادة بعد انهيار/إعادة تشغيل
    """)

def q(sql, args=(), one=False, write=False):
    with lock, db() as c:
        cur = c.execute(sql, args)
        if write: return cur.rowcount
        return cur.fetchone() if one else cur.fetchall()

# ---------- التنبيهات لصاحب المتجر (تيليغرام) ----------
_cool = {}
def alert(text, key=None, cooldown=0):
    if key and time.time() - _cool.get(key, 0) < cooldown: return
    if key: _cool[key] = time.time()
    log.warning("ALERT: %s", text[:200])
    if TG_TOKEN and TG_CHAT:
        try: requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", timeout=10,
                           json={"chat_id": TG_CHAT, "text": "🔔 " + text})
        except requests.RequestException: log.exception("telegram failed")

# ---------- الإرسال ----------
def _post(url, **kw):
    for i in range(3):
        try:
            r = requests.post(url, timeout=20, **kw)
            if r.ok: return r
            body = r.text[:300]; log.warning("send %s: %s", r.status_code, body)
            if r.status_code == 401 or '"code":190' in body.replace(" ", ""):
                alert("⚠️ توكن ميتا منتهٍ أو غير صالح! جدّده فوراً وإلا يتوقف البوت.", "token", 1800); return None
            if r.status_code < 500 and r.status_code != 429: return None
        except requests.RequestException as e: log.warning("send error %s", e)
        time.sleep(2 ** i)
    return None

def send_text(channel, uid, text):
    if channel == "wa":
        r = _post(f"{GRAPH}/{WA_PHONE_ID}/messages", headers={"Authorization": f"Bearer {WA_TOKEN}"},
                  json={"messaging_product": "whatsapp", "to": uid, "type": "text", "text": {"body": text}})
        mid = r and r.json().get("messages", [{}])[0].get("id")
    else:
        r = _post(f"{GRAPH}/me/messages", params={"access_token": IG_TOKEN},
                  json={"recipient": {"id": uid}, "messaging_type": "RESPONSE", "message": {"text": text}})
        mid = r and r.json().get("message_id")
    if mid: q("INSERT OR IGNORE INTO sent VALUES(?,?)", (mid, time.time()), write=True)
    return bool(r)

def show_typing(channel, uid, last_mid):
    try:
        if channel == "wa":
            _post(f"{GRAPH}/{WA_PHONE_ID}/messages", headers={"Authorization": f"Bearer {WA_TOKEN}"},
                  json={"messaging_product": "whatsapp", "status": "read", "message_id": last_mid, "typing_indicator": {"type": "text"}})
        else:
            _post(f"{GRAPH}/me/messages", params={"access_token": IG_TOKEN},
                  json={"recipient": {"id": uid}, "sender_action": "typing_on"})
    except Exception: pass

def chunks(p, limit=900):
    out = []
    while len(p.encode()) > limit:
        cut = p[:limit // 2].rfind(" "); cut = cut if cut > 0 else limit // 2
        out.append(p[:cut]); p = p[cut:].strip()
    return out + [p]

def send_parts(ukey, text):
    """رسالة بعد رسالة مع مهلة قصيرة (القاعدة 3)"""
    ch, uid = ukey.split(":", 1)
    parts = [x for p in text.split("\n") if p.strip() for x in chunks(p.strip())][:5]
    for i, p in enumerate(parts):
        if i: time.sleep(min(3.0, 0.8 + len(p) / 40))
        send_text(ch, uid, p)
    q("UPDATE convos SET last_out=? WHERE ukey=?", (time.time(), ukey), write=True)

# ---------- Claude / Gemini ----------
def claude(messages, system, tools=None, max_tokens=500):
    body = {"model": CLAUDE_MODEL, "max_tokens": max_tokens, "system": system, "messages": messages}
    if tools: body["tools"] = tools
    last = None
    for i in range(3):
        try:
            r = requests.post("https://api.anthropic.com/v1/messages", timeout=60, json=body, headers={
                "x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"})
            if r.status_code in (429, 500, 502, 503, 529): last = r; time.sleep(2 ** i * 2); continue
            r.raise_for_status(); return r.json()
        except requests.RequestException as e: last = e; time.sleep(2 ** i * 2)
    raise RuntimeError(f"claude failed: {last}")

def text_of(resp): return "\n".join(b["text"] for b in resp["content"] if b["type"] == "text").strip()

def gemini(data, mime, prompt):
    r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent", timeout=120,
        headers={"x-goog-api-key": GEMINI_KEY, "Content-Type": "application/json"},
        json={"contents": [{"parts": [{"text": prompt}, {"inline_data": {"mime_type": mime, "data": base64.b64encode(data).decode()}}]}]})
    r.raise_for_status()
    return " ".join(p.get("text", "") for p in r.json()["candidates"][0]["content"]["parts"]).strip()

TRANSCRIBE = ("فرّغ هذا التسجيل حرفياً كما نُطق. قد يكون بالدارجة الجزائرية أو العربية أو الفرنسية أو مزيجاً منها؛ "
              "اكتب الكلمات كما سُمعت بلا ترجمة ولا شرح. إن لم يكن فيه كلام واضح فأعد: (غير واضح). أعد النص فقط.")
DESCRIBE = "صف هذا الفيديو في جملتين بالعربية، مع التركيز على أي منتج/ملابس تظهر فيه وألوانها. أعد الوصف فقط."

# ---------- الكتالوج والأدوات ----------
def catalog(): return jload(CATALOG_FILE, {"products": []})
def shop(): return jload(SHOP_FILE, {})

def find_products(query):
    qs = set(fold(query).split()); res = []
    for p in catalog().get("products", []):
        hay = fold(" ".join([p.get("name", ""), p.get("name_fr", ""), p.get("category", ""), " ".join(p.get("tags", []))]))
        score = sum(1 for w in qs if w in hay.split() or (len(w) > 3 and w in hay))
        if score: res.append((score, p))
    res.sort(key=lambda x: -x[0])
    if not res: return "لا نتائج مطابقة. اطلب من الزبون اسم المنتج أو صورة أوضح، أو حوّله لصاحب المتجر إن لم تجد."
    out = []
    for _, p in res[:5]:
        out.append({"id": p["id"], "name": p["name"], "name_fr": p.get("name_fr"), "price_DZD": p.get("price"),
                    "colors_sizes_in_stock": p.get("colors", {})})
    return json.dumps(out, ensure_ascii=False)

def match_wilaya(w):
    w = fold(w)
    if w.isdigit() and 1 <= int(w) <= len(WILAYAS): return WILAYAS[int(w) - 1]
    for x in WILAYAS:
        if fold(x) == w: return x
    for x in WILAYAS:
        if w and (w in fold(x) or fold(x) in w): return x
    return None

def delivery_fee(wilaya):
    w = match_wilaya(wilaya)
    if not w: return "ولاية غير معروفة؛ اطلب من الزبون كتابة اسم الولاية (بالفرنسية أو الرقم)."
    d = shop().get("delivery", {})
    fee = d.get("wilayas", {}).get(w, d.get("default"))
    return json.dumps({"wilaya": w, "fee_DZD": fee}, ensure_ascii=False) if fee is not None else "رسوم التوصيل غير محددة؛ حوّل لصاحب المتجر."

def create_order(ukey, items, customer_name, phone, wilaya, address):
    cat = {p["id"]: p for p in catalog().get("products", [])}
    lines, total = [], 0
    if not items: return "لا عناصر في الطلب."
    for it in items:
        p = cat.get(it.get("product_id"))
        if not p: return f"المنتج {it.get('product_id')} غير موجود في الكتالوج. استخدم find_products."
        color, size, qty = it.get("color"), str(it.get("size", "")).upper(), int(it.get("qty", 1) or 1)
        if color not in p.get("colors", {}): return f"اللون غير متوفر لـ {p['name']}. المتوفر: {list(p.get('colors', {}))}"
        if size not in [s.upper() for s in p["colors"][color]]: return f"المقاس {size} غير متوفر بلون {color}. المتوفر: {p['colors'][color]}"
        if not 1 <= qty <= 10: return "الكمية غير منطقية."
        if p.get("price") is None: return f"سعر {p['name']} غير محدد؛ استخدم request_human."
        total += p["price"] * qty; lines.append(f"{p['name']} - {color} - {size} × {qty}")
    ph = re.sub(r"[\s.\-]", "", phone or "")
    if not re.fullmatch(r"(?:\+213|00213|0)[567]\d{8}", ph): return "رقم الهاتف غير صحيح (يجب أن يبدأ بـ 05/06/07 ويكون 10 أرقام). اطلبه من الزبون."
    w = match_wilaya(wilaya)
    if not w: return "ولاية غير صحيحة."
    d = shop().get("delivery", {}); fee = d.get("wilayas", {}).get(w, d.get("default", 0)) or 0
    if not customer_name or not address: return "ينقص الاسم أو العنوان/مكتب التوصيل."
    grand = total + fee
    oid = None
    with lock, db() as c:
        cur = c.execute("INSERT INTO orders(ukey,items,total,customer,phone,wilaya,address,created) VALUES(?,?,?,?,?,?,?,?)",
                        (ukey, json.dumps(lines, ensure_ascii=False), grand, customer_name, ph, w, address, datetime.now(TZ).isoformat(timespec="seconds")))
        oid = cur.lastrowid
    alert(f"🛍️ طلب جديد #{oid}\n{customer_name} | {ph}\n{w}: {address}\n" + "\n".join(lines) + f"\nالمجموع مع التوصيل: {grand} دج (دفع عند الاستلام)")
    return json.dumps({"order_id": oid, "items_total": total, "delivery": fee, "grand_total_DZD": grand}, ensure_ascii=False)

def request_human(ukey, reason):
    q("UPDATE convos SET human_until=? WHERE ukey=?", (time.time() + HUMAN_HOURS * 3600, ukey), write=True)
    c = q("SELECT name FROM convos WHERE ukey=?", (ukey,), one=True)
    alert(f"👤 زبون يحتاجك: {c['name'] if c else ukey} ({ukey})\nالسبب: {reason}")
    return "تم التحويل. أخبر الزبون أن صاحب المتجر سيتواصل معه قريباً."

TOOLS = [
 {"name": "find_products", "description": "ابحث في الكتالوج عن منتج (بالاسم أو النوع بأي لغة). الأسعار والألوان والمقاسات المتوفرة تأتي من هنا فقط.",
  "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
 {"name": "delivery_fee", "description": "رسوم التوصيل لولاية (اسمها بالفرنسية أو رقمها).",
  "input_schema": {"type": "object", "properties": {"wilaya": {"type": "string"}}, "required": ["wilaya"]}},
 {"name": "create_order", "description": "سجّل الطلب بعد أن يؤكد الزبون الملخص صراحةً. لا تستدعها قبل ذلك.",
  "input_schema": {"type": "object", "properties": {
     "items": {"type": "array", "items": {"type": "object", "properties": {"product_id": {"type": "string"}, "color": {"type": "string"},
        "size": {"type": "string"}, "qty": {"type": "integer"}}, "required": ["product_id", "color", "size", "qty"]}},
     "customer_name": {"type": "string"}, "phone": {"type": "string"}, "wilaya": {"type": "string"},
     "address": {"type": "string", "description": "العنوان أو اسم مكتب التوصيل"}},
   "required": ["items", "customer_name", "phone", "wilaya", "address"]}},
 {"name": "request_human", "description": "حوّل المحادثة لصاحب المتجر (غضب، شكوى، سؤال خارج نطاقك، طلب تفاوض/جملة).",
  "input_schema": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]}},
]

def run_tool(ukey, name, a):
    try:
        if name == "find_products": return find_products(a["query"])
        if name == "delivery_fee": return delivery_fee(a["wilaya"])
        if name == "create_order": return create_order(ukey, a["items"], a["customer_name"], a["phone"], a["wilaya"], a["address"])
        if name == "request_human": return request_human(ukey, a["reason"])
    except Exception as e:
        log.exception("tool error"); return f"خطأ: {e}"
    return "أداة غير معروفة"

SYSTEM = """أنت مساعد خدمة العملاء والبيع لدى متجر «{shop}» ({desc}). ترد على الزبائن في إنستغرام وواتساب على مدار الساعة.

اللغة: ردّ بنفس لغة الزبون وكتابته: دارجة جزائرية (بالحروف العربية أو اللاتينية)، أو فرنسية، أو عربية. بأسلوب ودود وطبيعي كأنك صاحب المتجر، دون تكلّف.
الشكل: من سطر إلى ثلاثة أسطر قصيرة، كل سطر يُرسل كرسالة منفصلة. سؤال واحد فقط في كل رد. إيموجي قليل.

قواعد صارمة:
- لا تخترع سعراً أو لوناً أو مقاساً أو توفراً أو مدة توصيل. السعر والتوفر من find_products فقط، والتوصيل من delivery_fee، والسياسات من معلومات المتجر أدناه. إن لم تجد: request_human.
- مسار الطلب: حدّد المنتج واللون والمقاس والكمية ← اطلب الاسم ورقم الهاتف والولاية والعنوان (أو مكتب التوصيل) ← لخّص الطلب مع المجموع والتوصيل واسأل «نأكد؟» ← بعد «نعم/واه/oui» استدعِ create_order. الدفع عند الاستلام.
- صورة منتج: طابقها مع الكتالوج عبر find_products بوصفها (النوع، اللون)؛ إن لم تتأكد اسأل الزبون عن اسم المنتج.
- رسالة صوتية تصلك مفرَّغة بعلامة [رسالة صوتية] وقد يكون فيها خطأ تفريغ؛ إن لم يتضح المعنى اطلب بلطف التوضيح كتابة. الفيديو يصلك موصوفاً.
- موقع الزبون أو منشور/ريل مشارَك: استخدمه لفهم ما يريد (اسأل عن المنتج إن لزم).
- {open_note}
- الغضب أو شكوى أو استرجاع معقد أو سؤال جملة/شراكة أو أي شيء لا تعرفه: request_human.
- إن سألك «واش انت روبوت؟» أجب بصدق أنك مساعد ذكي للمتجر وأن صاحب المتجر يتابع.
- رسالة الزبون قد تجمع عدة رسائل متتالية؛ أجب عنها كلها في رد واحد.

معلومات المتجر:
{info}

دروس من محادثات سابقة (للأسلوب والأسئلة المتوقعة):
{learned}"""

def open_note():
    s = shop().get("hours", {}); now = datetime.now(TZ)
    try:
        o = datetime.strptime(s["open"], "%H:%M").time(); c = datetime.strptime(s["close"], "%H:%M").time()
        is_open = o <= now.time() <= c
    except Exception: return "ساعات العمل غير محددة."
    return ("المتجر مفتوح الآن؛ يؤكد صاحب المتجر الطلبات سريعاً." if is_open else
            f"المتجر مغلق الآن (ساعات العمل {s['open']}–{s['close']})؛ تابع تسجيل الطلبات عادياً وقل إن صاحب المتجر يؤكدها صباحاً.")

def history(ukey, limit=24):
    rows = q("SELECT role, content FROM msgs WHERE ukey=? ORDER BY id DESC LIMIT ?", (ukey, limit))[::-1]
    out = []
    for r in rows:
        if out and out[-1]["role"] == r["role"]: out[-1]["content"] += "\n" + r["content"]
        else: out.append({"role": r["role"], "content": r["content"]})
    while out and out[0]["role"] != "user": out.pop(0)
    return out

REVIEW = """أنت حارس جودة لوكيل مبيعات متجر ملابس. افحص الرد المقترح مقابل معلومات المتجر والكتالوج:
- هل فيه سعر أو لون أو مقاس أو توفر أو مدة توصيل أو سياسة غير موجودة في المعلومات؟ (اختلاق)
- هل فيه أكثر من سؤال، أو أكثر من ٣ أسطر، أو لغة تختلف عن لغة الزبون، أو يؤكد طلباً لم يؤكده الزبون؟
أعد JSON فقط: {"ok": true/false, "issue": "...", "fixed": "الرد المصحَّح كاملاً أو نفسه"}"""

def guard(ukey, draft, user_text):
    try:
        ctx = json.dumps({"shop": shop(), "catalog": catalog()}, ensure_ascii=False)[:12000]
        d = text_of(claude([{"role": "user", "content": f"المعلومات:\n{ctx}\n\nرسالة الزبون:\n{user_text}\n\nالرد المقترح:\n{draft}"}], REVIEW, max_tokens=600))
        m = re.search(r"\{.*\}", d, re.S); data = json.loads(m.group(0)) if m else {}
        if data.get("ok") is False and data.get("fixed"):
            q("INSERT INTO flags(ukey,draft,fixed,issue,ts) VALUES(?,?,?,?,?)", (ukey, draft, data["fixed"], data.get("issue", ""), time.time()), write=True)
            return data["fixed"].strip()
    except Exception: log.exception("guard error")
    return draft

def run_agent(ukey, text, images):
    q("INSERT INTO msgs(ukey,role,content,ts) VALUES(?,?,?,?)", (ukey, "user", text or "[صورة]", time.time()), write=True)
    s = shop()
    system = SYSTEM.format(shop=s.get("name", "المتجر"), desc=s.get("description", ""), open_note=open_note(),
                           info=json.dumps({k: v for k, v in s.items() if k != "delivery"}, ensure_ascii=False),
                           learned=read_file(LEARNED_FILE, "لا توجد بعد.")[:5000])
    msgs = history(ukey)
    if images and msgs and msgs[-1]["role"] == "user":
        msgs[-1]["content"] = [{"type": "image", "source": {"type": "base64", "media_type": mt, "data": b}} for b, mt in images] + \
                              [{"type": "text", "text": msgs[-1]["content"]}]
    for _ in range(6):
        resp = claude(msgs, system, tools=TOOLS)
        if resp["stop_reason"] != "tool_use": break
        msgs.append({"role": "assistant", "content": resp["content"]})
        msgs.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": b["id"], "content": run_tool(ukey, b["name"], b["input"])}
                                                  for b in resp["content"] if b["type"] == "tool_use"]})
    reply = text_of(resp)
    return guard(ukey, reply, text) if reply else ""

# ---------- معالجة أنواع الرسائل ----------
def fetch_media(it_channel, d):
    if it_channel == "wa":
        info = requests.get(f"{GRAPH}/{d['media_id']}", headers={"Authorization": f"Bearer {WA_TOKEN}"}, timeout=15); info.raise_for_status()
        j = info.json(); r = requests.get(j["url"], headers={"Authorization": f"Bearer {WA_TOKEN}"}, timeout=40)
        r.raise_for_status(); return r.content, (j.get("mime_type") or "").split(";")[0]
    r = requests.get(d["url"], timeout=40); r.raise_for_status()
    return r.content, (r.headers.get("Content-Type") or "").split(";")[0]

def render_item(it):
    k, d = it["kind"], json.loads(it["data"])
    if k == "text": return d["text"], None
    if k in ("audio", "image", "video"):
        try: data, mime = fetch_media(it["channel"], d)
        except Exception: log.exception("media fetch"); return f"[{k}] (تعذّر تحميله؛ اطلب إعادة الإرسال)", None
        cap = d.get("caption", "")
        if k == "audio":
            try: t = gemini(data, mime or "audio/ogg", TRANSCRIBE)
            except Exception: log.exception("transcribe"); t = ""
            return ("[رسالة صوتية] " + t) if t and t != "(غير واضح)" else "[رسالة صوتية غير واضحة؛ اطلب منه بلطف إعادة التسجيل أو الكتابة]", None
        if k == "image":
            if mime not in ("image/jpeg", "image/png", "image/webp", "image/gif") or len(data) > 5_000_000:
                return "[صورة] (تعذّرت قراءتها؛ اطلب إعادة إرسالها)", None
            return ("[صورة] " + cap).strip(), (base64.b64encode(data).decode(), mime)
        if len(data) > 15_000_000: return "[فيديو طويل] اطلب منه صورة للمنتج أو اسمه", None
        try: return "[فيديو] " + gemini(data, mime or "video/mp4", DESCRIBE) + (" " + cap if cap else ""), None
        except Exception: log.exception("video"); return "[فيديو] (تعذّر تحليله؛ اطلب صورة أو اسم المنتج)", None
    if k == "location":
        return f"[موقع الزبون] {d.get('name','')} {d.get('address','')} ({d.get('lat')},{d.get('lng')})".strip(), None
    if k == "contact": return f"[جهة اتصال] {d.get('text','')}", None
    if k == "share": return f"[الزبون شارك منشوراً/ريل من الإنستغرام: {d.get('url','')}] اسأله أي منتج يقصد إن لم يتضح", None
    if k == "story": return f"[ردّ/إشارة على ستوري] {d.get('text','')} (اسأله أي منتج في الستوري يقصد)", None
    if k == "file": return f"[ملف مرفق {d.get('filename','')}] لا تستطيع فتحه؛ اطلب منه كتابة ما يريد", None
    if k == "sticker": return "", None
    return "[رسالة بنوع غير مدعوم؛ اطلب منه بلطف كتابة طلبه]", None

# ---------- العامل: صندوق وارد → معالجة ----------
active, active_lock = set(), threading.Lock()
pool = ThreadPoolExecutor(max_workers=WORKERS)
last_loop = time.time()

def process_user(ukey):
    try:
        items = q("SELECT * FROM inbox WHERE ukey=? AND status='pending' ORDER BY id", (ukey,))
        if not items: return
        ids = [i["id"] for i in items]; marks = ",".join("?" * len(ids))
        q(f"UPDATE inbox SET status='processing' WHERE id IN ({marks})", ids, write=True)
        ch, uid = items[0]["channel"], items[0]["uid"]
        try:
            q("INSERT OR IGNORE INTO convos(ukey,channel,uid,name) VALUES(?,?,?,?)", (ukey, ch, uid, items[0]["name"] or uid), write=True)
            now = time.time()
            q("UPDATE convos SET last_in=?, name=COALESCE(?,name) WHERE ukey=?", (now, items[0]["name"], ukey), write=True)
            # حماية من الإغراق/الحلقات
            cv = q("SELECT * FROM convos WHERE ukey=?", (ukey,), one=True)
            n = (cv["burst_n"] + len(items)) if now - cv["burst_ts"] < 3600 else len(items)
            q("UPDATE convos SET burst_n=?, burst_ts=? WHERE ukey=?", (n, cv["burst_ts"] if now - cv["burst_ts"] < 3600 else now, ukey), write=True)
            if n > 60:
                alert(f"رسائل كثيرة جداً من {ukey} خلال ساعة؛ أوقفت الرد عليه تلقائياً.", f"burst{ukey}", 3600)
                q(f"UPDATE inbox SET status='done' WHERE id IN ({marks})", ids, write=True); return
            show_typing(ch, uid, items[-1]["mid"])
            texts, images = [], []
            for it in items:
                t, img = render_item(it)
                if t: texts.append(t)
                if img: images.append(img)
            text = "\n".join(texts).strip()
            if not text and not images:
                q(f"UPDATE inbox SET status='done' WHERE id IN ({marks})", ids, write=True); return
            if (cv["human_until"] or 0) > now:      # صاحب المتجر يتولى المحادثة
                q("INSERT INTO msgs(ukey,role,content,ts) VALUES(?,?,?,?)", (ukey, "user", text, now), write=True)
                alert(f"رسالة جديدة من زبون تتولاه ({cv['name']}): {text[:300]}", f"h{ukey}", 120)
                q(f"UPDATE inbox SET status='done' WHERE id IN ({marks})", ids, write=True); return
            reply = run_agent(ukey, text, images)
            if reply:
                q("INSERT INTO msgs(ukey,role,content,ts) VALUES(?,?,?,?)", (ukey, "assistant", reply, time.time()), write=True)
                send_parts(ukey, reply)
            q(f"UPDATE inbox SET status='done' WHERE id IN ({marks})", ids, write=True)
        except Exception:
            log.exception("process_user failed")
            att = max(i["attempts"] for i in items) + 1
            if att < 3:   # أعد المحاولة بعد مهلة
                q(f"UPDATE inbox SET status='pending', attempts=?, ts=? WHERE id IN ({marks})", [att, time.time() + 20 * att] + ids, write=True)
            else:
                q(f"UPDATE inbox SET status='failed', attempts=? WHERE id IN ({marks})", [att] + ids, write=True)
                send_text(ch, uid, "معذرة على التأخر 🙏 صاحب المتجر راح يرد عليك في أقرب وقت.")
                alert(f"❌ فشل الرد على {ukey} بعد 3 محاولات؛ رُدّ عليه يدوياً.")
    finally:
        with active_lock: active.discard(ukey)

def worker_loop():
    global last_loop
    while True:
        try:
            last_loop = time.time()
            now = time.time()
            for r in q("SELECT ukey, MAX(ts) AS last FROM inbox WHERE status='pending' GROUP BY ukey"):
                if now - r["last"] >= DEBOUNCE:
                    with active_lock:
                        if r["ukey"] in active: continue
                        active.add(r["ukey"])
                    pool.submit(process_user, r["ukey"])
        except Exception: log.exception("worker loop")
        time.sleep(1)

def monitor_loop():
    while True:
        time.sleep(60)
        try:
            old = q("SELECT MIN(ts) AS t, COUNT(*) AS n FROM inbox WHERE status='pending'", one=True)
            if old["n"] and time.time() - old["t"] > 300:
                alert(f"⏱️ {old['n']} رسالة تنتظر أكثر من 5 دقائق. تحقق من الوكيل والمفاتيح.", "stuck", 1800)
            if time.time() - last_loop > 60: alert("⚠️ حلقة المعالجة متوقفة!", "loop", 600)
        except Exception: log.exception("monitor")

# ---------- Webhooks ----------
def verify_sig(secret):
    if not secret: return
    exp = "sha256=" + hmac.new(secret.encode(), request.get_data(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(request.headers.get("X-Hub-Signature-256", ""), exp): abort(403)

def ingest(channel, uid, name, mid, kind, data):
    ukey = f"{channel}:{uid}"
    q("INSERT OR IGNORE INTO inbox(ukey,channel,uid,name,mid,kind,data,ts) VALUES(?,?,?,?,?,?,?,?)",
      (ukey, channel, uid, name, mid, kind, json.dumps(data, ensure_ascii=False), time.time()), write=True)

def parse_wa(p):
    for e in p.get("entry", []):
        for ch in e.get("changes", []):
            v = ch.get("value", {}); names = {c.get("wa_id"): c.get("profile", {}).get("name") for c in v.get("contacts", [])}
            for m in v.get("messages", []):
                u, t, mid = m["from"], m.get("type"), m["id"]; nm = names.get(u)
                if t == "text": ingest("wa", u, nm, mid, "text", {"text": m["text"]["body"]})
                elif t in ("audio", "image", "video"): ingest("wa", u, nm, mid, t, {"media_id": m[t]["id"], "caption": m[t].get("caption", "")})
                elif t == "document": ingest("wa", u, nm, mid, "file", {"filename": m["document"].get("filename", "")})
                elif t == "sticker": ingest("wa", u, nm, mid, "sticker", {})
                elif t == "location": ingest("wa", u, nm, mid, "location", {k: m["location"].get(k) for k in ("latitude", "longitude", "name", "address")} | {"lat": m["location"].get("latitude"), "lng": m["location"].get("longitude")})
                elif t == "contacts": ingest("wa", u, nm, mid, "contact", {"text": json.dumps(m["contacts"], ensure_ascii=False)[:300]})
                elif t == "interactive":
                    i = m["interactive"]; r = i.get("button_reply") or i.get("list_reply") or {}
                    ingest("wa", u, nm, mid, "text", {"text": r.get("title", "")})
                elif t == "button": ingest("wa", u, nm, mid, "text", {"text": m["button"].get("text", "")})
                elif t == "reaction": pass
                else: ingest("wa", u, nm, mid, "other", {})

def parse_ig(p):
    for e in p.get("entry", []):
        for ev in e.get("messaging", []):
            msg = ev.get("message")
            if not msg and ev.get("postback"):
                ingest("ig", ev["sender"]["id"], None, f"pb{ev['timestamp']}{ev['sender']['id']}", "text", {"text": ev["postback"].get("title", "")}); continue
            if not msg: continue
            if msg.get("is_echo"):   # رسالة أرسلناها: إن لم نرسلها نحن فصاحب المتجر تدخّل يدوياً
                if not q("SELECT 1 FROM sent WHERE mid=?", (msg.get("mid"),), one=True):
                    uk = f"ig:{ev['recipient']['id']}"
                    q("INSERT OR IGNORE INTO convos(ukey,channel,uid,name) VALUES(?,?,?,?)", (uk, "ig", ev["recipient"]["id"], ev["recipient"]["id"]), write=True)
                    q("UPDATE convos SET human_until=? WHERE ukey=?", (time.time() + HUMAN_HOURS * 3600, uk), write=True)
                continue
            u, mid = ev["sender"]["id"], msg["mid"]
            story = (msg.get("reply_to") or {}).get("story")
            if msg.get("text"):
                if story: ingest("ig", u, None, mid, "story", {"text": msg["text"]})
                else: ingest("ig", u, None, mid, "text", {"text": msg["text"]})
            for i, a in enumerate(msg.get("attachments", [])):
                t, url = a.get("type"), (a.get("payload") or {}).get("url")
                m2 = f"{mid}:{i}"
                if t in ("image", "audio", "video") and url: ingest("ig", u, None, m2, t, {"url": url})
                elif t in ("share", "ig_post", "ig_reel", "reel"): ingest("ig", u, None, m2, "share", {"url": url or ""})
                elif t == "story_mention": ingest("ig", u, None, m2, "story", {"text": "أشار إلى المتجر في ستوري"})
                elif t == "like_heart" or t == "sticker": ingest("ig", u, None, m2, "sticker", {})
                elif t == "file": ingest("ig", u, None, m2, "file", {})
                else: ingest("ig", u, None, m2, "other", {})

@app.get("/webhook")
@app.get("/ig-webhook")
def verify():
    if request.args.get("hub.verify_token") == VERIFY_TOKEN: return request.args.get("hub.challenge", ""), 200
    abort(403)

@app.post("/webhook")
def wa_hook():
    verify_sig(WA_SECRET)
    try: parse_wa(request.get_json(force=True))
    except Exception: log.exception("wa parse")
    return "ok", 200     # نردّ 200 فوراً؛ الرسالة محفوظة في الصندوق

@app.post("/ig-webhook")
def ig_hook():
    verify_sig(IG_SECRET)
    try: parse_ig(request.get_json(force=True))
    except Exception: log.exception("ig parse")
    return "ok", 200

@app.get("/health")
def health():
    st = q("SELECT COUNT(*) AS n, MIN(ts) AS t FROM inbox WHERE status='pending'", one=True)
    alive = time.time() - last_loop < 30
    return jsonify(ok=alive, pending=st["n"], oldest_s=int(time.time() - st["t"]) if st["t"] else 0, uptime_s=int(time.time() - START)), (200 if alive else 503)

# ---------- التشغيل ----------
_started = False
def start_background():
    global _started
    if _started: return
    _started = True
    if ENV == "production" and catalog().get("_example"):
        sys.exit("catalog.json ما زال تجريبياً (_example). ضع منتجاتك وأسعارك الحقيقية وأزل _example قبل الإنتاج.")
    threading.Thread(target=worker_loop, daemon=True).start()
    threading.Thread(target=monitor_loop, daemon=True).start()
    log.info("الوكيل يعمل. صندوق الوارد الدائم نشط.")

if not E("NO_BG"): start_background()

def cmd_learn(folder):
    files = sorted(glob.glob(os.path.join(folder, "*.txt")))[:30]
    if not files: return print(f"ضع حتى ٣٠ محادثة (.txt) في {folder}")
    blob = "\n\n===== محادثة =====\n".join(read_file(f)[:5000] for f in files)
    blob = re.sub(r"\+?\d[\d\s\-]{8,}\d", "[رقم]", blob)
    out = text_of(claude([{"role": "user", "content":
        "حلّل محادثات زبائن متجر ملابس جزائري وبائعه، واكتب دليل أسلوب مختصراً (<600 كلمة) بالعربية: أكثر الأسئلة تكراراً وأفضل إجاباتها، "
        "عبارات الدارجة والفرنسية الشائعة، الاعتراضات وكيف تُعالج، أخطاء يجب تجنبها. لا تخترع شيئاً.\n\n" + blob}],
        "أنت محلل مبيعات.", max_tokens=1800))
    open(LEARNED_FILE, "w", encoding="utf-8").write(out); print("✓ حُفظ في", LEARNED_FILE)

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("run"); sub.add_parser("orders"); sub.add_parser("flags")
    sub.add_parser("learn").add_argument("--dir", default="chats")
    sub.add_parser("resume").add_argument("ukey")
    a = ap.parse_args()
    if a.cmd == "learn": cmd_learn(a.dir)
    elif a.cmd == "orders":
        for o in q("SELECT * FROM orders ORDER BY id DESC LIMIT 20"): print(f"#{o['id']} {o['customer']} {o['phone']} {o['wilaya']} | {o['items']} | {o['total']} دج | {o['status']}")
    elif a.cmd == "flags":
        for r in q("SELECT * FROM flags ORDER BY id DESC LIMIT 20"): print(f"- [{r['issue']}]\n  قبل: {r['draft']}\n  بعد: {r['fixed']}\n")
    elif a.cmd == "resume":
        q("UPDATE convos SET human_until=0 WHERE ukey=?", (a.ukey,), write=True); print("عاد البوت لهذا الزبون")
    else: app.run(host="0.0.0.0", port=int(E("PORT", 8000)))
