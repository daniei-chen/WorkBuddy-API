"""RSC 数据文件被当成页面导航时，要送回对应页面（issue #48）。

## 现象

把 `http://<host>:7864/dashboard/` 存成书签直接访问，移动端 Safari 上地址栏会
变成 `/dashboard/index.txt`，页面显示满屏 `1:"$Sreact.fragment"…` 这种原始 flight
数据，刷新只会继续显示它，必须手工把地址改回 `/`。

链路（报告者已定位到部署产物内）：

  · 客户端路由抓 RSC 数据失败 → router 走兜底分支
    （`Failed to fetch RSC payload … Falling back to browser navigation`）；
  · 兜底做的是一次**文档级**导航，目标正是那个 `.txt` 数据文件；
  · 服务端（本项目的 FastAPI 静态托管）照常把它当静态文件返回，
    `Content-Type: text/plain` → 浏览器按纯文本渲染。

## 修法

文档型请求（`Accept` 含 `text/html`、且**没有** `RSC` 头）命中
`<页>/index.txt` 时 302 回对应页面。客户端真正的数据抓取（`fetch` + `RSC: 1`）
不受影响——它拿到的仍然是 flight 数据。

判据里「对应页面必须存在」这一条不能省：`.txt` 也可能是真实静态文件
（`robots.txt`），那种没有同名页面，不能一并重定向。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi.testclient import TestClient  # noqa: E402

from server import config  # noqa: E402
from server import main as app_main  # noqa: E402

FLIGHT = '1:"$Sreact.fragment"\n2:I[30679,["210","static/chunks/210.js"]]\n'


def _spa_mounted() -> bool:
    return any(getattr(r, 'path', '') == '/{full_path:path}' for r in app_main.app.routes)


class _Fixture:
    """临时静态目录：两个页面 + 各自的 RSC 数据文件 + 一个真实 .txt 文件。"""

    def __enter__(self) -> Path:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        (root / 'index.html').write_text('<html>root</html>', encoding='utf-8')
        (root / 'index.txt').write_text(FLIGHT, encoding='utf-8')
        for page in ('dashboard', 'settings'):
            d = root / page
            d.mkdir()
            (d / 'index.html').write_text(f'<html>{page}</html>', encoding='utf-8')
            (d / 'index.txt').write_text(FLIGHT, encoding='utf-8')
        # 单一文件形态的页面（根页面就是这种：index.html + index.txt）
        (root / 'playground.html').write_text('<html>playground</html>', encoding='utf-8')
        (root / 'playground.txt').write_text(FLIGHT, encoding='utf-8')
        (root / 'robots.txt').write_text('User-agent: *\n', encoding='utf-8')
        self._patch = mock.patch.object(config, 'STATIC_DIR', root)
        self._patch.start()
        return root

    def __exit__(self, *exc: object) -> None:
        self._patch.stop()
        self._tmp.cleanup()


class RscPageForTest(unittest.TestCase):
    """判据本身（不经过 HTTP）。"""

    def test_maps_page_data_to_page(self) -> None:
        with _Fixture():
            self.assertEqual(app_main._rsc_page_for('dashboard/index.txt'), '/dashboard')
            self.assertEqual(app_main._rsc_page_for('settings/index.txt'), '/settings')

    def test_root_data_maps_to_root(self) -> None:
        with _Fixture():
            self.assertEqual(app_main._rsc_page_for('index.txt'), '/')

    def test_single_file_shape_page(self) -> None:
        """`<页>.html` + `<页>.txt` 这种形态（根页面就是）也要认。

        导出产物里两种形态并存：绝大多数页面是 `<页>/index.html`，根页面是
        `index.html` + `index.txt`。只认目录形态的话，单一文件形态的页面在真机上
        会继续显示原始数据。
        """
        with _Fixture():
            self.assertEqual(app_main._rsc_page_for('playground.txt'), '/playground')

    def test_real_txt_file_not_redirected(self) -> None:
        """`robots.txt` 是真实文件、没有同名页面 → 不能重定向。"""
        with _Fixture():
            self.assertIsNone(app_main._rsc_page_for('robots.txt'))

    def test_html_and_other_paths_untouched(self) -> None:
        with _Fixture():
            for p in ('dashboard/index.html', 'dashboard', 'dashboard/', '_next/x.js',
                      'nonexistent/index.txt', ''):
                with self.subTest(path=p):
                    self.assertIsNone(app_main._rsc_page_for(p))

    def test_traversal_is_not_resolved(self) -> None:
        """越界路径不能借这条逻辑变成可访问的页面（沿用 _safe_static_path 的防线）。"""
        with _Fixture():
            for p in ('../index.txt', '../../etc/index.txt', 'dashboard/../../index.txt'):
                with self.subTest(path=p):
                    self.assertIsNone(app_main._rsc_page_for(p))

    def test_document_detection(self) -> None:
        class _Req:
            def __init__(self, headers: dict):
                self.headers = headers

        self.assertTrue(app_main._is_document_request(
            _Req({'accept': 'text/html,application/xhtml+xml;q=0.9'})))
        # 客户端路由抓数据：Accept 不带 text/html
        self.assertFalse(app_main._is_document_request(_Req({'accept': '*/*'})))
        self.assertFalse(app_main._is_document_request(_Req({})))
        # 明确声明是 RSC 抓取时，即使 Accept 里带 text/html 也不重定向
        self.assertFalse(app_main._is_document_request(
            _Req({'accept': 'text/html', 'rsc': '1'})))


@unittest.skipUnless(_spa_mounted(), '静态前端未挂载（本地没跑过前端构建）')
class RscNavigationHttpTest(unittest.TestCase):
    """走一次真实请求：用户在地址栏里访问那个 .txt 会看到什么。"""

    def setUp(self) -> None:
        self._fx = _Fixture()
        self._fx.__enter__()
        self.client = TestClient(app_main.app)

    def tearDown(self) -> None:
        self._fx.__exit__()

    def test_document_navigation_gets_the_page(self) -> None:
        r = self.client.get('/dashboard/index.txt',
                            headers={'accept': 'text/html,application/xhtml+xml'},
                            follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers['location'], '/dashboard')

    def test_root_document_navigation(self) -> None:
        r = self.client.get('/index.txt', headers={'accept': 'text/html'},
                            follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers['location'], '/')

    def test_single_file_shape_navigation_http(self) -> None:
        """单一文件形态的页面，走真实请求也送回页面。"""
        r = self.client.get('/playground.txt', headers={'accept': 'text/html'},
                            follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers['location'], '/playground')

    def test_client_side_fetch_still_gets_flight_data(self) -> None:
        """客户端路由抓数据必须照旧拿到 flight 文本，否则面板整站路由会坏。"""
        r = self.client.get('/dashboard/index.txt',
                            headers={'accept': '*/*', 'rsc': '1'})
        self.assertEqual(r.status_code, 200)
        self.assertIn('$Sreact.fragment', r.text)

    def test_plain_accept_without_rsc_header_untouched(self) -> None:
        """没有 RSC 头也没有 text/html（自动化脚本/curl）：不重定向，原样返回。"""
        r = self.client.get('/dashboard/index.txt', headers={'accept': '*/*'},
                            follow_redirects=False)
        self.assertEqual(r.status_code, 200)

    def test_real_txt_served_normally(self) -> None:
        r = self.client.get('/robots.txt', headers={'accept': 'text/html'},
                            follow_redirects=False)
        self.assertEqual(r.status_code, 200)

    def test_page_itself_unchanged(self) -> None:
        r = self.client.get('/dashboard/', headers={'accept': 'text/html'})
        self.assertEqual(r.status_code, 200)
        self.assertIn('dashboard', r.text)


if __name__ == '__main__':
    unittest.main()
