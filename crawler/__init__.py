"""
真实上游适配器：从公开文档站按 ``sitemap`` 发现页面，礼貌抓取后推给接入管道。

**模块划分：**

 - ``sitemap`` ：sitemap 解析、URL 规范化与过滤、``external_id`` 生成（纯函数）
 - ``fetch`` ：``robots.txt`` 规则、按 host 限速、条件请求与重试
 - ``extract`` ：HTML → 正文 / 标题 / 作者 / 更新时间 / canonical

crawler 只负责“把 HTML 变成正文”，不做全角转半角、零宽字符等标准化 ——
那是摄入层 ``app/services/normalize_service.py`` 的职责，避免两套清洗规则漂移。
"""
