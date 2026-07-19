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

        # استخراج بطاقات المنافسات من الصفحة
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
                return refLabelEls.map(el => findCard(el).innerText.replace(/\\s+/g, ' ').trim());
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


def extract_fields(text: str) -> dict:
    """يستخرج الحقول المطلوبة من نص بطاقة منافسة واحدة عبر تعابير نمطية (regex)."""
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
    }


def days_remaining(deadline_str: str, today: date) -> int:
    try:
        deadline_date = datetime.strptime(deadline_str[:10], "%Y-%m-%d").date()
        return (deadline_date - today).days
    except ValueError:
        return -9999


def build_html(active_tenders: list, today_str: str) -> str:
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

            cards.append(f"""
    <div class="card">
      <p class="card-title">{t['title']}</p>
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
<title>منافسات وزارة الطاقة - اعتماد</title>
<style>
  :root {{ color-scheme: light; }}
  * {{ box-sizing: border-box; }}
  body {{ font-family: 'Segoe UI', Tahoma, Arial, sans-serif; background: #f5f7f6; color: #1a2e2b; margin: 0; padding: 24px; }}
  .header {{ display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px; margin-bottom: 16px; }}
  h1 {{ font-size: 20px; margin: 0; color: #0d3b44; }}
  .updated {{ font-size: 13px; color: #667; background: #eef3f2; padding: 6px 12px; border-radius: 20px; }}
  .card-list {{ display: flex; flex-direction: column; gap: 12px; }}
  .card {{ background: #fff; border: 1px solid #e0e6e4; border-radius: 12px; padding: 16px 18px; box-shadow: 0 1px 3px rgba(0,0,0,0.04); }}
  .card-title {{ font-size: 16px; font-weight: 600; color: #0d3b44; margin: 0 0 8px 0; }}
  .meta {{ display: flex; flex-wrap: wrap; gap: 8px 20px; font-size: 13px; color: #445; }}
  .meta span b {{ color: #1a2e2b; }}
  .badge {{ display: inline-block; font-size: 12px; font-weight: 600; padding: 3px 10px; border-radius: 20px; margin-inline-start: 8px; }}
  .badge.urgent {{ background: #fde2e1; color: #9b2c2c; }}
  .badge.soon {{ background: #fff3cd; color: #8a6100; }}
  .badge.ok {{ background: #dcf5e6; color: #1a7a44; }}
  .empty {{ text-align: center; color: #667; padding: 40px 0; font-size: 15px; }}
  .footer-note {{ margin-top: 20px; font-size: 12px; color: #889; }}
</style>
</head>
<body>
  <div class="header">
    <h1>المنافسات المتاحة لوزارة الطاقة — منصة اعتماد</h1>
    <div class="updated">آخر تحديث: {today_str}</div>
  </div>
  <div class="card-list">
    {cards_html}
  </div>
  <p class="footer-note">
    يتم تحديث هذه الصفحة تلقائيًا كل ساعة عبر سكربت آلي يفحص منصة اعتماد (tenders.etimad.sa) ويستبعد المنافسات المنتهية.
    المصدر: <a href="https://tenders.etimad.sa/Tender/AllTendersForVisitor" target="_blank">tenders.etimad.sa</a>
  </p>
</body>
</html>
"""


async def main():
    today = date.today()
    today_str = today.isoformat()

    raw_cards = await fetch_active_tenders()
    all_tenders = [extract_fields(c) for c in raw_cards]

    active = []
    for t in all_tenders:
        if t["ended"] or not t["deadline"]:
            continue
        remaining = days_remaining(t["deadline"], today)
        if remaining >= 0:
            t["days_remaining"] = remaining
            active.append(t)

    active.sort(key=lambda t: t["deadline"])

    OUTPUT_JSON.write_text(
        json.dumps({"updated": today_str, "tenders": active}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    OUTPUT_HTML.write_text(build_html(active, today_str), encoding="utf-8")

    print(f"تم العثور على {len(active)} منافسة متاحة لوزارة الطاقة بتاريخ {today_str}")


if __name__ == "__main__":
    asyncio.run(main())
