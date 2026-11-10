"""本地门户的协议与浏览器行为检查。"""
import json
import os
import pathlib
import unittest
from urllib.request import Request, urlopen
from playwright.sync_api import sync_playwright, expect
from portal.__main__ import running


class PortalProtocolTests(unittest.TestCase):
    def test_start_and_runtime(self):
        with running() as (base, state):
            with urlopen(base+'/api/runtime') as response:
                self.assertEqual('base', json.load(response)['phase'])

    def test_operator_phase(self):
        with running() as (base, state):
            request=Request(base+'/__control',data=b'{"phase":"changed"}',headers={'Content-Type':'application/json'})
            with urlopen(request) as response:
                self.assertEqual('changed',json.load(response)['phase'])


class BrowserSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.driver=sync_playwright().start()
        cls.browser=cls.driver.chromium.launch(headless=True,args=['--no-sandbox'])

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.driver.stop()

    def setUp(self):
        self.server=running()
        self.base,self.state=self.server.__enter__()
        self.context=self.browser.new_context(accept_downloads=True,viewport={'width':1280,'height':900})
        self.page=self.context.new_page()
        self.errors=[]
        self.page.on('pageerror',lambda error:self.errors.append(str(error)))
        self.page.goto(self.base)
        expect(self.page.locator('#status')).not_to_have_text('页面加载中')

    def tearDown(self):
        self.context.close()
        self.server.__exit__(None,None,None)
        self.assertEqual([],self.errors)

    def test_portal_ready(self):
        expect(self.page.locator('h1')).to_be_visible()
        capture=os.environ.get('PORTAL_CAPTURE_DIR')
        if capture:
            target=pathlib.Path(capture)/(pathlib.Path.cwd().name+'.png')
            self.page.screenshot(path=str(target),full_page=True)

    def test_browser_behavior_1(self):
        self.page.check('#page-all')
        self.page.click('#preview')
        expect(self.page.locator('#members')).to_contain_text('explicit')
        self.assertEqual(4,len(json.loads(self.page.locator('#members').inner_text())['members']))

    def test_browser_behavior_2(self):
        self.page.click('#all-matching')
        self.page.uncheck('[data-id=DOC-01]')
        self.page.click('#preview')
        expect(self.page.locator('#members')).to_contain_text('matching')
        r=json.loads(self.page.locator('#members').inner_text())
        self.assertEqual(10,len(r['members']))
        self.assertNotIn('DOC-01',r['members'])

    def test_browser_behavior_3(self):
        self.page.click('#next')
        self.page.check('[data-id=DOC-05]')
        self.page.click('#preview')
        expect(self.page.locator('#members')).to_contain_text('DOC-05')
        self.assertEqual(['DOC-05'],json.loads(self.page.locator('#members').inner_text())['members'])

    def test_browser_source_edge(self):
        self.page.click('#all-matching')
        self.page.request.post(self.base+'/__control',data={'phase':'new-record'})
        self.page.click('#preview')
        expect(self.page.locator('#members')).to_contain_text('DOC-NEW')
        expect(self.page.locator('#records')).to_have_attribute('data-revision','2')
        self.assertFalse(any(a['name']=='package' for a in self.state['actions']))


if __name__ == "__main__":
    unittest.main()
