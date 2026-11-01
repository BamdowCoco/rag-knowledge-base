"""
业务异常 + 统一异常处理器。

为什么不让 service 直接抛 HTTPException：

  - service 是【业务逻辑】，不该知道 HTTP 的存在。同一个 ingest() 将来还要被
    定时任务 / CLI / 消息消费者调用 —— 它们没有「状态码」这种东西。
  - 「业务错误 → HTTP 状态码」的映射集中放在边界层（本模块），业务错误保持纯粹。

异常类【不带】 status_code —— 那等于把 HTTP 知识又塞回业务层。映射写在下面的表里。
"""
from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse


class AppError(Exception):
    """本项目所有业务异常的基类。"""

    def __init__(self, detail: str = "") -> None:
        """
        用一个可选的 detail 文本构造业务异常。

        :param detail: 对外文案；不传则用子类自己的 docstring（见下方说明）。
        """
        # 没传 detail 就退回到子类自己的文档字符串 —— 所以下面每个异常类的 docstring
        # 就是它对外的默认文案。结尾别写句号，免得各处文案不一致。
        self.detail = detail or (self.__class__.__doc__ or "业务错误").strip()
        super().__init__(self.detail)


class DocumentInProgressError(AppError):
    """该文档正在被另一个请求处理，请稍后重试"""


class ContentConflictError(AppError):
    """要推的内容已经属于另一篇文档"""


class EmptyContentError(AppError):
    """标准化后正文为空"""


class DocumentNotFoundError(AppError):
    """文档不存在"""


class ChunkNotFoundError(AppError):
    """分块不存在"""


class ChunkClaimMismatchError(AppError):
    """认领令牌已失效，可能已被回收后重新认领"""


class ChunkNotProcessingError(AppError):
    """分块不在处理中状态"""


class RetrievalUnavailableError(AppError):
    """检索服务暂不可用"""


# 业务异常 → HTTP 状态码。这张表只存在于这个（HTTP）边界模块里。
_ERROR_STATUS = {
    DocumentNotFoundError: status.HTTP_404_NOT_FOUND,
    ChunkNotFoundError: status.HTTP_404_NOT_FOUND,
    DocumentInProgressError: status.HTTP_409_CONFLICT,
    ContentConflictError: status.HTTP_409_CONFLICT,
    # fencing 失败 / 状态不对都是「你手里的状态过期了」，让调用方重新认领，所以是 409
    ChunkClaimMismatchError: status.HTTP_409_CONFLICT,
    ChunkNotProcessingError: status.HTTP_409_CONFLICT,
    # 检索依赖（Qdrant / embedding）不可用 —— 这是暂时的，503 才让调用方重试
    RetrievalUnavailableError: status.HTTP_503_SERVICE_UNAVAILABLE,
    EmptyContentError: status.HTTP_422_UNPROCESSABLE_CONTENT,
}


def register_exception_handlers(app: FastAPI) -> None:
    """
    把所有 AppError 子类统一转成 ``{"detail": ...}``（与 FastAPI 默认形状一致）。

    :param app: FastAPI 应用实例；处理器会挂到它身上。
    """
    for exc_type, code in _ERROR_STATUS.items():

        async def handler(request: Request, exc: AppError, _code: int = code) -> JSONResponse:
            """
            把一个业务异常转成 JSON 响应。

            :param request: 触发异常的请求（FastAPI 传入；本处理器不需要读它）。
            :param exc: 业务异常实例。
            :param _code: 该异常对应的 HTTP 状态码（默认参数绑定当前循环值）。
            :return: ``{"detail": exc.detail}`` 的 JSON 响应。
            """
            # `_code=code` 用默认参数把当前值绑进来。少了它，闭包会把循环结束后的
            # 最后一个 code 用到所有 handler 上 —— 经典闭包坑。
            return JSONResponse(status_code=_code, content={"detail": exc.detail})

        app.add_exception_handler(exc_type, handler)
