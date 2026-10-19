from tests import isolation  # Must precede application imports.
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import httpx

from qwen_asr_app.smartclass import (
    CAS, PORTAL, SERVICE, SmartClassClient, SmartClassError, LoginRequired,
    current_term, decrypt_platform, encrypt_cas, lesson_groups, media_url, parse_index, private_json,
)


def video(ident, start, end, node, title="光电子学 李老师 教3-310"):
    return {"NewID": ident, "Title": title, "Teachers": "李老师", "StartTime": start,
            "EndTime": end, "NodeName": str(node), "Duration": 2820}


class GroupTests(unittest.TestCase):
    course = {"CourseID": 1, "ClassID": 2, "TeacherID": 3, "CourseName": "光电子学", "TeacherName": "李老师"}

    def test_pair_in_order_and_deduplicate(self):
        a = video("a", "2026-09-03 13:45:00", "2026-09-03 14:30:00", 6)
        b = video("b", "2026-09-03 14:35:00", "2026-09-03 15:20:00", 7)
        groups = lesson_groups([b, a, a], self.course)
        self.assertEqual(len(groups), 1)
        self.assertEqual([v["NewID"] for v in groups[0]["videos"]], ["a", "b"])
        self.assertEqual(groups[0]["course_name"], "光电子学")
        self.assertEqual(groups[0]["duration"], 5640)
        self.assertEqual(groups[0]["id"], lesson_groups([a, b], self.course)[0]["id"])

    def test_do_not_join_across_break_day_or_missing_period(self):
        rows = [video("a", "2026-09-03 14:35:00", "2026-09-03 15:20:00", 7),
                video("b", "2026-09-03 15:35:00", "2026-09-03 16:20:00", 8),
                video("c", "2026-09-04 15:35:00", "2026-09-04 16:20:00", 8),
                video("d", "2026-09-04 16:25:00", "2026-09-04 17:10:00", 10)]
        self.assertEqual([len(g["videos"]) for g in lesson_groups(rows, self.course)], [1, 1, 1, 1])

    def test_four_periods_become_two_notes(self):
        rows = [video(str(i), f"2026-09-03 {8+i:02}:00:00", f"2026-09-03 {8+i:02}:50:00", i+1) for i in range(4)]
        self.assertEqual([len(g["videos"]) for g in lesson_groups(rows, self.course)], [2, 2])

    def test_term_boundary(self):
        self.assertEqual(current_term(datetime(2027, 1, 10)), "2026-2027-1")
        self.assertEqual(current_term(datetime(2027, 2, 10)), "2026-2027-2")
        self.assertEqual(current_term(datetime(2026, 9, 10)), "2026-2027-1")


class ProtocolTests(unittest.TestCase):
    def test_gateway_failure_retries_get_without_losing_query(self):
        seen = []
        def handle(request):
            seen.append(request)
            return httpx.Response(502 if len(seen) < 3 else 200, text='ready')
        client = SmartClassClient(transport=httpx.MockTransport(handle))
        self.addCleanup(client.close)
        with patch('qwen_asr_app.smartclass.time.sleep'):
            result = client.request('GET', PORTAL + '/UserSpace/CourseCenter.aspx', params={'check': '1'})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(len(seen), 3)
        self.assertTrue(all(r.url.params['check'] == '1' for r in seen))

    def test_gateway_retries_are_bounded_and_do_not_repeat_credentials(self):
        for method, code, expected in [('GET', 502, 3), ('GET', 401, 1), ('POST', 502, 1)]:
            seen = []
            def handle(request):
                seen.append(request)
                return httpx.Response(code)
            client = SmartClassClient(transport=httpx.MockTransport(handle))
            try:
                with patch('qwen_asr_app.smartclass.time.sleep'), self.assertRaises(SmartClassError):
                    client.request(method, PORTAL + '/test')
                self.assertEqual(len(seen), expected)
            finally:
                client.close()

    def test_platform_cipher_matches_published_script_vector(self):
        self.assertEqual(decrypt_platform("30597f5d6d50b15923ab9ecd17180292"), "test")
        self.assertEqual(len(encrypt_cas("password")), 32)

    def test_index_is_parsed_as_data_not_executed(self):
        self.assertEqual(parse_index('var Info={"Video1":{"Src":"Video1.mp4"}};')["Video1"]["Src"], "Video1.mp4")
        with self.assertRaises(ValueError):
            parse_index('var Info=(function(){return {}})()')

    def test_media_domain_and_tls_restricted(self):
        self.assertEqual(media_url("https://njuptvod7.smartclass.cn:8010/a/Video1.mp4"), "https://njuptvod7.smartclass.cn:8010/a/Video1.mp4")
        for url in ["http://njuptvod7.smartclass.cn/a", "https://evil.test/a", "https://njuptvod7.smartclass.cn.evil.test/a", "file:///etc/passwd"]:
            with self.assertRaises(SmartClassError):
                media_url(url)

    def test_login_redirects_cookie_scoping_and_no_password_echo(self):
        seen = []
        def handle(request):
            seen.append(request)
            host, path = request.url.host, request.url.path
            if path == "/cas/login":
                return httpx.Response(302, headers={"location": "http://i.njupt.edu.cn/user-login/?service=flow-id"})
            if path == "/user-login/":
                self.assertEqual(request.url.params["service"], "flow-id")
                self.assertEqual(request.url.scheme, "https")
                return httpx.Response(200, text="CAS")
            if path == "/loginrule/tLoginRule/getActiveRule":
                return httpx.Response(200, json={"success": True, "result": {"passwordLoginFlag": 1, "verificationCodeFlag": 2}})
            if path == "/AppLoginAllocation/queryLoginAllocationByService":
                return httpx.Response(200, json={"success": True, "result": {"loginAppId": "app"}})
            if path == "/ssoLogin/login":
                payload = json.loads(request.content)
                self.assertNotIn("test-password", request.content.decode())
                self.assertEqual(payload["username"], encrypt_cas("student"))
                return httpx.Response(200, json={"code": 200, "result": {"token": "sample-tgc"}})
            if path == "/ssoLogin/index":
                self.assertIn("tgc=sample-tgc", request.headers.get("cookie", ""))
                return httpx.Response(302, headers={"location": SERVICE + "?ticket=sample-ticket"})
            if path == "/SystemSpace/Redirect.aspx":
                self.assertNotIn("tgc=", request.headers.get("cookie", ""))
                return httpx.Response(302, headers={"location": "/", "set-cookie": "PowerCreatorCMSAuthenticationID=sample-session; Path=/; Secure"})
            if host == "njupt.smartclass.cn":
                return httpx.Response(200, text='var userInfo=[{"UserID":123,"ID":456,"UserType":2,"Name":"测试用户","SchoolID":478}];')
            raise AssertionError(path)
        with tempfile.TemporaryDirectory() as directory:
            client = SmartClassClient(Path(directory), httpx.MockTransport(handle))
            self.assertEqual(client.login("student", "test-password")["name"], "测试用户")
            client.close()
            self.assertEqual((Path(directory)/"identity.json").stat().st_mode & 0o777, 0o600)
        self.assertTrue(all(r.url.scheme == "https" for r in seen))

    def test_do_not_forward_login_payload_to_other_host(self):
        def handler(request):
            return httpx.Response(307, headers={"location": "https://njupt.smartclass.cn/receive"})
        client = SmartClassClient(transport=httpx.MockTransport(handler))
        with self.assertRaises(SmartClassError):
            client.request("POST", CAS + "/ssoLogin/login", json={"password": "sensitive"})
        client.close()


if __name__ == "__main__":
    unittest.main()
