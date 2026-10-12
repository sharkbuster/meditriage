"""前端静态资源的一致性检查。

为什么需要这些测试：
    后端接口有测试覆盖，但前端一旦写错（比如 JS 里引用了已被删掉的元素），
    页面只会白屏或者某个按钮没反应 —— 接口测试全绿也发现不了。
    这里用最朴素的办法兜住最低底线：文件能取到、关键结构在、ID 对得上。

    这个文件不做浏览器渲染测试（那需要 Playwright 之类的重依赖）。
    它只保证"不会因为改名/删元素导致前端静默失效"。
"""
from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from app.main import STATIC_DIR, app


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def test_home_is_patient_page(client):
    """根路径给患者界面，不是自动生成的 JSON 文档。"""
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "门诊预约" in resp.text
    # 患者界面上不该出现技术词汇
    for jargon in ("patient_id", "confidence", "schedule_id", "need_human"):
        assert jargon not in resp.text, f"患者界面不应出现 {jargon}"


def test_static_assets_served(client):
    for path in ("/static/style.css", "/static/app.js", "/favicon.ico"):
        resp = client.get(path)
        assert resp.status_code == 200, f"{path} 取不到"
        assert resp.content, f"{path} 是空文件"


def test_js_references_exist_in_html(client):
    """JS 里 $('#xxx') 引用的 id，必须在 HTML 里真的存在。

    踩过的坑：删了一个下拉框，但 JS 里还在 appendChild，
    结果基础数据加载整个中断，页面看着只是"没数据"，很难查。
    例外：go-all 是 JS 动态创建后立即绑定的，不在 HTML 里。
    """
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    html_ids = set(re.findall(r'id="([\w\-]+)"', html))
    js_ids = set(re.findall(r"\$\(['\"]#([\w\-]+)['\"]\)", js))

    dynamically_created = {"go-all"}
    missing = sorted(js_ids - html_ids - dynamically_created)
    assert not missing, f"JS 引用了 HTML 里不存在的 id：{missing}"


def test_hidden_elements_have_global_rule():
    """[hidden] 必须有 !important 兜底。

    踩过的坑：写 .sheet{display:flex} 以后，hidden 属性被类选择器压制，
    弹层从页面加载起就一直显示、关不掉。
    """
    css = (STATIC_DIR / "style.css").read_text(encoding="utf-8")
    assert re.search(r"\[hidden\]\s*\{\s*display:\s*none\s*!important", css), \
        "[hidden] 缺少 display:none !important 兜底，弹层会关不掉"


def test_booking_flow_is_two_step():
    """预约页应是"先选科室再看号"的结构。

    早期版本把几十条排班一次性铺出来，手机上要滚很久，
    而且条目结构相似，看着像数据重复。
    """
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert 'id="book-step1"' in html, "缺少第一步容器"
    assert 'id="dept-grid"' in html, "缺少科室网格"
    assert 'id="book-step2"' in html, "缺少第二步容器"
    assert "全部科室" not in html, "不应再出现一览式科室下拉框"
