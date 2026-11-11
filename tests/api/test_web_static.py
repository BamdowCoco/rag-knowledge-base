"""
演示前端静态页：``GET /``、``/admin.html``、``/about.html``、``/help.html``
与分层资源都能取到。

测试环境 ``APP_ROLE=all``（见 compose 的 test 服务），``web/`` 由 Dockerfile 的 test stage
继承 runtime stage 一起拷进镜像，所以 ``StaticFiles`` 的挂载在测试里是生效的。

**只做冒烟断言：** 页面正文包含各自的关键字、资源返回 200 —— 前端视觉与交互不在这里测。
"""


async def test_index_page(client):
    """聊天页挂在 ``/``（``html=True`` 时由 index.html 兜底）。"""
    response = await client.get("/")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "rag" in response.text.lower()


async def test_admin_page(client):
    """运维页 ``/admin.html``。"""
    response = await client.get("/admin.html")

    assert response.status_code == 200
    assert "运维" in response.text


async def test_about_page(client):
    """关于页 ``/about.html``。"""
    response = await client.get("/about.html")

    assert response.status_code == 200
    assert "关于" in response.text


async def test_help_page(client):
    """免认证的访问说明页 ``/help.html``（公网经 Caddy 映射到 ``/demo/help.html``）。"""
    response = await client.get("/help.html")

    assert response.status_code == 200
    assert "演示访问说明" in response.text


async def test_static_assets(client):
    """分层的 CSS / JS 也要能按路径取到（目录结构与页面里的引用一致）。"""
    css = await client.get("/css/tokens.css")
    assert css.status_code == 200

    js = await client.get("/js/chat.js")
    assert js.status_code == 200
