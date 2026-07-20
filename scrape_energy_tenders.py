"""
سكربت فحص منصة اعتماد (tenders.etimad.sa) عن المنافسات المتاحة لـ "وزارة الطاقة"
=================================================================================

وش يسوي هذا السكربت؟
1) يفتح متصفح آلي (بدون واجهة رسومية - headless) عبر مكتبة Playwright.
2) يدخل صفحة كل المنافسات في اعتماد.
3) يفلتر تلقائيًا على الجهة الحكومية = "وزارة الطاقة" (نفس الكود 029001000000 اللي اكتشفناه).
4) يقرأ كل المنافسات، يستبعد المنتهية منها، ويرتب الباقي من الأقرب إغلاقًا للأبعد.
5) يكتب النتيجة في ملفين:
   - tenders_data.json  (بيانات خام، تقدر تستخدمها في أي مكان)
   - energy-ministry-tenders.html  (صفحة جاهزة للعرض، بنفس تصميم صفحة Cowork)

طريقة التشغيل يدويًا على جهازك (لتجربته قبل رفعه على GitHub):
    pip install playwright
    playwright install chromium
    python scrape_energy_tenders.py

بعد التجربة، ارفع هذا الملف + requirements.txt + ملف الجدولة (hourly.yml)
على مستودع GitHub، وفعّل GitHub Actions ليشتغل كل ساعة تلقائيًا.
"""

import asyncio
import json
import os
import re
import sys
from datetime import datetime, date
from pathlib import Path

from playwright.async_api import async_playwright

# -------- إعدادات ثابتة --------
BASE_URL = "https://tenders.etimad.sa/Tender/AllTendersForVisitor?PageNumber=1"
AGENCY_CODE = "029001000000"  # الكود الدقيق لـ "وزارة الطاقة" (وليس فروعها)

# رابط البحث المفلتر مباشرة على وزارة الطاقة.
# هذي هي نفس معاملات الرابط التي ينتجها الموقع عند الفلترة يدويًا،
# فنستخدمها مباشرة بدل التعامل مع القائمة المنسدلة التي لا تتحمّل آليًا.
SEARCH_URL = (
    "https://tenders.etimad.sa/Tender/AllTendersForVisitor"
    "?MultipleSearch="
    "&TenderCategory=0"
    "&ReferenceNumber="
    "&TenderNumber="
    f"&agency={AGENCY_CODE}"
    "&ConditionaBookletRange="
    "&PublishDateId=5"
    "&LastOfferPresentationDate="
    "&TenderAreasIdString="
    "&TenderTypeId="
    "&TenderActivityId="
    "&TenderSubActivityId="
    f"&AgencyCode={AGENCY_CODE}"
    "&FromLastOfferPresentationDateString="
    "&ToLastOfferPresentationDateString="
    "&SortDirection=DESC"
    "&Sort=SubmitionDate"
    "&PageSize=24"
    "&IsSearch=true"
    "&PageNumber=1"
)
OUTPUT_JSON = Path(__file__).parent / "tenders_data.json"
OUTPUT_HTML = Path(__file__).parent / "energy-ministry-tenders.html"
# نفس الصفحة باسم index.html حتى يفتحها رابط الموقع مباشرة (GitHub Pages)
OUTPUT_INDEX = Path(__file__).parent / "index.html"

# -------- إعدادات البروكسي (اختيارية) --------
# ما تكتب بيانات البروكسي هنا أبدًا. السكربت يقرأها من متغيرات البيئة
# (Environment Variables) اللي تُضبط في GitHub Secrets، أو تصدّرها محليًا
# قبل التشغيل اليدوي، مثلاً:
#   export PROXY_SERVER="http://host:port"
#   export PROXY_USERNAME="xxxx"
#   export PROXY_PASSWORD="xxxx"
# لو ما ضبطت هذي المتغيرات، السكربت يشتغل بدون بروكسي عادي (زي جهازك الحالي).
PROXY_SERVER = os.environ.get("PROXY_SERVER", "").strip()
PROXY_USERNAME = os.environ.get("PROXY_USERNAME", "").strip()
PROXY_PASSWORD = os.environ.get("PROXY_PASSWORD", "").strip()


def _build_proxy_config():
    """يبني إعدادات البروكسي لـ Playwright لو كانت متوفرة، وإلا يرجع None."""
    if not PROXY_SERVER:
        return None
    config = {"server": PROXY_SERVER}
    if PROXY_USERNAME:
        config["username"] = PROXY_USERNAME
    if PROXY_PASSWORD:
        config["password"] = PROXY_PASSWORD
    return config


# بصمة متصفح واقعية: نفس ما يرسله متصفح Chrome حقيقي على ويندوز
REAL_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

# سكربت يُحقن قبل تحميل أي صفحة، يخفي العلامات التي تكشف أن المتصفح آلي
STEALTH_SCRIPT = """
// إخفاء navigator.webdriver (أشهر علامة تكشف الأتمتة)
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});

// إضافة إضافات وهمية (المتصفح الآلي يكون بلا إضافات، وهذا مريب)
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});

// ضبط اللغات بشكل طبيعي (عربي + إنجليزي)
Object.defineProperty(navigator, 'languages', {get: () => ['ar-SA', 'ar', 'en-US', 'en']});

// إظهار عدد أنوية معالج ومقدار ذاكرة واقعيين
Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});
Object.defineProperty(navigator, 'deviceMemory', {get: () => 8});

// كائن chrome الموجود في المتصفح الحقيقي فقط
window.chrome = window.chrome || {runtime: {}};

// تعديل استجابة الأذونات لتطابق سلوك المتصفح الحقيقي
const originalQuery = window.navigator.permissions && window.navigator.permissions.query;
if (originalQuery) {
  window.navigator.permissions.query = (parameters) => (
    parameters.name === 'notifications'
      ? Promise.resolve({state: Notification.permission})
      : originalQuery(parameters)
  );
}
"""


async def _try_fetch_once(attempt: int):
    """محاولة واحدة لفتح اعتماد وتطبيق الفلاتر. ترجع قائمة نصوص البطاقات أو ترفع استثناء."""
    proxy_config = _build_proxy_config()
    async with async_playwright() as p:
        launch_kwargs = {
            "headless": True,
            # وسائط تخفي علامات الأتمتة على مستوى المتصفح نفسه
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--lang=ar-SA",
            ],
        }
        if proxy_config:
            launch_kwargs["proxy"] = proxy_config
            print(f"[محاولة {attempt}] تشغيل عبر البروكسي: {PROXY_SERVER}")
        else:
            print(f"[محاولة {attempt}] تشغيل بدون بروكسي (اتصال مباشر).")
        browser = await p.chromium.launch(**launch_kwargs)

        # سياق متصفح ببصمة واقعية (لغة، توقيت، حجم شاشة، ترويسات طبيعية)
        context = await browser.new_context(
            user_agent=REAL_USER_AGENT,
            viewport={"width": 1920, "height": 1080},
            locale="ar-SA",
            timezone_id="Asia/Riyadh",
            extra_http_headers={
                "Accept-Language": "ar-SA,ar;q=0.9,en-US;q=0.8,en;q=0.7",
            },
        )
        # حقن سكربت التخفي قبل تحميل أي صفحة
        await context.add_init_script(STEALTH_SCRIPT)
        page = await context.new_page()

        # نذهب مباشرة إلى رابط البحث المفلتر على "وزارة الطاقة".
        # هذا يتجاوز تمامًا مشكلة قائمة الجهات المنسدلة (التي لا تتحمّل في البيئة الآلية)،
        # لأن الفلاتر كلها تنعكس في معاملات الرابط نفسه.
        print(f"[محاولة {attempt}] فتح الرابط المفلتر مباشرة...")
        await page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(12000)  # انتظار تحميل النتائج (JS + بيانات)

        # التحقق من أن النتائج ظهرت فعلاً (نبحث عن بطاقات المنافسات)
        results_loaded = False
        for _ in range(6):  # حتى 30 ثانية انتظار إضافي
            count = await page.evaluate(
                """() => Array.from(document.querySelectorAll('*'))
                    .filter(e => e.children.length === 0 && e.textContent.trim() === 'الرقم المرجعي').length"""
            )
            if count and count > 0:
                results_loaded = True
                print(f"[محاولة {attempt}] تم العثور على {count} بطاقة منافسة.")
                break
            await page.wait_for_timeout(5000)

        if not results_loaded:
            # تشخيص: نطبع معلومات تكشف السبب الحقيقي للفشل قبل ما نستسلم
            diag = await page.evaluate(
                """() => ({
                    title: document.title,
                    url: location.href,
                    bodyLength: document.body ? document.body.innerText.length : 0,
                    bodySnippet: document.body ? document.body.innerText.slice(0, 500) : '',
                    hasNoData: document.body ? document.body.textContent.includes('لا توجد بيانات') : false,
                    hasJQuery: typeof window.jQuery !== 'undefined',
                    webdriverFlag: navigator.webdriver
                })"""
            )
            print("---------- تشخيص الفشل ----------")
            print(f"عنوان الصفحة: {diag.get('title')}")
            print(f"الرابط الحالي: {diag.get('url')}")
            print(f"طول محتوى الصفحة: {diag.get('bodyLength')} حرف")
            print(f"هل ظهرت رسالة 'لا توجد بيانات'؟ {diag.get('hasNoData')}")
            print(f"هل jQuery محمّلة؟ {diag.get('hasJQuery')}")
            print(f"علامة webdriver: {diag.get('webdriverFlag')}")
            print(f"مقتطف من الصفحة:\n{diag.get('bodySnippet')}")
            print("--------------------------------")

            # لو الموقع صرّح أنه "لا توجد بيانات"، فهذه نتيجة صحيحة وليست فشلًا
            if diag.get("hasNoData"):
                await browser.close()
                print("الموقع يقول: لا توجد منافسات مطابقة حاليًا.")
                return []

            await browser.close()
            raise RuntimeError(
                f"[محاولة {attempt}] لم تظهر أي نتائج خلال المهلة المحددة."
            )

        # استخراج بطاقات المنافسات من الصفحة (النص + رابط التفاصيل)
        cards_text = await page.evaluate(
            """() => {
                const all = Array.from(document.querySelectorAll('*'));
                const refLabelEls = all.filter(
                    e => e.children.length === 0 && e.textContent.trim() === 'الرقم المرجعي'
                );
                function findCard(el) {
                    let cur = el;
                    for (let i = 0; i < 12; i++) {
                        if (!cur.parentElement) break;
                        cur = cur.parentElement;
                        if (cur.querySelectorAll('a').length && cur.innerText.length > 200 && cur.innerText.length < 2000) {
                            return cur;
                        }
                    }
                    return cur;
                }
                return refLabelEls.map(el => {
                    const card = findCard(el);
                    // نبحث عن رابط تفاصيل المنافسة داخل البطاقة
                    const links = Array.from(card.querySelectorAll('a[href]'));
                    const detail = links.find(a =>
                        a.href.includes('DetailsForVisitor') ||
                        a.href.includes('Details') ||
                        a.textContent.trim() === 'التفاصيل'
                    );
                    return {
                        text: card.innerText.replace(/\\s+/g, ' ').trim(),
                        link: detail ? detail.href : ''
                    };
                });
            }"""
        )

        await browser.close()
        return cards_text


async def fetch_active_tenders():
    """يحاول فتح اعتماد وتطبيق الفلاتر، ويعيد المحاولة تلقائيًا (حتى 3 مرات) لو فشلت المرة الأولى."""
    last_error = None
    for attempt in range(1, 4):
        try:
            return await _try_fetch_once(attempt)
        except Exception as e:
            last_error = e
            print(f"المحاولة {attempt} فشلت: {e}")
            if attempt < 3:
                await asyncio.sleep(5)
    raise last_error


def extract_fields(card: dict) -> dict:
    """يستخرج الحقول المطلوبة من بطاقة منافسة واحدة (نص + رابط) عبر تعابير نمطية (regex)."""
    text = card.get("text", "") if isinstance(card, dict) else str(card)
    link = card.get("link", "") if isinstance(card, dict) else ""
    pub_match = re.search(r"تاريخ النشر\s*:\s*([\d-]+)", text)
    ref_match = re.search(r"الرقم المرجعي\s*(\d+)", text)
    deadline_match = re.search(r"آخر موعد لتقديم العروض\s*([\d\- :]+)", text)
    title_match = re.search(
        r"(?:منافسة عامة|شراء مباشر|منافسة محدودة|منافسة إتفاقية إطارية|مسابقة|المزايدة العكسية الالكترونية)\s+(.+?)\s+وزارة الطاقة",
        text,
    )
    ended = ("إنتهى" in text) or ("انتهى" in text)

    return {
        "title": title_match.group(1).strip() if title_match else "",
        "reference_number": ref_match.group(1) if ref_match else "",
        "agency": "وزارة الطاقة - شعبة المشتريات - الرياض",
        "publish_date": pub_match.group(1) if pub_match else "",
        "deadline": deadline_match.group(1).strip() if deadline_match else "",
        "ended": ended,
        "link": link,
    }


def days_remaining(deadline_str: str, today: date) -> int:
    try:
        deadline_date = datetime.strptime(deadline_str[:10], "%Y-%m-%d").date()
        return (deadline_date - today).days
    except ValueError:
        return -9999


def load_previous_state() -> dict:
    """يقرأ نتيجة آخر تشغيل ناجح (إن وجدت) للمقارنة بها — أساس كاشف الأعطال."""
    if not OUTPUT_JSON.exists():
        return {}
    try:
        return json.loads(OUTPUT_JSON.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"تعذّرت قراءة الحالة السابقة: {e}")
        return {}


def build_html(active_tenders: list, today_str: str, health: dict = None) -> str:
    health = health or {}
    banner_html = ""

    if health.get("status") == "stale":
        last_ok = health.get("last_successful_update", "غير معروف")
        banner_html = f"""
  <div class="alert">
    <b>⚠️ تعذّر تحديث البيانات في آخر محاولة.</b>
    البيانات المعروضة أدناه من آخر تحديث ناجح بتاريخ <b>{last_ok}</b> وقد لا تكون دقيقة الآن.
    السبب المرجّح: تغيّر في بنية موقع اعتماد. يُنصح بالتحقق يدويًا من
    <a href="https://tenders.etimad.sa/Tender/AllTendersForVisitor" target="_blank">المنصة</a>.
  </div>"""

    if not active_tenders:
        cards_html = """
        <div class="empty">لا توجد حالياً منافسات متاحة للتقديم لوزارة الطاقة في اعتماد.</div>
        """
    else:
        cards = []
        for t in active_tenders:
            days = t["days_remaining"]
            if days <= 3:
                badge_class, badge_text = "urgent", f"{days} يوم متبقٍ" if days != 1 else "يوم واحد متبقٍ"
            elif days <= 10:
                badge_class, badge_text = "soon", f"{days} أيام متبقية"
            else:
                badge_class, badge_text = "ok", f"{days} يومًا متبقية"

            # لو توفر رابط التفاصيل، نجعل العنوان قابلاً للنقر ويفتح في تبويب جديد
            link = t.get("link", "")
            if link:
                title_html = (
                    f'<a class="card-title-link" href="{link}" target="_blank" '
                    f'rel="noopener">{t["title"]}</a>'
                )
            else:
                title_html = t["title"]

            cards.append(f"""
    <div class="card">
      <p class="card-title">{title_html}</p>
      <div class="meta">
        <span>رقم المنافسة: <b>{t['reference_number']}</b></span>
        <span>الجهة: <b>{t['agency']}</b></span>
        <span>تاريخ النشر: <b>{t['publish_date']}</b></span>
        <span>آخر موعد للتقديم: <b>{t['deadline']}</b></span>
        <span class="badge {badge_class}">{badge_text}</span>
      </div>
    </div>""")
        cards_html = "\n".join(cards)

    return f"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Q Day Check — منافسات وزارة الطاقة</title>
<style>
  :root {{ color-scheme: light; }}
  * {{ box-sizing: border-box; }}
  body {{ font-family: 'Segoe UI', Tahoma, Arial, sans-serif; background: #f5f7f6; color: #1a2e2b; margin: 0; padding: 24px; }}
  .header {{ display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px; margin-bottom: 16px; }}
  h1 {{ font-size: 20px; margin: 0; color: #0d3b44; }}
  .updated {{ font-size: 13px; color: #667; background: #eef3f2; padding: 6px 12px; border-radius: 20px; }}
  .card-list {{ display: flex; flex-direction: column; gap: 12px; }}
  .card {{ background: #fff; border: 1px solid #e0e6e4; border-radius: 12px; padding: 16px 18px; box-shadow: 0 1px 3px rgba(0,0,0,0.04); }}
  .subtitle {{ font-size: 14px; font-weight: 400; color: #5a7a72; }}
  .card-title {{ font-size: 16px; font-weight: 600; color: #0d3b44; margin: 0 0 8px 0; }}
  .card-title-link {{ color: #0d3b44; text-decoration: none; border-bottom: 1px solid #b9d4cd; transition: color .15s, border-color .15s; }}
  .card-title-link:hover {{ color: #12708a; border-bottom-color: #12708a; }}
  .meta {{ display: flex; flex-wrap: wrap; gap: 8px 20px; font-size: 13px; color: #445; }}
  .meta span b {{ color: #1a2e2b; }}
  .badge {{ display: inline-block; font-size: 12px; font-weight: 600; padding: 3px 10px; border-radius: 20px; margin-inline-start: 8px; }}
  .badge.urgent {{ background: #fde2e1; color: #9b2c2c; }}
  .badge.soon {{ background: #fff3cd; color: #8a6100; }}
  .badge.ok {{ background: #dcf5e6; color: #1a7a44; }}
  .empty {{ text-align: center; color: #667; padding: 40px 0; font-size: 15px; }}
  .footer-note {{ margin-top: 20px; font-size: 12px; color: #889; }}
  .alert {{ background: #fff4e5; border: 1px solid #f0c48a; border-radius: 10px;
            padding: 14px 16px; margin-bottom: 16px; font-size: 14px; color: #7a4b0a; line-height: 1.7; }}
  .alert a {{ color: #7a4b0a; }}
  @media (max-width: 600px) {{
    body {{ padding: 14px; }}
    h1 {{ font-size: 17px; }}
    .subtitle {{ display: block; margin-top: 4px; }}
    .meta {{ gap: 6px 12px; font-size: 12.5px; }}
    .header {{ align-items: flex-start; }}
  }}
</style>
</head>
<body>
  <div class="header">
    <h1>Q Day Check <span class="subtitle">— المنافسات المتاحة لوزارة الطاقة</span></h1>
    <div class="updated">آخر تحديث: {today_str}</div>
  </div>
  {banner_html}
  <div class="card-list">
    {cards_html}
  </div>
  <p class="footer-note">
    تُحدَّث هذه الصفحة تلقائيًا كل يوم صباحًا، وتستبعد المنافسات المنتهية. اضغط على اسم المنافسة لفتح تفاصيلها في اعتماد
    (قد يُطلب منك تسجيل الدخول).
    المصدر: <a href="https://tenders.etimad.sa/Tender/AllTendersForVisitor" target="_blank">tenders.etimad.sa</a>
  </p>
</body>
</html>
"""


async def main():
    today = date.today()
    today_str = today.isoformat()

    # ---------- كاشف الأعطال (Health Watchdog) ----------
    # الفكرة: الفشل الصامت هو الخطر الأكبر. لو تغيّرت بنية موقع اعتماد،
    # الاستخراج يرجع صفر نتائج، والصفحة تعرض "لا توجد منافسات" — وهذا لا يُفرَّق
    # عن نتيجة فارغة حقيقية. فنقارن كل تشغيل بآخر تشغيل ناجح لكشف ذلك.
    previous = load_previous_state()
    previous_tenders = previous.get("tenders", [])
    previous_count = len(previous_tenders)
    last_successful = previous.get("last_successful_update", previous.get("updated", "غير معروف"))

    raw_cards = await fetch_active_tenders()
    print(f"عدد البطاقات الخام المستخرجة: {len(raw_cards)}")

    all_tenders = [extract_fields(c) for c in raw_cards]

    # فحص جودة الاستخراج: بطاقة صحيحة يجب أن تحتوي رقمًا مرجعيًا وموعدًا
    well_formed = [t for t in all_tenders if t["reference_number"] and t["deadline"]]
    if all_tenders and len(well_formed) < len(all_tenders) / 2:
        print(
            f"تحذير: {len(all_tenders) - len(well_formed)} من أصل {len(all_tenders)} "
            "بطاقة لم تُستخرج حقولها بشكل صحيح — قد تكون صياغة الموقع تغيّرت."
        )

    active = []
    for t in all_tenders:
        if t["ended"] or not t["deadline"]:
            continue
        remaining = days_remaining(t["deadline"], today)
        if remaining >= 0:
            t["days_remaining"] = remaining
            active.append(t)

    active.sort(key=lambda t: t["deadline"])

    # ---------- قرار الصحة ----------
    # نعتبر النتيجة "مريبة" إذا لم نجد أي بطاقة خام إطلاقًا بينما كان لدينا
    # بيانات سابقة. ملاحظة مهمة: انتهاء صلاحية كل المنافسات أمر طبيعي
    # (raw_cards > 0 لكن active == 0)، أما raw_cards == 0 فيعني أن الاستخراج نفسه فشل.
    suspicious = (len(raw_cards) == 0 and previous_count > 0)

    if suspicious:
        print("=" * 60)
        print("!! كاشف الأعطال: نتيجة مريبة !!")
        print(f"لم يُعثر على أي بطاقة، بينما آخر تشغيل ناجح وجد {previous_count} منافسة.")
        print("الاحتمال الأرجح: تغيّرت بنية صفحات اعتماد أو صياغة 'الرقم المرجعي'.")
        print("الإجراء: تم الإبقاء على آخر بيانات ناجحة وعرض تنبيه في الصفحة.")
        print("=" * 60)

        # لا نستبدل البيانات الجيدة ببيانات فارغة مشبوهة — نُبقيها ونضيف تنبيهًا
        health = {"status": "stale", "last_successful_update": last_successful}
        display_tenders = previous_tenders
        state = {
            "updated": today_str,
            "last_successful_update": last_successful,
            "health": "stale",
            "health_note": "الاستخراج رجع صفر بطاقات بينما توجد بيانات سابقة",
            "tenders": previous_tenders,
        }
    else:
        health = {"status": "ok"}
        display_tenders = active
        state = {
            "updated": today_str,
            "last_successful_update": today_str,
            "health": "ok",
            "tenders": active,
        }

    OUTPUT_JSON.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    html = build_html(display_tenders, state["last_successful_update"], health)
    OUTPUT_HTML.write_text(html, encoding="utf-8")
    OUTPUT_INDEX.write_text(html, encoding="utf-8")  # نسخة للموقع العام

    if suspicious:
        # الخروج بخطأ يجعل GitHub يضع علامة فشل حمراء ويرسل لك بريدًا تلقائيًا
        print("إنهاء بحالة خطأ لتنبيهك عبر بريد GitHub.")
        sys.exit(1)

    print(f"تم العثور على {len(active)} منافسة متاحة لوزارة الطاقة بتاريخ {today_str}")
    if previous_count and len(active) != previous_count:
        print(f"(تغيّر العدد: {previous_count} ← {len(active)})")


if __name__ == "__main__":
    asyncio.run(main())
