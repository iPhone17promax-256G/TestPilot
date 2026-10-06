from appium.webdriver.common.appiumby import AppiumBy
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

def open_controls(driver):
    wait = WebDriverWait(driver, 12)
    for name in ("Views", "Controls", "1. Light Theme"):
        wait.until(EC.element_to_be_clickable((AppiumBy.ACCESSIBILITY_ID, name))).click()
    return wait

def test_text_and_checkbox(driver):
    wait = open_controls(driver)
    edit = wait.until(EC.visibility_of_element_located((AppiumBy.ID, "io.appium.android.apis:id/edit")))
    edit.clear()
    edit.send_keys("AIQA demo")
    assert edit.text == "AIQA demo"
    checkbox = driver.find_element(AppiumBy.ID, "io.appium.android.apis:id/check1")
    assert checkbox.get_attribute("checked") == "false"
    checkbox.click()
    wait.until(lambda _: checkbox.get_attribute("checked") == "true")
    assert checkbox.get_attribute("checked") == "true"

def test_disabled_button(driver):
    wait = open_controls(driver)
    button = wait.until(EC.visibility_of_element_located((AppiumBy.ID, "io.appium.android.apis:id/button_disabled")))
    assert not button.is_enabled()
