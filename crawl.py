"""
雲端爬蟲腳本（取代原本的 server.py）

由 GitHub Actions 每小時自動執行：
1. 用 Playwright 爬取每位教師官網的瀏覽數與研究領域
2. 更新 views_cache.json（保留基準日資料，用來算日均瀏覽數）
3. 產生 data.json，格式與原本 /api/teachers 回傳的內容相同，網頁直接讀取

本機測試：
    python crawl.py            # 實際爬取並更新
    python crawl.py --offline  # 不爬取，只用現有快取重新產生 data.json
"""

import json
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

TAIPEI = ZoneInfo("Asia/Taipei")  # GitHub 主機在國外，日期一律用台灣時間計算

BASE_DIR = Path(__file__).resolve().parent
TEACHERS_PATH = BASE_DIR / "teachers.json"
CACHE_PATH = BASE_DIR / "views_cache.json"
DATA_PATH = BASE_DIR / "data.json"


def today_taipei() -> date:
    return datetime.now(TAIPEI).date()


def load_json(path, default):
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path, payload):
    temporary_path = path.with_suffix(".tmp")
    temporary_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    temporary_path.replace(path)


# ---------- 爬蟲（沿用原本 server.py 的邏輯） ----------

def extract_research_keywords(page):
    for heading in page.locator("h3").all():
        if heading.inner_text().strip() != "研究領域":
            continue

        section_text = heading.locator("xpath=..").inner_text()
        research_text = section_text.replace("研究領域", "", 1)
        keywords = [
            keyword.strip(" \t\r\n‧•·・、,，;；")
            for keyword in re.split(r"[‧•·・]|\r?\n+|[,，;；]", research_text)
        ]
        return [keyword for keyword in keywords if keyword][:5]

    return []


def extract_view_count(page):
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    try:
        page.wait_for_function(
            r"""() => {
                const counter = document.querySelector(".PtStatistic i");
                return counter && /\d/.test(counter.textContent || "");
            }""",
            timeout=12000,
        )
    except PlaywrightTimeoutError:
        body_text = page.inner_text("body")
        match = re.search(r"瀏覽數\s*[：:]\s*([\d,]+)", body_text)
        if match:
            return int(match.group(1).replace(",", ""))
        raise ValueError("教師官網未提供可辨識的瀏覽數") from None

    counter_text = page.locator(".PtStatistic i").inner_text()
    match = re.search(r"\d[\d,]*", counter_text)
    if match:
        return int(match.group(0).replace(",", ""))

    raise ValueError("教師官網未提供可辨識的瀏覽數")


def fetch_all(teachers):
    """回傳 (views_map, keywords_map)；失敗的教師 views 為 None、不放 keywords。"""
    from playwright.sync_api import sync_playwright

    views_map = {}
    keywords_map = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
                locale="zh-TW",
            )
            page = context.new_page()

            for teacher in teachers:
                name = teacher["name"]
                views = None
                # 每位教師最多試 2 次，降低偶發網路錯誤的影響
                for attempt in (1, 2):
                    try:
                        page.goto(teacher["url"], wait_until="domcontentloaded", timeout=20000)
                        views = extract_view_count(page)
                        keywords_map[name] = extract_research_keywords(page)
                        print(f"✅ 【{name}】 瀏覽數: {views}")
                        break
                    except Exception as error:
                        print(f"⚠️ 【{name}】 第 {attempt} 次擷取失敗：{error}")
                views_map[name] = views
        finally:
            browser.close()

    return views_map, keywords_map


# ---------- 合併快取與產生網頁資料 ----------

def merge_into_cache(cache, fetched_views, fetched_keywords, today):
    views = dict(cache.get("views", {}))
    keywords = dict(cache.get("keywords", {}))
    baselines = {k: dict(v) for k, v in cache.get("baselines", {}).items()}

    for name, count in fetched_views.items():
        if count is None:
            continue  # 擷取失敗：保留上一次的有效數據
        views[name] = count
        if name in fetched_keywords:
            keywords[name] = fetched_keywords[name]
        baseline = baselines.get(name)
        # 第一次記錄，或官網計數被重置（變小）時，重設基準日
        if baseline is None or count < baseline["views"]:
            baselines[name] = {"views": count, "date": today.isoformat()}

    return {
        "views": views,
        "keywords": keywords,
        "baselines": baselines,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def build_payload(teachers, cache, today):
    """產生與原本 /api/teachers 相同結構的資料。"""
    views_map = cache.get("views", {})
    keywords_map = cache.get("keywords", {})
    baselines_map = cache.get("baselines", {})

    results = []
    category_totals = {}

    for teacher in teachers:
        name = teacher["name"]
        views = views_map.get(name)
        baseline = baselines_map.get(name)
        average_daily_views = None
        baseline_date = None
        if baseline is not None:
            baseline_date = baseline["date"]
            if views is not None:
                elapsed_days = max(1, (today - date.fromisoformat(baseline_date)).days)
                average_daily_views = max(0, views - baseline["views"]) / elapsed_days

        results.append({
            "name": name,
            "category": teacher["category"],
            "url": teacher["url"],
            "views": views,
            "average_daily_views": average_daily_views,
            "baseline_date": baseline_date,
            "keywords": keywords_map.get(name, []),
        })

        category = teacher["category"]
        category_totals[category] = category_totals.get(category, 0) + (views or 0)

    results.sort(key=lambda item: (item["views"] is None, -(item["views"] or 0)))

    return {
        "status": "success",
        "cache_ready": True,
        "refreshing": False,
        "updated_at": cache.get("updated_at"),
        "total_views": sum(category_totals.values()),
        "teachers": results,
        "categories": category_totals,
    }


def main():
    offline = "--offline" in sys.argv
    teachers = load_json(TEACHERS_PATH, [])
    cache = load_json(CACHE_PATH, {})
    today = today_taipei()

    if not offline:
        fetched_views, fetched_keywords = fetch_all(teachers)
        success = sum(1 for v in fetched_views.values() if v is not None)
        print(f"\n共 {len(teachers)} 位教師，成功 {success} 位")

        if success == 0:
            # 全部失敗通常代表網站擋住或連不上，不覆寫舊資料，並讓 Actions 顯示紅色錯誤
            print("❌ 全部擷取失敗，保留舊資料")
            sys.exit(1)

        cache = merge_into_cache(cache, fetched_views, fetched_keywords, today)
        write_json(CACHE_PATH, cache)

    write_json(DATA_PATH, build_payload(teachers, cache, today))
    print("📄 data.json 已更新")


if __name__ == "__main__":
    main()
