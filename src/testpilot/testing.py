# 学习导航：Test 从本文件 case_save 的 Test: Step1 开始；Agent 从 task_start 的 Agent: Step1 开始。
# 每个标注块末尾给出下一步；编号是阅读顺序，不是用例动作序号，也不表示全程同进程调用。
# 核心业务层：用例版本、执行记录和对话状态都从这里组织。
# 阅读顺序建议 case_save → run_start → task_start/task_message，再看 runners 和 agent 的实际执行。

"""Shared business operations for the UI, Agent, CLI and MCP."""
import ast
import hashlib
import json
import os
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from urllib.parse import urlparse
from pathlib import Path
from .workspace import current_project
from .storage import assert_project
from .config import settings
from .assets import validate_assets, version_hash
from .performance import PerformanceConfig
from .assets import AppConfig
from .storage import Mutation, Record, list_records, now, read_record, session, uid, update_record
from .job_queue import enqueue, get_job

TERMINAL = {"passed", "failed", "error", "cancelled", "timed_out", "interrupted", "completed"}


# 功能：生成业务记录显示用的 UTC 时间文本，统一用在版本、消息和执行记录中。
# 输入：无显式参数；从 storage.now 取得当前时间。
# 输出/影响：返回精确到秒且以 Z 结尾的字符串，不直接读写数据库或文件。
def stamp():
    return now().isoformat(timespec="seconds") + "Z"


# 所有业务文件都限制在 settings.data 下，先 resolve 再检查，防止 ../ 或链接逃逸。
# 本地执行子进程缺少某些平台文件时按需下载；平台数据库记录与磁盘正文是分开的。
# 功能：把业务相对路径解析为受数据目录约束的实际路径，防止路径逃逸。
# 输入：relative 是相对 settings.data 的业务文件路径；fetch 为 True 时允许执行器按需下载缺少的受支持文件。
# 输出/影响：返回 pathlib.Path；越界抛 ValueError，executor 分支可能经 replica 下载文件到本任务本地数据目录。
def safe_path(relative, fetch=True):
    root = settings.data.resolve()
    target = (root / relative).resolve()
    if not target.is_relative_to(root):
        raise ValueError("路径超出工作区")
    if fetch and settings.role=="executor" and settings.executor_claim and not target.exists() and relative.startswith(("cases/","attachments/","sources/","applications/","apps/","runs/","inspections/")) and Path(relative).suffix:
        from .replica import download
        download(relative)
    return target


# 先写临时文件再替换目标，避免读到只写了一半的内容。
# 文件系统替换和数据库提交不是同一个事务，不能当成跨两种存储的原子提交。
# 功能：原子替换一个业务文本文件，避免并发读取只写到一半的内容。
# 输入：relative 为数据目录下的相对路径，content 为完整文本；用于保存版本源码及辅助文件。
# 输出/影响：无返回值；创建父目录、写 UTF-8 临时文件并替换目标文件，不把磁盘写入纳入数据库事务。
def write_file(relative, content):
    target = safe_path(relative, fetch=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + "." + uid("TMP"))
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, target)


# 功能：在用例元信息中补充各版本源码，形成前端和工具可直接展示的完整用例。
# 输入：payload 是数据库中的用例字典，各 revision.path 指向数据目录内的源码文件。
# 输出/影响：返回新用例字典，revisions 中加入 code；原 payload 不原地修改，读取文件可能触发 safe_path 的执行器下载。
def hydrate_case(payload):
    item = dict(payload)
    item["revisions"] = [dict(r, code=safe_path(r["path"]).read_text()) for r in item["revisions"]]
    return item


# Case 是用例身份，revision 是这个用例某一次保存的内容。
# 运行必须选定具体版本，后续编辑用例不会改变历史运行的输入。
# 功能：按固定版本号读取用例及其版本元信息，确保一次 Run 使用确定的执行输入。
# 输入：case_id 为当前项目的用例 ID，number 为从 1 开始的已保存版本号。
# 输出/影响：返回 (用例字典, 版本字典)；用例归属由 read_record 校验，版本不存在抛 ValueError，本函数不执行测试。
def revision(case_id, number):
    item = read_record(case_id, "case")
    rev = next((r for r in item["revisions"] if r["number"] == number), None)
    if rev is None:
        raise ValueError("找不到用例版本")
    return item, rev


# 给可能被重复提交的写操作加幂等键：同键同参数取已有结果，同键不同参数拒绝。
# 数据库唯一约束处理并发撞键；没有传 key 的请求不会获得这项去重保证。
# 本地 HTTP 同步和外部工具副作用还有各自边界，不能把它概括成全系统“恰好执行一次”。
# 辅助块：统一事务与幂等。scope 是操作名，key 是调用去重键，arguments 用于内容摘要，action 是实际写入回调。
# 输入/输出：mutate 把当前 SQLAlchemy Session 作为 db 传给 action(db)，并返回它的结果；失败由事务回滚。
# 存储：有 key 时写 mutations 的幂等记录；平台使用 MySQL，执行器业务会话经 replica 的 SQLite/HTTP 路径。
# 边界：空 key 不去重；同键不同参数拒绝；文件和外部工具副作用不属于数据库原子事务。
# 功能：为一项业务写操作统一提供数据库事务、项目级幂等校验和并发撞键后的结果复用。
# 输入：scope 为操作名；key 为请求去重键，空值不去重；arguments 用于摘要比较；action 是接收当前 Session 的写入回调。
# 输出/影响：返回 action(db) 的结果；必要时写 mutations 缓存及回调业务记录，异常回滚数据库；文件和外部工具副作用不自动回滚。
def mutate(scope, key, arguments, action):
    # 第一块：给“本项目的这次写操作”确定身份。
    # scope 加项目 ID，key 是操作编号，arguments 排序后计算 SHA-256 摘要来核对重试参数；摘要用于比较，不是加密密钥。
    # 例如同一个 runs_start 请求重试要复用 key，确实再跑一次则使用新 key。
    scope = current_project()+":"+scope
    fingerprint = hashlib.sha256(json.dumps(arguments, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    # 功能：查本次操作是否已经办理，并核对重试参数。
    # 输入：db 是当前事务中的 SQLAlchemy Session；项目操作类别 scope、幂等键 key 和参数摘要 fingerprint 来自外层闭包。
    # 输出/影响：找到匹配记录就返回 mutations.result，未找到为 None；相同 key 对应不同参数就报错，不执行写操作。
    def cached(db):
        row = db.scalar(select(Mutation).where(Mutation.scope == scope, Mutation.key == key))
        if row:
            if row.fingerprint != fingerprint:
                raise ValueError("幂等键已用于不同的请求内容")
            return row.result
    try:

        # 第二块：在事务里先查旧结果，没有才执行业务。
        # 有 key 才按操作编号去重；旧结果存在就直接返回，不调用 action。没传 key 时每次都执行 action。
        # 平台 session 正常退出会提交、异常退出会回滚；执行器分支走副本事务，文件和外部请求不随数据库自动撤销。
        with session() as db:
            existing = cached(db) if key else None
            if existing is not None:
                return existing

            # 先用 mutations 的 (scope,key) 唯一约束占住这个操作编号，再开始写业务数据。
            # flush 发出 INSERT 但还不提交，占位的 result 先为空。并发请求即使都查到“没有记录”，也不能同时插入相同编号。
            # 这一步是数据库层面的防重复，不是只靠前面的查询判断。
            reservation = Mutation(scope=scope, key=key, fingerprint=fingerprint, result={}) if key else None
            if reservation:
                db.add(reservation)
                db.flush()

            # 占位成功后才把 db 交给真正的业务回调，例如创建 Task/Job。
            # 回调返回的字典写入 mutations.result；平台路径中它和业务记录一起提交，后续重试读取这一份结果。
            # return 仍会先退出 with 完成事务。缓存是本次提交结果，不是持续刷新的测试状态，最新进度还要查 Run。
            result = action(db)
            if reservation:
                reservation.result = result
            return result

    # 第三块：数据库约束冲突后，原事务先回滚，再开新事务查结果。
    # 若另一份相同请求已经成功提交，就返回它的结果；没有可复用记录或没传 key，则继续抛出异常。
    # 这里捕获的是 IntegrityError，不能把所有数据库失败都当作“别人已经办好了”。
    except IntegrityError:
        if not key:
            raise
        with session() as db:
            existing = cached(db)
            if existing is None:
                raise
            return existing


# 功能：校验原生 Python 用例是否为空、是否超长以及是否语法正确。
# 输入：code 是待保存的源码字符串；此函数是单独的语法校验助手，不能替代四类型 validate_assets。
# 输出/影响：无返回值；不合法抛 ValueError 或 SyntaxError，只解析 AST，不执行源码、不保存文件。
def validate_code(code):
    if not code.strip() or len(code) > 150_000:
        raise ValueError("代码不能为空，且不能超过 150KB")
    ast.parse(code, filename="test_case.py")


# 保存入口同时服务人和 Agent。steps 会先校验并编译成 Python/JMX，然后才生成版本。
# 每个版本保存代码、辅助文件、配置、步骤及 stepMap；内容摘要用于执行前核对。
# Agent 调用还有可视化优先检查，不能仅凭模型说“脚本更方便”就绕过。
# Test: Step1 [开始] 接收可视化步骤并保存用例版本，这是手动编写与 Agent 保存共用的入口。
# 概念：Case（用例）与 revision（版本）是 TestPilot 自己的业务约定。
# Case 的 id 标识同一条用例；revision 固定某次保存的步骤、源码和执行参数。修改后追加版本，历史 Run 仍使用提交时选定的版本。
# steps 是界面编辑出来的动作字典列表；code 是编译得到的 Python/JMX 文本；stepMap 是步骤 ID 对应源码行号的列表，供报告跳转。
# 这里的 config 是这条用例的执行参数，例如负载人数或 APP 设备 ID；进程 settings 在本步骤下方解释，LLM 配置在 Agent: Step3/Step7 解释。
# 职责：把手工或 Agent 编写的用例保存为可追溯版本；相同内容复用版本，内容变化才追加版本。
# 输入：前端保存接口或工具分发器调用。name/kind 是名称与测试类型；description/source 是说明与来源。
# 输入：steps 是可视化步骤列表；有它就编译得到 code。code 是原生源码字符串，可视化入口会传空串。
# 输入：case_id 留空新建，非空修订该用例；entry 是入口文件；files 是辅助文件名→文本内容。
# 输入：config 是负载/应用/设备等运行配置；task_id 留空表示无 Agent 关联，非空则关联对话。
# 输入：key 是写操作幂等键，空值不去重；fallback 记录原生脚本例外理由，steps 路线不使用它。
# 返回：完整用例字典，含 id、名称、版本列表；hydrate_case 从文件读取源码，补到各版本的 code。
# 存储：records(kind=case) 的 payload 保存版本号、hash、steps、stepMap、config、路径；files也保留辅助文本内容。
# 存储：源码与辅助文件写到 settings.data/cases/用例ID/版本号/；关联 Task 的 caseIds 同步更新。
# 边界：标准部署平台写 MySQL，执行器先操作 SQLite 副本再经 HTTP 同步；本块保存/编译，不执行测试。
def case_save(name, kind, code, description="", case_id="", entry="test_case.py", source="手工", task_id="", key="", files=None, config=None, steps=None, fallback=None):
    # 这一块准备保存输入，暂时都在 Python 内存中。files 是“辅助文件名→文本内容”，
    # config 是这条用例的运行参数；mapping 稍后保存可视化步骤与生成源码行号的对应关系。
    # 有 steps 时以步骤为编写来源，code 由编译器生成；没有 steps 时保存用户或 Agent 提交的原生脚本。
    files, config = files or {}, config or {}
    mapping = []

    # 如果这条用例来自 Agent 对话，先读取 Task，核对编写方式是否符合“可视化优先”。
    # 原生脚本需要 native_fallback 检查用户要求或能力缺口，并返回要保存的例外说明；
    # 可视化步骤由 visual_authoring 检查，不能用一个 python 步骤包住整份脚本来绕过规则。
    # 这些函数只是检查能否保存，尚未执行测试。
    if task_id:
        from .authoring import native_fallback, visual_authoring
        task = read_record(task_id,"task")
        if steps is None:
            fallback = native_fallback(kind, code, fallback, task)
        else:
            visual_authoring(steps)

    # 这一块把可视化步骤编译成实际执行文本：compile_steps_details 返回规范化 steps、
    # 生成的 Python/JMX code，以及步骤到源码的 mapping。步骤和脚本因此属于同一份用例。
    # 随后只检查启用步骤所引用的 CSV、上传和 multipart 文件是否在 files 中，
    # 缺少辅助文件就拒绝保存，避免到执行时才发现输入不完整。
    if steps is not None:
        from .steps import compile_steps_details
        steps,code,mapping=compile_steps_details(kind,steps)
        from .step_blocks import active_step_indices
        active = active_step_indices(steps)
        for index,step in enumerate(steps):
            if index not in active: continue
            required = [step["value"]] if step["action"] in {"csv","upload"} else list(step["uploads"].values()) if step["body_type"]=="multipart" else []
            for filename in required:
                if filename not in files: raise ValueError("请在辅助文件 files 中提供 "+filename)

    # 这一块校验最终保存内容：测试类型、入口文件、源码、辅助文件和运行参数都须合法。
    # 同时检查名称，以及要编辑的 Case、要关联的 Task 是否存在并属于当前项目。
    # args 汇集本次保存参数，供外层 mutate 判断重试请求是否与原请求相同。
    entry, config = validate_assets(kind, code, entry, files, config)
    if not name.strip() or len(name) > 120:
        raise ValueError("需要有效的用例名称")
    if case_id:read_record(case_id,"case")
    if task_id:read_record(task_id,"task")
    args = dict(name=name, kind=kind, code=code, description=description, case_id=case_id, entry=entry, task_id=task_id, files=files, config=config, steps=steps, fallback=fallback)

    # 功能：在数据库事务中创建或修订用例，并把用例 ID 关联到 Agent 对话。
    # 输入：db 是 mutate 传来的 SQLAlchemy Session；名称、源码、步骤等读取外层 case_save 的变量。
    # 输出/影响：返回用例元信息，写版本文件并登记 records 修改；数据库提交由 mutate 完成。
    # 概念：SQLAlchemy 是数据库操作库；ORM 用 Record 对象代表 records 表的一行，
    # kind=case/task/run 区分记录类型，payload 存业务 JSON。db 不是 Agent 的聊天会话。
    # 平台标准部署使用 MySQL；执行器复用此函数时先改本地 SQLite 副本，再通过 HTTP 同步。
    # 文件写入和数据库事务是两件事，数据库回滚不会自动撤销已经写出的文件。
    def action(db):
        # 这一块决定“编辑旧用例”还是“新建用例”。提供 case_id 就读取旧记录并请求行锁；
        # 不存在或不属于当前项目便拒绝编辑，不能悄悄变成新建。未提供 ID 才生成长期 TC ID。
        # payload 是准备写回的内存字典；已有用例的测试类型固定，名称和描述可以修改。
        # MySQL 行锁保护并发写入；执行器 SQLite 副本的修改仍须回平台核对合并。
        row = db.scalar(select(Record).where(Record.id == case_id, Record.kind == "case").with_for_update()) if case_id else None
        if case_id and not row:
            raise ValueError("用例不存在")
        if row: assert_project(row)
        payload = dict(row.payload) if row else {"id": uid("TC"), "name": name.strip(), "kind": kind, "description": description, "filename": "plan.jmx" if kind == "performance" else "test_case.py", "source": source, "tags": [], "revisions": []}
        if payload["kind"] != kind:
            raise ValueError("已有用例的执行类型不能修改，请创建另一条用例")
        payload.update(name=name.strip(), description=description)

        # 这一块判断是否需要新版本：计算源码、辅助文件和配置的摘要，再比较入口、步骤及例外说明。
        # 全部执行输入与最新版相同就复用版本；有变化才追加版本，因此修改名称不必复制一份脚本。
        # 这里的摘要用于内容比较，不是加密；版本复用也不同于 mutate 对一次提交请求的去重。
        code_hash = version_hash(code, files, config)
        unchanged = payload["revisions"] and payload["revisions"][-1]["hash"] == code_hash and payload["revisions"][-1]["entry"] == entry and payload["revisions"][-1].get("steps") == steps and payload["revisions"][-1].get("fallback") == fallback
        if not unchanged:

            # 这一块保存新的执行版本：版本号递增，在 cases/用例ID/版本号 下写主文件和辅助文件，
            # 旧目录不覆盖。保存时只写文本，不发接口请求、不操作浏览器，也不运行测试。
            # 版本索引中的 path 指向磁盘主文件；steps、stepMap、config 等放入数据库 JSON。
            # 概念：settings 是 testpilot.config 的进程配置对象，由 TESTPILOT_ 环境变量/.env 等提供初值。
            # settings.data 是当前进程的数据根目录，write_file 会检查边界后解析下面的相对路径。
            # settings.role 标记进程职责：platform 管 API，gateway 领取/管理 Job，executor 执行 Job，
            # legacy 兼容旧运行方式；deployment_mode=local/server 才表示本地或服务端部署。
            # 各进程拥有自己的 settings，不会因为导入同一模块就共享内存；Agent: Step1 也解释配置选择。
            number = len(payload["revisions"]) + 1
            path = f"cases/{payload['id']}/{number}/{payload['filename']}"
            write_file(path, code)
            for filename, content in files.items():
                write_file(f"cases/{payload['id']}/{number}/{filename}", content)
            payload["revisions"] = [*payload["revisions"], {"number": number, "hash": code_hash, "path": path, "entry": entry, "files": files, "config": config, "mode": "steps" if steps is not None else "script", "steps": steps, "stepMap": mapping, "fallback": fallback if steps is None else None, "createdAt": stamp()}]

        # 这一块登记数据库变化：编辑时把 payload 赋回旧 Record，新建时 db.add 一个 kind=case 对象。
        # 它们先进入当前 Session，由外层事务提交后才成为已保存记录，函数此处没有自行 commit。
        if row:
            row.payload = payload
        else:
            db.add(Record(id=payload["id"], kind="case", payload=payload))

        # 如果关联了 Agent 对话，再锁定 Task，把本用例 ID 加到其 caseIds 列表并去重。
        # 这样聊天页可以展示本次对话创建或修订的用例；它不是把测试结果写入 Task。
        # 返回的 payload 仍是用例元信息，源码要由外层 hydrate_case 按版本路径读取。
        if task_id:
            task = db.scalar(select(Record).where(Record.id == task_id).with_for_update())
            if task:
                task.payload = dict(task.payload, caseIds=list(dict.fromkeys([*task.payload.get("caseIds", []), payload["id"]])))
        return payload

    # 最后由 mutate 调用 action(db)，在同一事务中保存数据库修改，并按幂等键 key 去重。
    # 同 key、同参数复用原结果；同 key、不同参数报错；没有 key 就不做请求去重。
    # 参数摘要只是比较用，带 key 的提交及结果保存在 mutations 表。hydrate_case 随后读版本文件，
    # 把元信息补成前端/Agent 可查看的完整用例；返回的是保存结果，真正执行继续沿 Test: Step4。
    return hydrate_case(mutate("cases_save", key, args, action))
    # Test: Step1 [结束] 下一步：Test: Step2 → src/testpilot/steps.py 的 compile_steps_details。
    # 看完 Step2/Step3 后回来确认 action(db) 保存了版本；再去本文件 run_start 的 Test: Step4。


# 功能：创建或更新被测环境，分别保存用户访问地址和测试执行地址。
# 输入：name 为显示名称，url 为浏览器可访问 URL；execution_url 为执行容器使用地址，空值沿用 url；description 为说明，environment_id 非空表示编辑。
# 输出/影响：返回环境字典；在事务中写 records(kind=environment)，检查当前项目归属及无内嵌账号密码的 HTTP/HTTPS 地址。
def environment_save(name, url, execution_url="", description="", environment_id=""):
    for value in [url, execution_url or url]:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("环境地址必须为不带账号密码的 HTTP/HTTPS URL")
    if not name.strip():
        raise ValueError("环境名称不能为空")
    payload = {"id": environment_id or uid("ENV"), "name": name.strip(), "url": url.rstrip("/"), "executionUrl": (execution_url or url).rstrip("/"), "description": description}
    with session() as db:
        row = db.get(Record, payload["id"])
        if row:
            assert_project(row)
            if row.kind != "environment":
                raise ValueError("环境 ID 不正确")
            row.payload = payload
        else:
            db.add(Record(id=payload["id"], kind="environment", payload=payload))
    return payload


# “启动”在业务层的含义是创建 Run 和 Job；真正启动容器发生在 execute_run。
# 此时固定版本号、代码摘要、环境和执行配置快照，确保报告可追溯到当时的输入。
# APP 额外保存应用/设备快照；测试类型不同，但都返回统一的 Run 记录。
# Test: Step4 [开始] 用户点击执行或 Agent 调 runs_start，都先创建 Run 与 test 类型 Job。
# 概念：Run 是“某用例的某版本在某环境上的一次执行记录”，Task 是“一段可续聊的 Agent 对话”，Job 是“让执行器领取的一份作业”。
# 同一个 Case 可以有很多 Run；同一个 Task 可关联很多 Case/Run；Job.record_id 指向它要处理的 Task 或 Run。
# Run 和 Task 都存 MySQL records 表，分别用 kind=run/task；Job 在 Redis 中排队。kind=test/agent 是作业类别，不是进程 role。
# queued 表示已排队；创建这些记录不会立即发请求或运行模型，真正执行要等后面的领取和子进程。
# 职责：固定“哪个用例版本、在哪个环境运行”，生成一次 Run，并放入测试作业队列。
# 输入：前端执行接口或 runs_start 工具传 case_id、revision_number（从1开始）、environment_id。
# 输入：task_id 关联发起的 Agent 对话，空值为独立运行；timeout 是秒，默认120，随后按类型限幅。
# 输入：key 用于提交去重；overrides 只覆盖本次性能/APP 配置，不修改用例版本；None 表示无覆盖。
# 输入：network_capture 为 Web 采集等级 off/api/detailed；None 由任务/默认策略决定，其他类型不使用。
# 返回：status=queued 的 Run 字典，含独立 run_id、版本号、环境/配置快照、执行器和报告占位字段。
# 存储：records 的 kind=run 保存本次输入；jobs 的 kind=test 保存待领取作业；Task.runIds 追加关联。
# 边界：queued 只说明提交成功。平台入队或执行器同步后由独立测试槽领取，此处不发 HTTP 测试请求。
def run_start(case_id, revision_number, environment_id, task_id="", timeout=120, key="", overrides=None, network_capture=None):
    # 这一块固定执行目标：核对可选的 Agent Task，读取 Case 的指定版本和被测环境。
    # item 是用例元信息，rev 是指定版本；本次运行不会随着之后编辑用例而偷偷换成新源码。
    # 概念：environment 是 MM 等被测系统的环境记录，不是 Python 虚拟环境。
    # 其中 url 供人查看，executionUrl 供执行器/容器访问，Docker 网络下两者可能不同。
    # 后面会把环境和配置复制为 Snapshot，即“提交时的值”，用于复现本次运行。
    if task_id:read_record(task_id,"task")
    item, rev = revision(case_id, revision_number)
    env = read_record(environment_id, "environment")

    # 这一块准备运行参数：复制版本 config，再用 overrides 覆盖本次性能/APP 配置；
    # API/Web 不接受非空覆盖项。PerformanceConfig/AppConfig 是 Pydantic 校验类，
    # 用于检查运行参数，不是模型配置，也不会在校验时创建浏览器或 Appium 会话。
    # 性能时限至少包含负载时长加 90 秒；APP 必须有可访问的应用和设备，设备锁等到执行时申请。
    # timeout 是整次测试的秒数，最终限制为 5～600 秒，性能可到 3600 秒；
    # 可视化单步的 timeout 则是毫秒，不能当作同一个时间值。
    config = dict(rev.get("config", {}), **(overrides or {}))
    if item["kind"] == "performance":
        config = PerformanceConfig.model_validate(config).model_dump()
        seconds = sum(x["seconds"] for x in config["stages"]) if config["stages"] else config["duration_seconds"]
        timeout = max(seconds + 90, int(timeout))
    elif item["kind"] == "app":
        config = AppConfig.model_validate(config).model_dump()
        if not config["app_id"] or not config["device_id"]:raise ValueError("APP 运行需要选择应用版本和测试设备")
        read_record(config["app_id"], "application")
        read_record(config["device_id"], "device")
    elif overrides:
        raise ValueError("API/Web 用例没有运行配置覆盖项")
    timeout = max(5, min(int(timeout), 3600 if item["kind"] == "performance" else 600))

    # 这一块确定 Web 浏览器的网络证据等级，其他类型不创建这类采集设置。
    # network_capture 的 off 不记录，api 记录目标同源 XHR/fetch，detailed 扩展到同源资源和更多正文。
    # 同源要求协议、主机、端口一致；用户本轮明确关闭后，Agent 不能自行重新打开。
    # resolve_level 合并任务要求与本次输入；详细证据写独立文件，需另用工具读取，
    # 不会在提交运行时把所有请求塞进模型上下文。args 则汇集去重所需的执行参数。
    from .network import resolve_level
    capture_level = resolve_level(task_id, network_capture) if item["kind"] == "web" else None
    args = dict(case_id=case_id, revision_number=revision_number, environment_id=environment_id, task_id=task_id, timeout=timeout, config=config, network_capture=capture_level)

    # 功能：在事务中登记 Run、test 调度 Job，并关联到可选的 Agent Task。
    # 输入：db 是 mutate 注入的数据库 Session；版本、环境、配置等读取外层已校验的变量。
    # 输出/影响：返回 status=queued 的 Run 字典，写 records/jobs；此处不启动容器、不发送测试请求。
    def action(db):
        # 这一块先在内存中组织运行记录：每次真正复跑都生成新的 RUN ID，以便区分不同报告。
        # payload 固定版本号、源码摘要、环境、配置和超时，结果/产物列表初始为空。
        # queued 只表示已提交、等待领取，不能据此判断测试开始或通过。
        identity = uid("RUN")
        payload = {"id": identity, "caseId": case_id, "revision": revision_number, "status": "queued", "duration": "—", "createdAt": stamp(), "environment": env["name"], "environmentSnapshot": env, "taskId": task_id, "demo": False, "timeout": timeout, "artifacts": [], "items": [], "summary": "等待执行进程领取", "codeHash": rev["hash"], "kind": item["kind"], "configSnapshot": config}

        # 这一块给 Run 绑定执行器：平台优先沿用 Task 的 executorId，独立运行则使用选择结果；
        # 没有可选 ID 就要求先连接执行器。此处取得 ID，并不等于已经确认机器此刻在线。
        # 执行器内部由 Agent 创建 Run 时也保留 Task 的执行器归属，之后通过 HTTP 回传调度记录。
        # 这是进程职责 settings.role 的分支，不是 Agent 工具审批模式。
        if settings.role=="platform":
            from .executors import selected_executor
            payload["executorId"]=read_record(task_id).get("executorId","") if task_id else selected_executor()
            if not payload["executorId"]:raise ValueError("请先连接本地执行器")
        elif task_id:payload["executorId"]=read_record(task_id).get("executorId","")

        # 这一块补齐特定类型的固定输入：Web 保存有效采集等级；
        # APP 保存所选应用和设备的快照，后续执行可以检查安装包及设备是否仍符合要求。
        # 保存快照仍不代表手机已被占用，实际执行和设备清理在 Runner 中完成。
        if item["kind"] == "web":
            payload["networkCaptureLevel"] = capture_level
        if item["kind"] == "app":
            payload["appSnapshot"] = read_record(config["app_id"], "application")
            payload["deviceSnapshot"] = read_record(config["device_id"], "device")

        # 这一块登记两类数据库记录：records 的 kind=run 保存本次运行信息，
        # dispatch_outbox 保存投递事件；提交后由 enqueue 的调度层将 test Job 写入 Redis。
        # 如果来自 Agent，再把 RUN ID 加入 Task.runIds，聊天页才能找到关联报告。
        # db.add 和 payload 赋值先登记在 Session，Run、待投递事件、Task 关联由外层事务一起提交；
        # Job 是队列记录，本身不是线程、进程或正在运行的测试。
        db.add(Record(id=identity, kind="run", status="queued", payload=payload))
        enqueue(db, db.get(Record, identity), "test")
        if task_id:
            task = db.scalar(select(Record).where(Record.id == task_id).with_for_update())
            if task:
                task.payload = dict(task.payload, runIds=[*task.payload.get("runIds", []), identity])
        return payload

    # 最后由 mutate 提交事务并按 key 去重，返回排队中的 Run 给页面或 Agent。
    # 真正执行由常驻执行服务领取 Job 后启动，继续看 Test: Step5，而不是在这个接口中等待测试。
    return mutate("runs_start", key, args, action)
    # Test: Step4 [结束] 下一步：Test: Step5 → src/testpilot/local_executor.py 的 consumer。
    # 这里通过作业记录交接；常驻执行服务随后经 HTTP 领取，不是 run_start 直接调用 consumer。


# 先把取消意图写入状态，执行器的轮询再停止实际容器；不是本 HTTP 线程直接杀容器。
# 功能：保存一次运行的取消意图，供实际执行器轮询后停止操作。
# 输入：run_id 是当前项目的运行 ID，先经 read_record 检查记录类型和访问权限。
# 输出/影响：返回更新后的 Run；非终态写 cancelled/summary 到 records，终态保持原状态；本函数不直接杀容器。
def run_cancel(run_id):
    read_record(run_id,"run")
    return update_record(run_id, lambda p: p.update(status="cancelled", summary="用户取消了本次运行") if p["status"] not in TERMINAL else None)


# Task 是一段 Agent 对话的持久记录，包含用户消息、工具事件、用例和执行关联。
# 首条消息与模型选择一起保存，再创建 agent 类型 Job 让本地执行器领取。
# Agent: Step1 [开始] 把首条用户消息、环境、模型选择保存为 Task，并创建 agent 类型 Job。
# 职责：创建一段可续聊的 Task，保存首条用户消息和本轮配置，提交 Agent 作业。
# 输入：聊天接口/外部工具传 goal（首条目标）与 environment_id（被测环境ID，不是项目目录）。
# 输入：attachment_ids 是已上传附件的ID列表，None 等同空列表；此处保存附件引用，不在这里解析全文。
# 输入：model_profile_id 选择账号模型配置，reasoning_effort 选择支持的思考深度；executor_id 选择执行器。
# 输入：permission_mode 默认 auto，可选 readonly/bypass；network_capture 指定采集等级，None 未显式设置。
# 输入：key 是防重复创建的幂等键；空目标但有附件时生成“先概括附件、不要执行测试”的默认要求。
# 返回：queued 的 Task 字典，id 字段是 TASK ID，还包含首条 messages、审批/工具事件/用例/运行等关联列表。
# 存储：MySQL records 的 kind=task 保存会话与不含密钥的模型选择；Outbox 可靠投递 agent Job，Redis 保存实际调度状态。
# 边界：模型 Key 不写进 Task；平台只保存与派发，执行器稍后加载本机 Key 并进入 Agent 循环。
def task_start(goal, environment_id, key="", attachment_ids=None, network_capture=None, permission_mode="auto", model_profile_id="", reasoning_effort="", executor_id=""):
    # 第一块：检查这段对话允许 Agent 做什么。
    # permission_mode 是工具审批模式：readonly 只读，auto 交给独立 AI 审批，bypass 跳过逐次询问但仍保留账号权限和容器限制。
    # 这里仅检查模式名字是否合法，真正调用工具时还会在 Agent: Step9 检查权限。
    # network_capture 是另一项独立设置，控制浏览器请求证据的 off/api/detailed 等级；None 表示未明确选择，不等于关闭。
    from .attachments import attachment_refs
    from .agent.permissions import validate_mode
    validate_mode(permission_mode)
    from .network_capture import validate_level
    if network_capture is not None: validate_level(network_capture)

    # 第二块：整理用户说了什么、附了哪些文件、要测哪个环境。
    # 附件 ID 经过访问检查后变成名称/ID 等引用，正文留在附件文件里，Agent 用 attachments_read 按需读取；项目共享资料 source 则能供多段对话使用。
    # 这里只有附件时补上“先概括、不要执行”的默认要求；环境记录给出被测地址，空目标或超长消息会被拒绝。
    # 这些值此时还在当前请求的内存里，没有开始运行模型。
    attachments = attachment_refs(attachment_ids or [])
    goal = goal.strip() or ("请阅读附件，概括需求与待确认项，先不要执行测试。" if attachments else "")
    env = read_record(environment_id, "environment")
    if not goal.strip() or len(goal) > 6000:
        raise ValueError("请提供测试目标，最多 6000 字")

    # 第三块：确定后续由哪个执行服务、用哪项模型配置处理这段对话。
    # 概念：settings 是 testpilot.config 加载的当前进程配置，TESTPILOT_ROLE 对应 role，默认 platform。
    # platform 处理 API/保存/入队；gateway 常驻等待通知和管理子进程；executor 执行一份作业；legacy 仅供隔离检查配置使用，旧 Worker 已停用。
    # role 只是代码分支标记，Compose 和 subprocess.Popen 才真正启动不同进程；不同进程的 settings 也不会自动共享。
    # deployment_mode=local/server 另行决定本机免登录或服务端登录部署，两种模式都保留这套进程分工，与账号 admin/member 也不同。
    # 下面的平台分支只校验账号模型、思考深度和执行器，并把模型配置 ID 和执行器 ID 放进 selection；模型 Key 要等 Agent: Step3 在执行端加载。
    selection = {}
    if settings.role == "platform":
        from .models import validate_selection
        from .executors import selected_executor
        chosen = validate_selection(model_profile_id, reasoning_effort)
        executor_id = selected_executor(executor_id)
        if not executor_id: raise ValueError("请先连接本地执行器")
        selection = dict(modelProfileId=chosen["id"], reasoningEffort=reasoning_effort, executorId=executor_id)
    elif not settings.model_key or not settings.model_url:
        raise ValueError("本地执行器没有配置模型 API 地址和密钥")

    # 功能：把这段新对话保存为 Task，同时登记一份待领取的 Agent 作业。
    # 输入：db 是 mutate 提供的数据库 Session；用户文字、附件引用、环境、selection 和权限都从外层函数取得。
    # 输出/影响：返回 queued 的 Task 字典；标准平台路径写 MySQL records 与待投递事件，再投递 Redis；执行器路径写 SQLite 副本并同步，不调用模型。
    def action(db):
        # 这一块先在内存里准备整段对话数据。
        # TASK ID 是长期对话编号；payload 放首条 messages、目标环境和空的用例/运行/工具事件列表。
        # messages 是嵌在 payload 里的列表，之后整体保存到 records.payload 的 JSON 字段，当前不是每条消息单独一行。
        # 消息 role=user 指发言者，不是进程角色；delivered=False 表示还没交给 Agent，之后变 True 也不代表任务完成。
        identity = uid("TASK")
        payload = {"id": identity, "title": goal[:40], "goal": goal, "status": "queued", "step": 0, "caseIds": [], "runIds": [], "events": [], "inspections": [], "findings": [], "environmentId": environment_id, "environment": env["name"], "createdAt": stamp(), "summary": "等待 Agent 领取", "rounds": 0, "messages": [{"id": uid("MSG"), "role": "user", "content": goal, "attachments": attachments, "captureLevel": network_capture, "createdAt": stamp(), "delivered": False}]}

        # 把本轮模型配置 ID、思考深度和执行器 ID 补进对话。
        # 消息中另存模型选择，方便下一轮换模型；执行器绑定留在 Task 层。这里保存模型选择参数，不保存 API Key。
        # permissionVersion/intentVersion 从 0 开始记录权限和用户要求的变化，approvals 保存工具审批；要求变化后旧批准可能失效。
        payload.update(selection)
        payload["messages"][0].update({k:v for k,v in selection.items() if k!="executorId"})
        payload.update(permissionMode=permission_mode, permissionVersion=0, intentVersion=0, approvals=[])

        # 这一块保存两件不同的事：MySQL records 保存“用户说了什么”，enqueue 登记投递事件，提交后 Redis 保存“谁还需要处理这段对话”。
        # Job.record_id 指向 TASK ID；enqueue 还在 Task 中保存 dispatchId，区分同一对话的不同调度轮次。
        # db.add 只是把对象交给 SQLAlchemy，本函数没有自行提交。返回的 payload 给聊天页展示“等待处理”，不是模型回答。
        row=Record(id=identity, kind="task", status="queued", payload=payload)
        db.add(row);enqueue(db,row,"agent")
        return payload

    # 最后交给 mutate 调用 action，并统一处理数据库事务和请求去重。
    # 正常完成后 Task/首条消息与 Outbox 一起提交，再可靠投递 Redis Job；有幂等键的相同重试复用结果，不再新建对话。
    # 这个 HTTP 请求到此只完成保存和排队；下一步 Agent: Step2 的执行服务通过 HTTP 领取 Job，才会继续运行。
    return mutate("tasks_start", key, dict(goal=goal, environment_id=environment_id, attachment_ids=attachment_ids or [], network_capture=network_capture, permission_mode=permission_mode, selection=selection), action)
    # Agent: Step1 [结束] 下一步：Agent: Step2 → src/testpilot/local_executor.py 的 consumer。
    # 后续消息入口是本文件 task_message，仍沿用同一个 Task；第一次学习先跟首条消息。


# 取消对话时让旧审批失效，并请求取消关联测试；旧操作不能在用户停止后重新获批。
# 功能：停止当前 Agent 对话，废止未消费用户消息和旧审批，并请求取消关联运行。
# 输入：task_id 为当前项目的对话 ID；关联 runIds 从 Task 记录读取。
# 输出/影响：返回更新后的 Task；更新 records 中的状态/审批/消息及关联 Run，实际模型或容器停止由执行器后续检查执行。
def task_cancel(task_id):
    read_record(task_id,"task")
    # 功能：按 Task 当前状态写取消标记，并令旧工具审批和待处理用户消息失效。
    # 输入：p 是 update_record 传入的 Task payload 字典；取消原因由本回调固定提供。
    # 输出/影响：无返回值，原地更新 p 的审批、状态、summary 和消息；update_record 负责把更新后的 payload 写入数据库。
    def cancel(p):
        if p["status"] in {"queued","running","waiting_approval","cancelled"}:
            from .agent.permissions import invalidate
            invalidate(p,"用户停止了对话，旧操作不再有效")
            p.update(status="cancelled",summary="用户取消，已有用例和结果已保留")
            p["messages"]=[dict(m,delivered=True,suppressed=True) if m["role"]=="user" and not m.get("delivered") else m for m in p.get("messages",[])]
    task=update_record(task_id,cancel)
    if task["status"]=="cancelled":
        for run_id in task.get("runIds",[]):run_cancel(run_id)
    return task


# 功能：把可恢复的 Agent 对话及其调度作业重新置为排队，供执行器接着已有状态执行。
# 输入：task_id 为当前项目的对话 ID，仅接受 interrupted/error/budget_exhausted/needs_input 的 Task。
# 输出/影响：锁定并更新 Task、登记新一轮投递事件；Redis 新 Job 的领取凭证与旧轮次不同。恢复点由运行时保留，此处不调用模型。
def task_resume(task_id):
    read_record(task_id,"task")
    with session() as db:
        row = db.scalar(select(Record).where(Record.id == task_id, Record.kind == "task").with_for_update())
        if not row or row.status not in {"interrupted", "error", "budget_exhausted", "needs_input"}:
            raise ValueError("只有中断、错误、等待继续或预算耗尽的任务可继续")
        row.payload = dict(row.payload, status="queued", summary="从持久化状态继续")
        row.status = "queued"
        enqueue(db,row,"agent")
    return read_record(task_id)


# 功能：取得对话消息；对没有 messages 字段的旧 Task，根据目标、事件和摘要合成兼容消息列表。
# 输入：task_id 为当前项目的对话 ID；消息或旧版字段从 records(kind=task) 读取。
# 输出/影响：返回 user/tool/assistant 消息字典列表；兼容列表只在内存生成，不回写 Task、不加载附件正文。
def task_messages(task_id):
    task = read_record(task_id, "task")
    if task.get("messages") is not None:return task["messages"]
    legacy = [{"id": task_id+"-goal", "role":"user", "content":task["goal"], "createdAt":task["createdAt"], "delivered":True}]
    legacy += [{"id":e["id"], "role":"tool", "eventId":e["id"], "createdAt":e["createdAt"]} for e in task.get("events",[])]
    if task["status"] not in {"running","queued"}:
        legacy.append({"id":task_id+"-summary", "role":"assistant", "content":task["summary"], "createdAt":task["createdAt"]})
    return legacy


# 同一 Task 中追加用户消息。新要求会增加 intentVersion，使旧意图下的审批失效。
# 正在执行时先保存消息，下一次决策或后续领取再消费；不会强行修改一次在途模型请求。
# 功能：给同一段对话追加用户要求及附件引用，允许下一轮切换模型并令旧意图审批失效。
# 输入：task_id 为对话 ID，content 为 1～6000 字消息；key 去重；attachment_ids 为已上传 ID；network_capture 可指定采集，model_profile_id/reasoning_effort 为本轮选择。
# 输出/影响：返回更新后的 Task；通过 mutate 写消息/意图版本和必要的 agent Job，正在执行的模型请求不会被本函数强制改写，Key 不入记录。
def task_message(task_id, content, key="", attachment_ids=None, network_capture=None, model_profile_id="", reasoning_effort=""):
    from .attachments import attachment_refs
    from .network_capture import validate_level
    if network_capture is not None: validate_level(network_capture)
    attachments = attachment_refs(attachment_ids or [])
    content = content.strip() or ("请阅读附件，概括需求与待确认项，先不要执行测试。" if attachments else "")
    if not content.strip() or len(content)>6000:raise ValueError("消息需要 1～6000 字")
    existing=task_messages(task_id)
    selection={}
    if settings.role=="platform":
        from .models import validate_selection
        prior=read_record(task_id,"task")
        chosen=validate_selection(model_profile_id or prior.get("modelProfileId",""),reasoning_effort)
        selection=dict(modelProfileId=chosen["id"],reasoningEffort=reasoning_effort)
    # 功能：在事务内追加新消息，废止旧操作，并决定是否将 Agent 作业重新排队。
    # 输入：db 由 mutate 注入；task_id、content、attachments、existing 和 selection 从 task_message 闭包读取。
    # 输出/影响：更新 Task、增加 intentVersion 并废止旧审批；没有有效运行 Job 时登记下一轮投递事件，正在运行时保留消息供后续决策消费。
    def action(db):
        row=db.scalar(select(Record).where(Record.id==task_id,Record.kind=="task").with_for_update())
        if not row:raise ValueError("对话不存在")
        assert_project(row)
        p=dict(row.payload)
        from .agent.permissions import invalidate
        p["intentVersion"]=p.get("intentVersion",0)+1
        invalidate(p,"用户发送了新要求，不执行旧操作")
        p["messages"]=[*p.get("messages",existing), {"id":uid("MSG"),"role":"user","content":content.strip(),"attachments":attachments,"captureLevel":network_capture,"createdAt":stamp(),"delivered":False}]
        p["messages"][-1].update(selection)
        if row.status=="waiting_approval":p.update(status="queued",resumeRequested=True,summary="按新要求继续，旧操作已取消");row.status="queued"
        job=get_job(db,task_id)
        if not job or job.status!="running" or not job.lease or job.lease<now():
            p.update(status="queued",summary="等待 Agent 回复")
            row.status="queued"
        row.payload=p
        if row.status=="queued" and (not job or job.status!="running" or not job.lease or job.lease<now()):enqueue(db,row,"agent")
        return p
    return mutate("task_message",key,dict(task_id=task_id,content=content,attachment_ids=attachment_ids or [],network_capture=network_capture,selection=selection),action)


# 只消费当前子进程绑定模型的连续消息；遇到模型不同的消息就留给后续进程。
# 例如 A→B→A 三条消息不能跳过 B 提前消费最后一条 A，必须保持用户发言顺序。
# 功能：领取尚未交给 Agent 的连续用户消息，保持消息顺序并按本子进程绑定模型分批消费。
# 输入：task_id 为对话 ID；当前激活的 (模型配置 ID, 思考深度) 由 models.active_selection 提供。
# 输出/影响：返回被领取的消息副本列表；update_record 把这些消息 delivered 改为 True，遇到模型不同的消息后本批不再越过它消费。
def take_user_messages(task_id):
    taken=[]
    # 功能：扫描待消费用户消息并记录本进程可处理的连续一批，阻止跨模型越序消费。
    # 输入：p 由 update_record 提供；taken 列表取自外层闭包，active_selection 由 models 模块读取。
    # 输出/影响：无返回值；原地替换 p.messages，向 taken 追加本批消息副本；记录写入由 update_record 负责。
    def change(p):
        messages=[]
        deferred=False
        for original in p.get("messages",[]):
            message=dict(original)
            chosen=__import__("testpilot.models",fromlist=["active_selection"]).active_selection
            if message["role"]=="user" and not message.get("delivered") and not message.get("suppressed"):
                if chosen and (message.get("modelProfileId"),message.get("reasoningEffort",""))!=chosen:deferred=True
                if not deferred:
                    taken.append(message.copy());message["delivered"]=True
            messages.append(message)
        p["messages"]=messages
    update_record(task_id,change)
    return taken


# 流式输出会反复更新同一消息 ID，而不是每个字符追加一条聊天消息。
# 最终消息写入后，迟到的部分片段不能覆盖已经完整的内容。
# 功能：保存助手回答或工具关联消息，按消息 ID 更新流式片段并防止迟到片段覆盖最终内容。
# 输入：task_id 为对话 ID；role 为消息作者；content 为文字，identity 可指定稳定消息 ID，event_id 关联工具事件，partial 表示尚在流式输出。
# 输出/影响：无返回值；通过 update_record 更新 Task.messages；缺少 identity 时生成新 MSG ID，最终消息写入后不接受旧的 partial 覆盖。
def conversation_append(task_id, role, content="", identity="", event_id="", partial=False):
    # 功能：在一份 Task payload 中追加消息或更新同 ID 的流式回答。
    # 输入：p 由 update_record 注入；role/content/identity/event_id/partial 由 conversation_append 闭包提供。
    # 输出/影响：无返回值；原地更新 p.messages，不直接 commit；已完成消息不会被迟到的流式片段覆盖。
    def change(p):
        messages=p.get("messages",[])
        identity_value=identity or uid("MSG")
        messages=[dict(m) for m in messages]
        existing=next((m for m in messages if m["id"]==identity_value),None)
        if existing:
            if not partial or existing.get("streaming"):
                existing.update(content=content,streaming=partial)
            p["messages"]=messages
        else:p["messages"]=[*messages,dict(id=identity_value,role=role,content=content,eventId=event_id,streaming=partial,createdAt=stamp())]
    update_record(task_id,change)


# 工具 call_id 对应一次调用；planned/running/completed 等状态更新同一条事件。
# 聊天中的 tool 消息通过 eventId 指向事件，界面可展开看到参数与真实结果。
# 功能：记录 Agent 工具调用事件，并让聊天中的工具消息关联到可展开的事件详情。
# 输入：task_id 为对话 ID；name/arguments/result 为工具名称、参数和结果；call_id 标识同一次调用，status 表示 planned/running/completed 等阶段。
# 输出/影响：无返回值；update_record 保存 Task.events/step/messages，同 call_id 更新既有事件而非重复追加，事件结果不自动判定测试通过。
def task_event(task_id, name, arguments, result, call_id="", status="completed"):
    identity=uid("EVENT")
    # 功能：按调用 ID 更新已有工具事件，或新增事件及指向它的聊天工具消息。
    # 输入：p 由 update_record 提供；identity/name/arguments/result/call_id/status 来自 task_event 闭包。
    # 输出/影响：无返回值；原地替换事件列表并更新 step 计数，仅新事件追加 tool 消息；数据库写入交给 update_record。
    def change(p):
        events=[dict(e) for e in p.get("events",[])]
        existing=next((e for e in events if call_id and e.get("callId")==call_id),None)
        if existing:existing.update(result=result,status=status)
        else:events.append(dict(id=identity,callId=call_id,name=name,arguments=arguments,result=result,status=status,createdAt=stamp()))
        p["events"]=events;p["step"]=len(events)
        if not existing:
            p["messages"]=[*p.get("messages",[]),dict(id=identity,role="tool",eventId=identity,createdAt=stamp())]
    update_record(task_id,change)



# 给 Agent 项目概况、环境和资料目录，不一次塞入所有文件正文。
# 模型需要更详细的需求时通过 sources_read/attachments_read 按需读取。
# 功能：为 Agent 汇总当前项目、目标环境、资料目录和测试能力，便于先发现资料再按需读取。
# 输入：environment_id 可指定被测环境，空值省略环境；当前项目和账号来自请求/执行上下文。
# 输出/影响：返回概况字典，含最多 50 项资料索引及应用/设备列表；读取数据库但不加载所有文档正文，不向模型提供账号密码值。
def project_context(environment_id=""):
    from .sources import source_catalog
    return {"project": project_settings()["name"], "environment": read_record(environment_id, "environment") if environment_id else None, **source_catalog(), "credentials": "读取 TEST_USERNAME / TEST_PASSWORD 环境变量；不要在源码或工具参数中填写密码。", "applications": list_records("application"), "devices": __import__("testpilot.mobile", fromlist=["devices_list"]).devices_list(), "capabilities": ["api", "web", "performance (JMeter)", "app (Android/Appium)"], "principles": "业务预期来自需求；发现错误不能修改预期来掩盖。资料缺失时记录待确认项。"}


# 功能：读取当前项目的名称与描述，在项目记录缺失时提供 MM 演示的兼容默认说明。
# 输入：无显式参数；项目 ID 由 current_project 上下文提供。
# 输出/影响：返回项目字典或默认名称/描述字典；仅查询，捕获 ValueError 后不自动创建项目记录。
def project_settings():
    try:
        return read_record(current_project(), "project")
    except ValueError:
        return {"name": "MM 平台化", "description": "MM 登录、鉴权和协作功能的被测项目。"}


# 功能：按当前进程角色返回不含密钥的模型配置，供 Agent 初始化或前端展示。
# 输入：无显式参数；读取 settings.role、执行器已激活 profile、账号执行服务 profiles 或旧 settings 记录。
# 输出/影响：返回配置字典及 configured 标记；不返回 model_key、不写文件或数据库，configured 只是当前配置状态而非可用性测试结果。
def model_settings():
    if settings.role=="executor":
        from .models import active_profile
        if active_profile:return dict(active_profile,configured=bool(settings.model_key))
    if settings.role=="platform":
        from .executors import profiles
        rows=profiles()
        chosen=next((r for r in rows if r.get("default")),rows[0] if rows else {})
        return dict(model=chosen.get("model",""),baseUrl=chosen.get("baseUrl",""),maxSteps=chosen.get("maxSteps",24),configured=bool(rows))
    configs = list_records("settings")
    saved = configs[0] if configs else {}
    return dict(saved, model=saved.get("model") or settings.model, baseUrl=saved.get("baseUrl") or settings.model_url, maxSteps=saved.get("maxSteps") or settings.max_steps, configured=bool(settings.model_key and settings.model_url))


# 功能：为界面汇总当前项目的数据视图，使各页面加载同一组业务记录。
# 输入：无显式参数；账号与项目范围由上下文和各 list/read 函数校验。
# 输出/影响：返回用例源码、运行、对话消息、环境、项目、资料、不含密钥的模型配置和应用/设备信息字典；读取数据库和用例文件，不执行测试或模型。
def state_snapshot():
    from .sources import sources_list
    return {"cases": [hydrate_case(c) for c in list_records("case")], "runs": list_records("run"), "tasks": [dict(v,messages=task_messages(v["id"])) for v in list_records("task")], "environments": list_records("environment"), "projectId": current_project(), "projects": list_records("project"), "projectName": project_settings()["name"], "projectDescription": project_settings()["description"], "sources": sources_list(), "settings": model_settings(), "applications": list_records("application"), "devices": __import__("testpilot.mobile", fromlist=["devices_list"]).devices_list()}


# 功能：创建或编辑项目名称和说明，同时保持既有项目的成员及所有者信息。
# 输入：name 为非空项目名，description 为说明；project_id 留空生成 PROJECT ID，非空指定编辑或演示项目 ID。
# 输出/影响：返回项目字典；事务写 records(kind=project)，编辑时检查访问权限，新建普通账号项目时设置 ownerId/members；不创建用户本地源码目录。
def project_save(name, description="", project_id=""):
    if not name.strip():raise ValueError("项目名称不能为空")
    identity = project_id or uid("PROJECT")
    with session() as db:
        row = db.get(Record, identity)
        payload = dict(id=identity, name=name.strip(), description=description)
        if row and row.kind != "project":raise ValueError("项目 ID 不正确")
        if row:
            from .storage import assert_project
            assert_project(row)
            payload.update({k:v for k,v in row.payload.items() if k in {"ownerId","members"}})
        else:
            from .workspace import current_user
            if current_user() != "service":payload.update(ownerId=current_user(),members=[])
        if row:row.payload=payload
        else:db.add(Record(id=identity,kind="project",payload=payload))
    return payload


# 首次空项目的 MM 演示资料从这里写入，包括健康接口和登录约定。
# 因此 Agent 知道 MM 接口有明确资料来源；不能推广为它自动知道任意项目的业务。
# 功能：为空数据集补入 MM 演示项目、目标环境和已知接口说明，提供可追溯的业务资料来源。
# 输入：无显式参数；读取当前作用域的项目、环境和资料列表判断是否需要初始化。
# 输出/影响：无返回值；按缺失类型写 project/environment/source 记录；已有对应数据时跳过，不登录 MM、不调用模型生成需求。
def seed():
    if not list_records("project"):
        project_save("MM 平台化", "MM 登录、鉴权和协作功能的被测项目。", "PROJECT")
    if not list_records("environment"):
        environment_save("MM 本地环境", "http://127.0.0.1:8065", "http://mm-target:8065", "浏览器地址与容器执行地址分别配置")
    if not list_records("source"):
        with session() as db:
            db.add(Record(id="SRC-MM", kind="source", payload={"id": "SRC-MM", "name": "MM 登录与健康检查 API", "content": "GET /api/v4/system/ping：HTTP 200，JSON status 为 OK。POST /api/v4/users/login：JSON login_id、password；不存在账号应被拒绝，HTTP 401，body 包含 id/message/status_code。有效账号登录成功返回用户信息，响应头 Token 用于 Authorization: Bearer <token>。UI /login 账号和密码输入框，登录按钮；错误账号应提示错误且不能进入工作区。错误提示的精确文案未在需求中约定，不应臆造。"}))
