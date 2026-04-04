from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.firefox.launch(headless=True)
    page = browser.new_page()
    page.goto("https://www.instagram.com/eighthcollege/")
    page.wait_for_timeout(5000)
    page.screenshot(path="test/screenshot.png")
    content = page.content()
    with open("test/content.html", "w") as f:
        f.write(content)
    browser.close()

    print("Screenshot saved to screenshot.png")