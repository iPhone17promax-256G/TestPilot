import re
from playwright.sync_api import expect


def open_login(page, base_url):
    page.goto(base_url + "/login")
    choice = page.get_by_role("link", name="View in Browser", exact=True)
    expect(choice.or_(page.locator("#input_loginId")).first).to_be_visible()
    if choice.is_visible():
        choice.click()
    expect(page.locator("#input_loginId")).to_be_visible()


def test_login_controls(page, base_url):
    open_login(page, base_url)
    expect(page.locator("#input_loginId")).to_be_visible()
    expect(page.locator("#input_password-input")).to_be_visible()
    expect(page.locator("#saveSetting")).to_be_enabled()


def test_invalid_login_remains_on_login(page, base_url):
    open_login(page, base_url)
    page.locator("#input_loginId").fill("testpilot-nonexistent-user")
    page.locator("#input_password-input").fill("invalid-test-password")
    page.locator("#saveSetting").click()
    expect(page.get_by_text(re.compile(r"invalid", re.IGNORECASE))).to_be_visible()
    expect(page).to_have_url(re.compile(r".*/login.*"))
