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
import re
from datetime import datetime, date
from pathlib import Path

from playwright.async_api import async_playwright

# -------- إعدادات ثابتة --------
BASE_URL = "https://tenders.etimad.sa/Tender/AllTendersForVisitor?PageNumber=1"
AGENCY_CODE = "029001000000"  # الكود الدقيق لـ "وزارة الطاقة" (وليس فروعها)
OUTPUT_JSON = Path(__file__).parent / "tenders_data.json"
OUTPUT_HTML = Path(__file__).parent / "energy-ministry-tenders.html"


async def fetch_active_tenders():
    """يفتح اعتماد، يطبّق الفلاتر، ويرجّع قائمة المنافسات (بدون فلترة التاريخ بعد)."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        await page.goto(BASE_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(6000)  # الموقع بطيء أحيانًا، ننتظر تحميله كامل

        # 1) توسيع قسم "البحث المتقدم"
        await page.evaluate(
            """() => {
                const a = document.querySelector('a.search-expand[href="#dates"]');
                if (a) a.click();
            }"""
        )
        await page.wait_for_timeout(500)

        # 2) فتح قائمة "الجهة الحكوميه" لإجبار الموقع على تحميل الأسماء الحقيقية
        await page.evaluate(
            """() => {
                const btn = document.querySelector('button[data-id="agency"]');
                if (btn) btn.click();
            }"""
        )
        # ننتظر حتى يتحمّل عدد كبير من الخيارات (حوالي 1800 جهة)
        try:
            await page.wait_for_function(
                "document.getElementById('agency') && document.getElementById('agency').options.length > 100",
                timeout=8000,
            )
        except Exception:
            pass  # نكمل بأي حال، سنتحقق لاحقًا

        # 3) تعيين القيم عبر JavaScript مباشرة (نفس الطريقة التي أثبتت نجاحها)
        result = await page.evaluate(
            """(agencyCode) => {
                function setVal(id, val) {
                    const el = document.getElementById(id);
                    if (!el) return 'NOTFOUND';
                    el.value = val;
                    el.dispatchEvent(new Event('change', {bubbles:true}));
                    if (window.jQuery && jQuery.fn.selectpicker) jQuery(el).selectpicker('refresh');
                    return el.value;
                }
                const agency = setVal('agency', agencyCode);
                const category = setVal('TenderCategory', '0'); // كل الحالات
                const size = setVal('itemsPerPage', '24');
                return {agency, category, size};
            }""",
            AGENCY_CODE,
        )

        if result.get("agency") != AGENCY_CODE:
            await browser.close()
            raise RuntimeError(
                "فشل تعيين فلتر الجهة الحكومية - قد تكون قائمة الجهات ما تحمّلت. جرّب تشغيل السكربت مرة ثانية."
            )

        # 4) الضغط على زر البحث الفعلي
        await page.evaluate(
            """() => {
                const btns = Array.from(document.querySelectorAll('button'))
                    .filter(b => b.textContent.includes('بحث') && !b.textContent.includes('مسح'));
                const submitBtn = btns.find(b => b.className.includes('btn-primary'));
                if (submitBtn) submitBtn.click();
            }"""
        )
        await page.wait_for_timeout(3000)

        # 5) استخراج بطاقات المنافسات من الصفحة
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
