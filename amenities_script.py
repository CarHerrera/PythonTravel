import asyncio
import json
import os

from playwright.async_api import async_playwright

HTML_PATH = os.path.abspath("first_html.html")
OUTPUT_PATH = os.path.abspath("app/data/atelier_rooms.json")


async def scrape_rooms():
    """Parse the locally-saved Atelier Playa Mujeres room listing page.

    Note: "See All Inclusive Plan" is a data-toggle="modal" button, not an
    <a href>. The page has zero <a> tags — the plan details only ever show
    up client-side in a modal, so there is no URL to link to. We capture the
    modal's plan text (its bullet list) as `all_inclusive_plan` instead.
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto(f"file://{HTML_PATH}")

        rooms = []
        cards = page.locator(".card.card__fullwidth")
        count = await cards.count()

        for i in range(count):
            card = cards.nth(i)
            name = (await card.locator(".card__header-title").first.text_content() or "").strip()

            details_modal = card.locator('[id^="modal-suiteDetails"]')
            featured = await details_modal.locator(".icon-grid .icon-label__label-container p").all_text_contents()
            more = await details_modal.locator("ul.list-disc li").all_text_contents()
            amenities = [a.strip() for a in featured + more if a.strip()]

            plan_modal = card.locator('[id^="modal-mealPlan"]')
            plan_items = await plan_modal.locator("ul.list-disc li").all_text_contents()
            all_inclusive_plan = [p.strip() for p in plan_items if p.strip()]

            rooms.append({
                "name": name,
                "amenities": amenities,
                "all_inclusive_plan": all_inclusive_plan,
            })

        await browser.close()
        return rooms


async def main():
    rooms = await scrape_rooms()
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(rooms, f, indent=2)

    print(f"Scraped {len(rooms)} rooms -> {OUTPUT_PATH}")
    if rooms:
        first = rooms[0]
        print(f"\nExample - {first['name']}")
        print(f"  amenities ({len(first['amenities'])}): {first['amenities']}")
        print(f"  all_inclusive_plan ({len(first['all_inclusive_plan'])}): {first['all_inclusive_plan']}")


if __name__ == "__main__":
    asyncio.run(main())
