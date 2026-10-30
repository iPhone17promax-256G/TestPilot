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
# 总采集大小上限同时计入 index 索引和每个请求详情文件；索引不含正文，也仍需预留空间，不能只限制响应正文而让索引无限增长。
MAX_DETAIL_BYTES = MAX_TOTAL_BYTES // 2
HEADER_BYTES = 8192
BODY_BYTES = {"off": 0, "api": 64 * 1024, "detailed": 256 * 1024}
TEXT_TYPES = ("text/", "json", "xml", "javascript", "x-www-form-urlencoded")
ASYNC_DRAIN_TIMEOUT = 3


# 功能：校验浏览器网络采集级别是否是项目支持的固定值。
# 输入：level 为 off/api/detailed，来自用户设置、任务策略或运行配置。
# 输出/影响：返回原级别字符串；非法值抛 ValueError，不建立采集器或写数据。
def validate_level(level):
    if level not in LEVELS:
        raise ValueError("网络记录级别必须为 off、api 或 detailed")
    return level


# 功能：把 URL 转为协议、主机、有效端口三元组，用于同源范围判断。
# 输入：url 为目标环境地址或浏览器请求地址；省略端口时 HTTP/HTTPS 分别使用 80/443。
# 输出/影响：返回小写协议/主机及端口元组，不发网络请求；非法端口可由 urlparse 抛错。
def origin(url):
    parsed = urlparse(url)
    return (parsed.scheme.lower(), (parsed.hostname or "").lower(), parsed.port or (443 if parsed.scheme == "https" else 80))


# 功能：按 UTF-8 字节数限制证据文本大小，同时避免截断后残留半个字符。
# 输入：value 为可转成文本的数据；maximum 为允许的最多字节数。
# 输出/影响：返回 (限长文本, 是否截断)；只处理内存，不改原数据、不写文件。
def bounded_text(value, maximum):
    raw = str(value).encode("utf-8", errors="replace")
    return raw[:maximum].decode("utf-8", errors="ignore"), len(raw) > maximum


# 功能：先写同目录临时 JSON 再替换目标文件，减少读取到半份证据的可能。
# 输入：path 为目标 Path；value 为可序列化证据字典，由详情或索引保存逻辑传入。
# 输出/影响：返回 None；创建临时 .tmp 并替换目标文件，保留中文，不直接写数据库。
def atomic_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


# 每次 Run/页面观察独立创建，设置请求数、正文与总大小上限；不保存无限增长的抓包。
# 按用户要求主要做会话/来源隔离；请求头和正文可能含业务凭据，不应宣称全面脱敏。
class NetworkRecorder:
    # 功能：建立一个浏览器观察/测试专属的被动采集器，并初始化有限的内存和文件索引。
    # 输入：folder 为该会话 network 目录；session_id 为 NET ID；target_url 决定同源范围；level 默认 api。
    # 输出/影响：构造返回 None；创建目录和 running 索引，初始化请求/监听/异步任务集合，不自动绑定浏览器。
    def __init__(self, folder, session_id, target_url, level="api"):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.session_id, self.target = session_id, origin(target_url)
        self.level = validate_level(level)
        self.records, self.pending = {}, {}
        self.contexts, self.jobs = [], set()
        self.bytes = self.filtered = self.dropped = 0
        self.error, self.closed = "", False
        self._drain_event = None
        self._last_index_write = 0
        self._flush_index("running")

    # 只记录目标同源流量：api 级别限 XHR/fetch，detailed 扩展资源类型；off 不记录。
    # 因此跨域第三方请求会被过滤，“详细”也不等于整个浏览器或整台电脑的所有包。
    # 功能：判断请求是否在当前采集器允许的同源与资源类型范围内。
    # 输入：url 为请求 URL；resource_type 为 Playwright 请求类型；目标 origin 和 level 保存在 self。
    # 输出/影响：返回布尔值；api 只采 XHR/fetch，detailed 扩展同源资源，off 或跨源拒绝，只做内存判断。
    def eligible(self, url, resource_type):
        try:
            if self.level == "off" or origin(url) != self.target:
                return False
        except ValueError:
            return False
        return self.level == "detailed" or resource_type.lower() in {"xhr", "fetch"}

    # 功能：处理 Playwright 请求开始事件，登记限量的请求元信息、头、正文和当前动作关联。
    # 输入：request 是已绑定上下文传来的 Playwright Request；级别/目标/限额来自 self。
    # 输出/影响：返回 None；维护 records/pending 并写请求详情；被过滤或超限仅计数，不拦截、不修改或重放请求。
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
            # 适配器提供的请求头/响应头在写文件前限制字段数量和长度；这是大小限制，不是脱敏，Authorization、Cookie 等业务凭据仍可能保留在请求详情中。
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

    # 功能：把请求/响应头截成可控体积，避免单条证据无限增长。
    # 输入：headers 为 Playwright 给出的头字典；字段名、字段值和总字节数按模块常量限制。
    # 输出/影响：返回限长头字典，不修改原头或请求；这是大小控制，不会自动隐藏 Authorization/Cookie。
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

    # 功能：处理响应头到达事件，补上对应请求的状态码与响应头。
    # 输入：response 为 Playwright Response；通过 response.request 查找 self.pending 中的请求。
    # 输出/影响：返回 None；更新内存记录并保存详情，没有已登记请求则忽略，此时还不读取正文。
    def _response(self, response):
        item = self.pending.get(response.request)
        if item:
            record = self.records[item[0]]
            record.update(status=response.status, responseHeaders=self._headers(response.headers))
            self._persist(record)

    # 只采有限大小的文字类响应，二进制/流式/超大正文保留摘要和原因。
    # 功能：在读取响应正文前判断类型和大小是否符合当前采集限额。
    # 输入：record 为已收到响应头的请求记录；size 为 Playwright 报告的响应正文大小。
    # 输出/影响：返回布尔值；被拒绝时在 record 写说明，不读取二进制、流式、超限正文，不发额外请求。
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

    # 功能：请求结束时合并耗时、正文或失败原因，并从待完成集合移除。
    # 输入：request 为原请求；body 为可选响应字节；note 为无正文说明；failed 表示网络请求失败。
    # 输出/影响：返回 None；更新 records/pending 并写详情，正文受限；状态依据真实事件，不把 HTTP 错误当连接失败。
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
        self._notify_drain()

    # 功能：已有请求或正文任务的状态变化后，唤醒正在等待收尾的异步采集器。
    # 输入：无额外参数；_drain_event 只在 aclose 的当前事件循环中创建和使用。
    # 输出/影响：返回 None；不读取正文、不写文件，没有开始异步收尾时不做任何事。
    def _notify_drain(self):
        if self._drain_event is not None:
            self._drain_event.set()

    # 功能：按会话详情总量限制保存单个请求记录，超限优先去掉正文。
    # 输入：record 是 self.records 中含 id 的请求字典；self.bytes 跟踪已有详情文件体积。
    # 输出/影响：返回 None；原子写 REQ-编号.json 并刷新索引，仍超限则保留错误说明，不写数据库。
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

    # 功能：保存不含请求头/正文的概要索引，让页面和 Agent 能低成本分页查询。
    # 输入：status 为 running/completed；概要、限额和计数从 self 内存获取，running 写入间隔至少 0.25 秒。
    # 输出/影响：返回 None；原子写 index.json，并统计索引与详情总体积；只保存有限请求概要。
    def _flush_index(self, status):
        now = time.monotonic()
        if status == "running" and now - self._last_index_write < 0.25:
            return
        # index.json 及自动返回的采集摘要只含请求概要，不含请求头和正文；详情另存请求文件，Agent 需调用 network_read 按需读取。
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
    # 功能：为 pytest 的同步 Playwright BrowserContext 订阅被动网络事件。
    # 输入：context 是本次 Web 测试独有的上下文；off 级别不绑定，事件方法读取 self 的范围和限额。
    # 输出/影响：返回 None；登记 request/response/finished/failed 监听，回调保存证据，不拦截或修改流量。
    def bind_sync(self, context):
        if self.level == "off":
            return
        # 功能：同步 requestfinished 回调，尝试在允许范围内取响应正文再完成记录。
        # 输入：request 是 Playwright 传入的已结束请求；self 来自 bind_sync 外层采集器。
        # 输出/影响：返回 None；只对已登记请求读取 response/body 并调用 _finished 保存，读取失败写说明，不重发请求。
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
        # 功能：同步 requestfailed 回调，标记未收到完整响应的连接失败。
        # 输入：request 是 Playwright 传入的失败请求；self 为外层采集器。
        # 输出/影响：返回 None；调用 _finished 更新文件和计数，记录真实失败原因，不改变测试判定。
        def failed(request):
            self._finished(request, note="请求失败，未收到完整响应", failed=True)
        self._bind(context, {"request": self._request, "response": self._response,
                             "requestfinished": finished, "requestfailed": failed})

    # 供 browser-use 旁路观察的异步 Playwright 使用；跟踪正文读取任务，收尾时有界等待。
    # 功能：为 browser-use 的异步 Playwright 旁路上下文订阅网络事件，并限制正文读取并发。
    # 输入：context 是本次独占浏览器的异步上下文；self 提供会话范围、级别和 jobs 集合。
    # 输出/影响：返回 None；绑定被动监听并跟踪异步读取任务，off 不绑定，不导航或关闭页面。
    def bind_async(self, context):
        if self.level == "off":
            return
        # 功能：异步读取一个已完成请求的可允许响应正文，供 schedule 创建的任务执行。
        # 输入：request 是已登记的异步 Playwright 请求；self 为外层采集器，大小来自 request.sizes。
        # 输出/影响：异步返回 None；调用 _finished 保存证据，异常转正文不可用说明，不重放网络请求。
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
        # 功能：把请求结束事件安排为受控异步正文读取任务，避免采集堆积。
        # 输入：request 是 requestfinished 事件给出的请求；finished/self.jobs 来自 bind_async 闭包。
        # 输出/影响：返回 None；最多同时 8 个读取任务，超出仅保存摘要；完成后自动从 jobs 移除。
        def schedule(request):
            if request not in self.pending:
                return
            if len(self.jobs) >= 8:
                self._finished(request, note="采集并发达到上限，仅记录摘要")
                return
            job = asyncio.create_task(finished(request))
            self.jobs.add(job)
            # 功能：正文读取真正结束后从活动任务集合移除，并通知收尾流程重新检查。
            # 输入：finished_job 是本次 schedule 创建的 asyncio Task，self 来自外层采集器。
            # 输出/影响：返回 None；只更新内存集合和通知，不改变已保存的请求结果。
            def completed(finished_job):
                self.jobs.discard(finished_job)
                self._notify_drain()
            job.add_done_callback(completed)
            self._notify_drain()
        # 功能：异步上下文的 requestfailed 事件处理入口，记录失败摘要。
        # 输入：request 为浏览器实际失败的请求；self 来自外层 bind_async 采集器。
        # 输出/影响：返回 None；调用 _finished 写失败证据，不创建正文读取任务，不影响网络原操作。
        def failed(request):
            self._finished(request, note="请求失败，未收到完整响应", failed=True)
        self._bind(context, {"request": self._request, "response": self._response,
                             "requestfinished": schedule, "requestfailed": failed})

    # 功能：给一个 BrowserContext 注册带错误保护的事件回调，并保存清理句柄。
    # 输入：context 为本次浏览器上下文；handlers 为事件名到处理函数的字典，来自同步/异步绑定入口。
    # 输出/影响：返回 None；调用 context.on 并维护 self.contexts，稍后 close 移除监听，不接管页面控制。
    def _bind(self, context, handlers):
        for name, handler in handlers.items():
            # 功能：隔离网络采集回调错误，防止附加取证错误中断真实测试动作。
            # 输入：value 为 Playwright 事件数据；operation 默认参数固定本轮绑定的 handler，避免循环闭包引用错函数。
            # 输出/影响：返回 None；执行处理器，异常时仅在 self.error 保存限长原因，随后索引可展示采集问题。
            def safe(value, operation=handler):
                try:
                    operation(value)
                except Exception as error:
                    self.error = "部分网络记录不可用：" + str(error)[:200]
            context.on(name, safe)
            self.contexts.append((context, name, safe))

    # 功能：收尾同步采集器，移除监听并将尚未完成请求留作 incomplete 证据。
    # 输入：self 为当前网络会话记录器；contexts/pending 为先前绑定的监听与待结束请求。
    # 输出/影响：返回 None；关闭接收新请求、更新详情和 completed 索引，不关闭浏览器、不删除已有证据。
    def close(self):
        self.closed = True
        for context, name, handler in self.contexts:
            try:
                context.remove_listener(name, handler)
            except Exception:
                # pytest 在 sessionfinish 回调前可能已关闭浏览器 fixture，此时取消事件订阅可能报浏览器已断开。
                # 收尾容忍这种订阅清理失败，避免它阻断后续网络索引写盘，
                # 也避免覆盖真正的测试结果；浏览器先关闭不是断言失败。
                pass
        self.contexts.clear()
        for request in list(self.pending):
            identity = self.pending[request][0]
            self._finished(request, note="会话结束时请求尚未完成")
            self.records[identity]["state"] = "incomplete"
            self._persist(self.records[identity])
        self._flush_index("completed")

    # 功能：停止接收新请求，给已登记请求的结束事件和正文读取留出同一段有界收尾时间。
    # 输入：pending 是已登记但尚未完成的请求，jobs 是当前正文任务；后到的 requestfinished 仍可创建任务。
    # 输出/影响：异步返回 None；全部完成即结束等待，最多等待 3 秒；超时取消正文任务，close 将剩余请求标为 incomplete。
    # 浏览器仍由调用方保持存活。不能只等待调用时的 jobs 快照：旁路 CDP 可能尚未送来 requestfinished。
    async def aclose(self):
        self.closed = True
        loop = asyncio.get_running_loop()
        deadline = loop.time() + ASYNC_DRAIN_TIMEOUT
        self._drain_event = asyncio.Event()
        try:
            # 只等待已接收请求；监听暂不移除，让迟到的响应/结束事件完成这些记录。
            # 由状态变化唤醒，动态加入的正文任务共用原截止时间，不逐批重新等 3 秒。
            while self.pending or self.jobs:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                self._drain_event.clear()
                try:
                    await asyncio.wait_for(self._drain_event.wait(), timeout=remaining)
                except asyncio.TimeoutError:
                    break
        finally:
            # 超时或调用方取消收尾时都取消剩余正文读取；取消完成后才把未完成请求落盘。
            remaining_jobs = list(self.jobs)
            for job in remaining_jobs:
                job.cancel()
            try:
                if remaining_jobs:
                    await asyncio.gather(*remaining_jobs, return_exceptions=True)
            finally:
                self._drain_event = None
                self.close()
