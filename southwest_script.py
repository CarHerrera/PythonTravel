import asyncio
from playwright.async_api import async_playwright
import re
import json
import datetime
import os

HOTEL_URL_PATTERN = re.compile(r"/packages/P\d+\.\d+/hotel(?!/)")
ROOM_OPTIONS_PATTERN = re.compile(r"/packages/P\d+\.\d+/roomOptions")
DATA_DIR = "./app/data"
async def practice_basics():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto("https://books.toscrape.com")

        # Screenshot right after load — confirms the page actually rendered
        await page.screenshot(path="01_homepage.png")

        # Get all book titles on the page
        titles = await page.locator(".product_pod h3 a").all_text_contents()
        print(titles)

        # Get all prices
        prices = await page.locator(".price_color").all_text_contents()
        print(prices)

        # Click into the first book
        await page.locator(".product_pod h3 a").first.click()
        await page.wait_for_load_state("networkidle")

        # Screenshot the detail page too
        await page.screenshot(path="02_book_detail.png")

        # Grab detail page content
        stock = await page.locator(".instock").text_content()
        print(stock.strip())

        await browser.close()   

async def practice_forms():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto("https://the-internet.herokuapp.com/login")

        await page.fill("#username", "tomsmith")
        await page.fill("#password", "SuperSecretPassword!")
        await page.click("button[type='submit']")

        await page.wait_for_load_state("networkidle")
        message = await page.locator("#flash").text_content()
        print(message.strip())

        await browser.close()

async def practice_pagination():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto("https://books.toscrape.com")

        all_titles = []
        while True:
            titles = await page.locator(".product_pod h3 a").all_text_contents()
            all_titles.extend(titles)

            next_button = page.locator(".next a")
            if await next_button.count() == 0:
                break  # no more pages
            await next_button.click()
            await page.wait_for_load_state("networkidle")

        print(len(all_titles), "books collected")
        await browser.close()


def extract_room_prices(rooms_json):
    """Build a {roomTypeId: price_info} lookup from packageRoomOptions.
 
    Pricing lives in a separate block from the room descriptions themselves —
    this joins them by roomTypeId. Prefers the rate marked "selected", falls
    back to the first available rate if none is marked.
    """
    price_map = {}
    if not rooms_json:
        return price_map
 
    for option_group in rooms_json.get("packageRoomOptions", []):
        for room_type_entry in option_group.get("roomTypeIds", []):
            room_type_id = room_type_entry.get("roomTypeId")
            rates = room_type_entry.get("roomRateIds", [])
            if not room_type_id or not rates:
                continue
 
            selected = next((r for r in rates if r.get("selected")), rates[0])
            price_diff = selected.get("priceDifference", {}).get("cash", {})
            total = selected.get("package", {}).get("total", {}).get("cash", {})
 
            price_map[room_type_id] = {
                "price_difference": price_diff.get("amount"),
                "total_price": total.get("amount"),
                "currency": price_diff.get("currencyCode") or total.get("currencyCode"),
            }
 
    return price_map
 
 
async def scope_southwest():
    os.makedirs(DATA_DIR, exist_ok=True)
 
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto("https://www.southwest.com/vacations/", wait_until="networkidle")
 
        possible_destinations = ["Cancun", "Los Cabos", "Punta Cana"]
        origin = "BWI"
        departure_date = "10/31"
        return_day = "11/07"
 
        await page.wait_for_timeout(3000)
        await page.fill("#originationAirportCode", origin)
 
        for dest in possible_destinations:
            print(f"STARTING {dest} PARSE")
 
            # Clear any previously-typed destination before entering the new one —
            # otherwise the old text is still in the field and the new type() call
            # just appends to it.
            input_box = page.locator("#destinationAirportCode")
            await input_box.click()
            await input_box.fill("")
            await input_box.type(dest, delay=100)
            await page.get_by_role("listbox").wait_for()
            await page.get_by_role("option").first.click()
            await page.fill("#departureDate", departure_date)
            await page.fill("#returnDate", return_day)
 
            async with page.context.expect_page() as new_page_info:
                await page.locator("#submitVacationsBookingForm").click()
            results_page = await new_page_info.value
            await results_page.wait_for_load_state("networkidle")
 
            titles = await results_page.locator(".name").all_text_contents()
            print(titles)
 
            # ---- Network response capture setup ----
            captured = []
 
            async def handle_response(response):
                if response.request.method != "GET":
                    return
                url = response.url
                try:
                    if HOTEL_URL_PATTERN.search(url) and "reviews" not in url:
                        data = await response.json()
                        if "apiErrors" not in data:
                            captured.append(("hotel", data))
                    elif ROOM_OPTIONS_PATTERN.search(url):
                        data = await response.json()
                        if "apiErrors" not in data:
                            captured.append(("roomOptions", data))
                except Exception:
                    pass  # non-JSON or unrelated response, ignore
 
            results_page.on("response", handle_response)
            # ---- end setup ----
 
            NEXT_PAGE_BUTTON = 'button[aria-label="Next page"]'

            async def get_current_page_number():
                el = results_page.locator('[aria-current="page"]')
                if await el.count() == 0:
                    return None
                value = await el.get_attribute("value")
                return int(value) if value is not None else None

            async def ensure_on_page(expected_page_num, max_advances=10):
                """Southwest's SPA sometimes unwinds pagination history past where
                we actually are — e.g. go_back() after a click that failed/timed
                out before it ever navigated pops the "Next page" history entries
                instead, landing back on page 1. Detect that via the pagination
                control's aria-current and re-click Next until we're back where
                the scraper thinks it is.
                """
                for _ in range(max_advances):
                    # The pagination indicator can lag a beat behind go_back()
                    # while the SPA re-renders, so poll briefly before trusting it.
                    current = None
                    for _ in range(6):
                        current = await get_current_page_number()
                        if current == expected_page_num:
                            return True
                        if current is not None:
                            break
                        await results_page.wait_for_timeout(250)
                    if current is None or current >= expected_page_num:
                        return False
                    next_button = results_page.locator(NEXT_PAGE_BUTTON)
                    if await next_button.count() == 0:
                        return False
                    await next_button.scroll_into_view_if_needed()
                    await next_button.click(force=True, timeout=10000)
                    await results_page.locator(".result-select-button").first.wait_for(
                        state="visible", timeout=15000
                    )
                return await get_current_page_number() == expected_page_num
 
            # One output file per route — keeps re-scraping BWI->CUN from ever
            # touching BWI->PUJ data, and makes it trivial to know what's stale.
            safe_date = departure_date.replace("/", "-")
            output_path = os.path.join(DATA_DIR, f"hotels_{origin}_{dest}_{safe_date}.jsonl")
            scraped_at = datetime.date.today().isoformat()
 
            # Fresh file each run — this route's old data shouldn't linger
            # alongside new data from this cron execution.
            open(output_path, "w").close()
 
            async def scrape_current_page(all_details, seen_names, page_num):
                """Scrape every hotel card visible on the current results page."""
                prices = await results_page.locator(".amount").all_text_contents()
 
                view_buttons = results_page.locator(".result-select-button")
                count = await view_buttons.count()
 
                for i in range(count):
                    success = False
                    last_error = None
 
                    for attempt in range(3):
                        try:
                            captured.clear()
 
                            view_buttons = results_page.locator(".result-select-button")
                            await view_buttons.nth(i).wait_for(state="attached", timeout=10000)
 
                            target = view_buttons.nth(i)
                            await target.scroll_into_view_if_needed()
                            await target.click(force=True, timeout=10000)
 
                            for _ in range(50):
                                if any(t == "hotel" for t, _ in captured):
                                    break
                                await asyncio.sleep(0.1)
 
                            hotel_json = next((d for t, d in captured if t == "hotel"), None)
                            rooms_json = next((d for t, d in captured if t == "roomOptions"), None)
 
                            # Merge pricing (from packageRoomOptions) into each room type
                            room_types_raw = rooms_json.get("roomType") if rooms_json else None
                            price_map = extract_room_prices(rooms_json)
                            room_types = None
                            if room_types_raw:
                                room_types = [
                                    {
                                        "id": rt.get("roomTypeId"),
                                        "name": rt.get("name"),
                                        "description": rt.get("description"),
                                        "amenities": rt.get("amenities"),
                                        "images": [img.get("url") for img in rt.get("images", [])],
                                        **price_map.get(rt.get("roomTypeId"), {}),
                                    }
                                    for rt in room_types_raw
                                ]
 
                            details = {
                                "origin": origin,
                                "destination": dest,
                                "departure_date": departure_date,
                                "return_date": return_day,
                                "scraped_at": scraped_at,
                                "base_cost": prices[i] if i < len(prices) else None,
                                "name": hotel_json.get("name") if hotel_json else None,
                                "description": (
                                    hotel_json["description"][0]["text"]
                                    if hotel_json and hotel_json.get("description")
                                    else None
                                ),
                                "star_rating": hotel_json.get("starRating") if hotel_json else None,
                                "amenities": hotel_json.get("hotelAmenities") if hotel_json else None,
                                "images": (
                                    [img.get("url") for img in hotel_json.get("images", [])]
                                    if hotel_json
                                    else None
                                ),
                                "room_types": room_types,
                            }
 
                            if details["name"] is None:
                                raise RuntimeError("No hotel JSON captured after click")

                            # go_back() sometimes lands the results SPA on an earlier
                            # page instead of the current one, causing the same hotel
                            # to get walked (and written) more than once per run.
                            if details["name"] not in seen_names:
                                seen_names.add(details["name"])
                                all_details.append(details)
                                with open(output_path, "a") as f:
                                    f.write(json.dumps(details) + "\n")
                            else:
                                print(f"Skipping duplicate hotel: {details['name']!r}")

                            success = True
                            break
 
                        except Exception as e:
                            last_error = e
                            print(f"Hotel {i} attempt {attempt + 1} failed: {e}")
                            try:
                                await results_page.go_back()
                                await results_page.locator(".result-select-button").first.wait_for(
                                    state="visible", timeout=5000
                                )
                                if not await ensure_on_page(page_num):
                                    print(f"WARNING: could not resync to page {page_num} after failed click on hotel {i}")
                            except Exception:
                                pass

                    if not success:
                        print(f"Hotel {i} failed after 3 attempts: {last_error}")
                        all_details.append(None)
                    else:
                        try:
                            await results_page.go_back()
                            await results_page.locator(".result-select-button").first.wait_for(
                                state="visible", timeout=10000
                            )
                            if not await ensure_on_page(page_num):
                                print(f"WARNING: could not resync to page {page_num} after scraping hotel {i}")
                        except Exception:
                            pass
 
            # ---- Pagination loop ----
            all_details = []
            seen_names = set()
            page_num = 1

            while True:
                print(f"--- Scraping {dest} results page {page_num} ---")
                await scrape_current_page(all_details, seen_names, page_num)
 
                next_button = results_page.locator(NEXT_PAGE_BUTTON)

                # Same re-render lag as the pagination indicator — give the SPA
                # a moment before concluding there's no next page.
                for _ in range(6):
                    if await next_button.count() > 0:
                        break
                    await results_page.wait_for_timeout(250)

                if await next_button.count() == 0:
                    print("No 'Next page' button found — done.")
                    break
 
                is_disabled_attr = await next_button.get_attribute("disabled")
                classes = await next_button.get_attribute("class") or ""
                if is_disabled_attr is not None or "state-disabled" in classes:
                    print("Next page button is disabled — done.")
                    break
 
                await next_button.scroll_into_view_if_needed()
                await next_button.click(force=True, timeout=10000)
                await results_page.locator(".result-select-button").first.wait_for(
                    state="visible", timeout=15000
                )
                page_num += 1
 
            results_page.remove_listener("response", handle_response)
            await results_page.close()
            print(f"Wrote {output_path} ({len(all_details)} hotels)")
 
        await browser.close()
 
 
if __name__ == "__main__":
    asyncio.run(scope_southwest())