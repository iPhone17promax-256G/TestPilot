# 浏览器上下文内的被动网络记录器，无数据库和模型依赖，可放进测试容器。
# 它订阅 Playwright 网络事件，不是代理服务器；不会抓用户其他标签、手机或任意 Python 进程的流量。

"""Bounded, passive network evidence for one owned browser context.

This module also runs in test containers: it has no database/model dependency.
Bodies are captured while the browser is alive, never replayed by this module.
"""
import asyncio
import json
import os
import time
from pathlib import Path
from urllib.parse import urlparse

LEVELS = {"off", "api", "detailed"}
MAX_REQUESTS = 500
MAX_TOTAL_BYTES = 10 * 1024 * 1024
# Reserve bounded space for the body-free index as well as request files.
MAX_DETAIL_BYTES = MAX_TOTAL_BYTES // 2
HEADER_BYTES = 8192
BODY_BYTES = {"off": 0, "api": 64 * 1024, "detailed": 256 * 1024}
TEXT_TYPES = ("text/", "json", "xml", "javascript", "x-www-form-urlencoded")


def validate_level(level):
    if level not in LEVELS:
        raise ValueError("网络记录级别必须为 off、api 或 detailed")
    return level


def origin(url):
    parsed = urlparse(url)
    return (parsed.scheme.lower(), (parsed.hostname or "").lower(), parsed.port or (443 if parsed.scheme == "https" else 80))


def bounded_text(value, maximum):
    raw = str(value).encode("utf-8", errors="replace")
    return raw[:maximum].decode("utf-8", errors="ignore"), len(raw) > maximum


def atomic_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


# 每次 Run/页面观察独立创建，设置请求数、正文与总大小上限；不保存无限增长的抓包。
# 按用户要求主要做会话/来源隔离；请求头和正文可能含业务凭据，不应宣称全面脱敏。
class NetworkRecorder:
    def __init__(self, folder, session_id, target_url, level="api"):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.session_id, self.target = session_id, origin(target_url)
        self.level = validate_level(level)
        self.records, self.pending = {}, {}
        self.contexts, self.jobs = [], set()
        self.bytes = self.filtered = self.dropped = 0
        self.error, self.closed = "", False
        self._last_index_write = 0
        self._flush_index("running")

    # 只记录目标同源流量：api 级别限 XHR/fetch，detailed 扩展资源类型；off 不记录。
    # 因此跨域第三方请求会被过滤，“详细”也不等于整个浏览器或整台电脑的所有包。
    def eligible(self, url, resource_type):
        try:
            if self.level == "off" or origin(url) != self.target:
                return False
        except ValueError:
            return False
        return self.level == "detailed" or resource_type.lower() in {"xhr", "fetch"}

    def _request(self, request):
        if self.closed or self.level == "off":
            return
        if not self.eligible(request.url, request.resource_type):
            self.filtered += 1
            return
        if len(self.records) >= MAX_REQUESTS or self.bytes >= MAX_DETAIL_BYTES:
            self.dropped += 1
            return
        identity = "REQ-" + str(len(self.records) + 1).zfill(4)
        record = dict(id=identity, method=bounded_text(request.method, 32)[0], url=bounded_text(request.url, 4000)[0],
                      resourceType=bounded_text(request.resource_type, 32)[0], startedAt=time.time(), status=None,
                      durationMs=None, state="pending", requestHeaders={}, responseHeaders={},
                      requestBody="", responseBody="", requestBodyNote="", responseBodyNote="尚未收到完整响应")
        try:
            record["pageUrl"] = bounded_text(request.frame.page.url, 4000)[0]
        except Exception:
            record["pageUrl"] = ""
        try:
            # The adapter's header property is bounded before persistence.
            record["requestHeaders"] = self._headers(request.headers)
            data = request.post_data
            if data:
                record["requestBody"], truncated = bounded_text(data, BODY_BYTES[self.level])
                if truncated:
                    record["requestBodyNote"] = "请求正文超过单条上限，已截断"
        except Exception as error:
            record["requestBodyNote"] = "请求正文不可用：" + str(error)[:200]
        steps = self.folder.parent / "steps.json"
        try:
            if steps.is_file() and steps.stat().st_size < 2_000_000:
                active = json.loads(steps.read_text())[-1]
                if active.get("status") == "running":
                    record["step"] = {k: active[k] for k in ["id", "index", "name"] if k in active}
        except (OSError, ValueError, IndexError):
            pass
        self.records[identity] = record
        self.pending[request] = (identity, time.monotonic())
        self._persist(record)

    def _headers(self, headers):
        result, total = {}, 0
        for name, value in headers.items():
            key = bounded_text(name, 200)[0]
            text, _ = bounded_text(value, min(2048, HEADER_BYTES - total))
            total += len(key.encode()) + len(text.encode())
            if total > HEADER_BYTES:
                break
            result[key] = text
        return result

    def _response(self, response):
        item = self.pending.get(response.request)
        if item:
            record = self.records[item[0]]
            record.update(status=response.status, responseHeaders=self._headers(response.headers))
            self._persist(record)

    # 只采有限大小的文字类响应，二进制/流式/超大正文保留摘要和原因。
    def _body_allowed(self, record, size):
        mime = next((v for k, v in record["responseHeaders"].items() if k.lower() == "content-type"), "").lower()
        if not any(kind in mime for kind in TEXT_TYPES) or "event-stream" in mime:
            record["responseBodyNote"] = "二进制或流式响应仅记录摘要"
            return False
        maximum = BODY_BYTES[self.level]
        length = next((v for k, v in record["responseHeaders"].items() if k.lower() == "content-length"), "")
        if size > maximum or (length.isdigit() and int(length) > maximum):
            record["responseBodyNote"] = "响应正文超过单条上限，未读取"
            return False
        if self.bytes + maximum > MAX_DETAIL_BYTES:
            record["responseBodyNote"] = "会话记录已达到大小上限，未读取正文"
            return False
        return True

    def _finished(self, request, body=None, note="", failed=False):
        item = self.pending.pop(request, None)
        if not item:
            return
        record = self.records[item[0]]
        elapsed = (time.monotonic() - item[1]) * 1000
        try:
            if request.timing.get("responseEnd", -1) >= 0:
                elapsed = request.timing["responseEnd"]
        except Exception:
            pass
        record.update(durationMs=round(elapsed, 2),
                      state="failed" if failed else "finished")
        if body is not None:
            record["responseBody"], truncated = bounded_text(body.decode("utf-8", errors="replace"), BODY_BYTES[self.level])
            record["responseBodyNote"] = "响应正文超过单条上限，已截断" if truncated else ""
        elif note:
            record["responseBodyNote"] = note
        if failed:
            record["error"] = str(request.failure or "网络请求失败")[:500]
        self._persist(record)

    def _persist(self, record):
        path = self.folder / (record["id"] + ".json")
        value = json.dumps(record, ensure_ascii=False).encode()
        old = path.stat().st_size if path.exists() else 0
        if self.bytes - old + len(value) > MAX_DETAIL_BYTES:
            record.update(requestBody="", responseBody="", requestBodyNote="会话记录已达到大小上限，正文未保存",
                          responseBodyNote="会话记录已达到大小上限，正文未保存")
            value = json.dumps(record, ensure_ascii=False).encode()
        if self.bytes - old + len(value) <= MAX_DETAIL_BYTES:
            atomic_json(path, record)
            self.bytes += len(value) - old
        else:
            self.error = "会话记录已达到大小上限，部分详情未保存"
        self._flush_index("running")

    def _flush_index(self, status):
        now = time.monotonic()
        if status == "running" and now - self._last_index_write < 0.25:
            return
        # Bodies and headers never enter the index or an automatic tool result.
        rows = [{k: r[k] for k in ["id", "method", "url", "resourceType", "status", "durationMs", "state", "startedAt", "pageUrl"]}
                for r in self.records.values()]
        index = dict(sessionId=self.session_id, level=self.level,
            status=status, requests=rows, filtered=self.filtered, dropped=self.dropped,
            bytes=self.bytes, error=self.error,
            limits=dict(requests=MAX_REQUESTS, bodyBytes=BODY_BYTES[self.level], totalBytes=MAX_TOTAL_BYTES))
        for _ in range(3):
            index["bytes"] = self.bytes + len(json.dumps(index, ensure_ascii=False).encode())
        atomic_json(self.folder / "index.json", index)
        self._last_index_write = now

    # 供 pytest 同步 Playwright 使用，事件回调只观察，不拦截/修改/重放请求。
    def bind_sync(self, context):
        if self.level == "off":
            return
        def finished(request):
            item = self.pending.get(request)
            if not item:
                return
            record = self.records[item[0]]
            try:
                response = request.response()
                if response and self._body_allowed(record, request.sizes().get("responseBodySize", 0)):
                    self._finished(request, response.body())
                else:
                    self._finished(request)
            except Exception as error:
                self._finished(request, note="响应正文不可用：" + str(error)[:200])
        def failed(request):
            self._finished(request, note="请求失败，未收到完整响应", failed=True)
        self._bind(context, {"request": self._request, "response": self._response,
                             "requestfinished": finished, "requestfailed": failed})

    # 供 browser-use 旁路观察的异步 Playwright 使用；跟踪正文读取任务，收尾时有界等待。
    def bind_async(self, context):
        if self.level == "off":
            return
        async def finished(request):
            item = self.pending.get(request)
            if not item:
                return
            record = self.records[item[0]]
            try:
                response = await request.response()
                sizes = await request.sizes()
                if response and self._body_allowed(record, sizes.get("responseBodySize", 0)):
                    self._finished(request, await response.body())
                else:
                    self._finished(request)
            except Exception as error:
                self._finished(request, note="响应正文不可用：" + str(error)[:200])
        def schedule(request):
            if request not in self.pending:
                return
            if len(self.jobs) >= 8:
                self._finished(request, note="采集并发达到上限，仅记录摘要")
                return
            job = asyncio.create_task(finished(request))
            self.jobs.add(job)
            job.add_done_callback(self.jobs.discard)
        def failed(request):
            self._finished(request, note="请求失败，未收到完整响应", failed=True)
        self._bind(context, {"request": self._request, "response": self._response,
                             "requestfinished": schedule, "requestfailed": failed})

    def _bind(self, context, handlers):
        for name, handler in handlers.items():
            def safe(value, operation=handler):
                try:
                    operation(value)
                except Exception as error:
                    self.error = "部分网络记录不可用：" + str(error)[:200]
            context.on(name, safe)
            self.contexts.append((context, name, safe))

    def close(self):
        self.closed = True
        for context, name, handler in self.contexts:
            try:
                context.remove_listener(name, handler)
            except Exception:
                # pytest closes its browser fixtures before sessionfinish.
                # Unsubscribing from a disconnected browser must not prevent
                # the index or the real test results from being written.
                pass
        self.contexts.clear()
        for request in list(self.pending):
            identity = self.pending[request][0]
            self._finished(request, note="会话结束时请求尚未完成")
            self.records[identity]["state"] = "incomplete"
            self._persist(self.records[identity])
        self._flush_index("completed")

    async def aclose(self):
        if self.jobs:
            done, pending = await asyncio.wait(self.jobs, timeout=3)
            for job in pending:
                job.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        self.close()
