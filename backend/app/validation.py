from fastapi.responses import JSONResponse


async def validation_error_handler(request, exception):
    # 不回显输入中的密码或NaN/Inf，保持可序列化的422错误。
    return JSONResponse(status_code=422, content={'detail': [
        {key: error[key] for key in ('type', 'loc', 'msg') if key in error}
        for error in exception.errors()]})
