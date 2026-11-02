"""
crawler 的单元测试。

**不访问真实网络：** 页面与 sitemap 用内联 fixture，HTTP 用 ``httpx.MockTransport`` 。
真实抓取只在验收时用 ``CRAWL_MAX_PAGES`` 小规模跑。
"""
