"""给主机验收脚本提供仅使用标准库的 HTTPS 控制面请求。"""
import os
import ssl
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, build_opener


def require_https_url(value, label="平台地址"):
    """拒绝明文地址和 URL 内凭据；错误说明不回显可能包含秘密的原始值。"""
    try:
        parsed=urlsplit(value)
        valid=(parsed.scheme=="https" and bool(parsed.hostname) and parsed.username is None
               and parsed.password is None and not parsed.query and not parsed.fragment)
        parsed.port
    except (ValueError,TypeError):
        valid=False
    if not valid:
        raise ValueError(label+"必须使用 HTTPS，且不能包含凭据、查询参数或片段")
    return value.rstrip("/")


class NoRedirect(HTTPRedirectHandler):
    """验收请求直接报告重定向，不把账号 Token 转发到其他地址或 HTTP。"""
    def redirect_request(self,request,response,code,message,headers,new_url):
        return None


def open_https(request,timeout):
    """验证证书后发送请求；优先使用显式 CA 合集，其次使用仓库本地合集。"""
    # 查询参数可出现在业务请求中；它们不改变已验证的目标协议和主机。
    parsed=urlsplit(request.full_url)
    require_https_url(parsed._replace(query="",fragment="").geturl(),"请求地址")
    bundle=Path(__file__).resolve().parents[1]/".tls"/"ca-bundle.pem"
    cafile=os.environ.get("SSL_CERT_FILE") or (str(bundle) if bundle.is_file() else None)
    context=ssl.create_default_context(cafile=cafile)
    opener=build_opener(HTTPSHandler(context=context),NoRedirect())
    return opener.open(request,timeout=timeout)
