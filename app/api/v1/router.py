"""v1 路由聚合：各 endpoints 的 router 在这里拼起来，main.py 只挂这一个。

【按角色挂路由】同一个镜像按 ``APP_ROLE`` 扮演不同进程：

- ``ingest`` 只挂摄入一侧（stats / documents / chunks）；
- ``rag`` 只挂检索与问答（qa）；
- ``all`` 两边都挂（本地默认）。

rag 进程不暴露摄入接口，ingest 进程不暴露检索接口 —— 写路径与读路径的依赖不同
（rag 要 Qdrant / LLM，ingest 要后台循环），混在一个进程里等于让两边互相拖累。
"""
from fastapi import APIRouter

from app.core.config import settings

api_router = APIRouter()

if settings.app_role in ("all", "ingest"):
    from app.api.v1.endpoints import chunks, documents, stats

    # ⚠️ 顺序要紧：stats 必须【先】于 documents 注册。
    #    因为 documents 里有一条 /documents/{document_id}；Starlette 按注册顺序匹配路径，
    #    若它排在前面，GET /documents/stats 会先命中 /documents/{document_id}
    #    （"stats" 当成 id 去转 int 失败）→ 422，stats 路由永远到不了。
    api_router.include_router(stats.router, prefix="/documents", tags=["stats"])
    api_router.include_router(documents.router, prefix="/documents", tags=["documents"])
    # /chunks/* 与 /documents/* 不冲突，放最后即可
    api_router.include_router(chunks.router, prefix="/chunks", tags=["chunks"])

if settings.app_role in ("all", "rag"):
    # 延迟 import：rag 进程没必要加载摄入侧的模块树（反之亦然）
    from app.api.v1.endpoints import demo_auth, qa

    # 访客表单登录：/api/v1/auth/login|check|logout
    api_router.include_router(demo_auth.router, prefix="/auth", tags=["demo-auth"])
    api_router.include_router(qa.router, tags=["qa"])
