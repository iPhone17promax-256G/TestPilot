import ast
import json
import xml.etree.ElementTree as ET
import httpx
import pytest
from aiqa.steps import compile_steps_details, validate_steps
from aiqa.authoring import native_fallback, visual_authoring
from aiqa.tools import SaveVisualCase
from aiqa.step_catalog import capabilities, catalog
from aiqa.runners import visual_runtime as runtime


def execute(kind, steps, **fixtures):
    normalized,source,mapping=compile_steps_details(kind,steps)
    scope={};exec(compile(source,"test_case.py","exec"),scope)
    scope["test_visual_case"](**fixtures)
    return source,mapping

@pytest.fixture
def report(tmp_path,monkeypatch):
    from aiqa.runners import steps
    original=runtime.StepRuntime
    monkeypatch.setattr(steps,"StepRuntime",lambda *args,**kwargs:original(*args,report_path=str(tmp_path/"steps.json"),**kwargs))
    return tmp_path/"steps.json"


def test_api_nested_branches_loops_and_generated_report(report,monkeypatch):
    seen=[]
    def target(request):
        seen.append(request.url.path)
        return httpx.Response(200,json={"status":"OK"})
    client=httpx.Client
    monkeypatch.setattr(runtime.httpx,"Client",lambda **kw:client(transport=httpx.MockTransport(target),**kw))
    source,mapping=execute("api",[
        {"action":"set_variable","variable":"allow","expected":True},
        {"action":"foreach","variable":"path","items":["one","two"]},
        {"action":"repeat","variable":"i","count":2},
        {"action":"if","source":"variable","expression":"allow","expected":True},
        {"action":"request","path":"/{{path}}","assert_json":{"$.status":"OK"}},
        {"action":"else"},{"action":"request","path":"/never"},{"action":"end"},
        {"action":"end"},{"action":"end"},
        {"action":"repeat","count":2,"enabled":False},{"action":"request","path":"/disabled"},{"action":"end"},
    ],base_url="http://target.test")
    # Variable path must resolve to a relative path, without accidental double slash.
    assert seen==["/one","/one","/two","/two"]
    rows=json.loads(report.read_text())
    assert any(r["name"]=="步骤 7" and r["status"]=="skipped" for r in rows)
    assert any(r.get("conditionMatched") is True for r in rows)
    assert len(mapping)==13 and "if _aiqa_condition_" in source
    assert len([r for r in rows if r["action"]=="request" and r["status"]=="passed"])==4


def test_api_false_branch_and_multipart(report,monkeypatch,tmp_path):
    (tmp_path/"hello.txt").write_text("AIQA multipart")
    monkeypatch.chdir(tmp_path);seen=[]
    def target(request):
        seen.append(request)
        assert b"AIQA multipart" in request.content and b'name="title"' in request.content
        return httpx.Response(201,json={"uploaded":True})
    client=httpx.Client
    monkeypatch.setattr(runtime.httpx,"Client",lambda **kw:client(transport=httpx.MockTransport(target),**kw))
    execute("api",[{"action":"if","source":"variable","expression":"missing","operator":"exists"},{"action":"request","path":"/never"},{"action":"else"},{"action":"request","path":"/upload","method":"POST","body_type":"multipart","body":{"title":"test"},"uploads":{"file":"hello.txt"},"expected_status":201,"assert_json":{"$.uploaded":True}},{"action":"end"}],base_url="http://target.test")
    assert len(seen)==1 and all(r["status"] in {"passed","skipped"} for r in json.loads(report.read_text()))


@pytest.mark.parametrize("steps",[
    [{"action":"end"}], [{"action":"else"}], [{"action":"repeat"},{"action":"request"}],
    [{"action":"repeat"},{"action":"else"},{"action":"end"}],
    [{"action":"foreach","items":{},"variable":"item"},{"action":"end"}],
    [{"action":"if","source":"element","selector":"#a"},{"action":"request"},{"action":"end"}],
])
def test_invalid_controls_rejected(steps):
    with pytest.raises(ValueError):validate_steps("api",steps)


def test_default_tool_requires_steps_and_no_code_schema():
    schema=SaveVisualCase.model_json_schema()
    assert "steps" in schema["required"] and "code" not in schema["properties"]
    with pytest.raises(ValueError):SaveVisualCase(name="ping",kind="api",code="pass")


def test_prevents_hiding_whole_test_in_python():
    with pytest.raises(ValueError,match="藏进"):
        visual_authoring([{"action":"python","python":"assert True","reason":"脚本比步骤更容易编写"}])
    with pytest.raises(ValueError,match="reason"):
        visual_authoring([{"action":"request"},{"action":"python","python":"assert True"}])
    visual_authoring([{"action":"request"},{"action":"python","reason":"计算当前标准提取器无法表达的校验和","python":"assert True"}])


def test_native_fallback_checks_latest_user_request_and_actual_gap():
    task={"goal":"检查健康接口","messages":[{"role":"user","content":"请检查健康接口"}]}
    fallback={"mode":"user_requested","reason":"用户明确指定了原生脚本编写方式","user_quote":"请写脚本"}
    with pytest.raises(ValueError):native_fallback("api","def test_ping(): pass",fallback,task)
    task["messages"].append({"role":"user","content":"请写脚本，保留 Python 源码"})
    assert native_fallback("api","def test_ping(): pass",fallback,task)["policy"]=="visual-first-v1"
    task["messages"][-1]["content"]="不要写脚本，使用步骤"
    with pytest.raises(ValueError):native_fallback("api","def test_ping(): pass",fallback,task)
    fallback={"mode":"capability_gap","reason":"失败后也必须通过 pytest fixture 的 teardown 清理远端测试数据","capability":"pytest_lifecycle"}
    with pytest.raises(ValueError):native_fallback("api","def test_ping(): pass",fallback,task)
    code="import pytest\n@pytest.fixture\ndef cleanup():\n    yield\ndef test_cleanup(cleanup):\n    assert cleanup is None\n"
    assert native_fallback("api",code,fallback,task)["capability"]=="pytest_lifecycle"


def test_jmeter_visual_controls_and_fallback_validation():
    steps=[{"action":"csv","value":"accounts.csv","columns":"login,password"},{"action":"once"},{"action":"request","path":"/login"},{"action":"end"},{"action":"transaction","name":"健康事务"},{"action":"repeat","count":2},{"action":"request","path":"/ping","assert_json":{"$.status":"OK"},"max_response_ms":1000},{"action":"end"},{"action":"end"}]
    _,source,mapping=compile_steps_details("performance",steps)
    root=ET.fromstring(source)
    assert root.find(".//CSVDataSet") is not None and root.find(".//OnceOnlyController") is not None
    assert root.find(".//TransactionController/../hashTree/LoopController") is not None
    assert root.find(".//JSONPathAssertion") is not None and root.find(".//DurationAssertion") is not None
    assert len(mapping)==len(steps) and all(m["start"] for m in mapping)
    fallback={"mode":"capability_gap","capability":"jmeter_component","reason":"该计划必须采用可视化尚未提供的吞吐控制器"}
    with pytest.raises(ValueError,match="已覆盖"):native_fallback("performance",source,fallback,{})
    source=source.replace("OnceOnlyController","ThroughputController")
    assert "ThroughputController" in native_fallback("performance",source,fallback,{})["detectedComponents"]


def test_browser_branch_tabs_download_and_scroll(report,tmp_path):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser=p.chromium.launch(executable_path="/usr/lib/chromium/chromium",args=["--no-sandbox"])
        context=browser.new_context(accept_downloads=True)
        page=context.new_page()
        page.context.route("http://target.test/**",lambda route:route.fulfill(content_type="text/html",body="""<button id=popup onclick="window.open('/detail')">Open</button><a id=download download=demo.txt href='data:text/plain,AIQA%20download'>Download</a><p id=content>AIQA page</p>"""))
        source,mapping=execute("web",[{"action":"goto","path":"/"},{"action":"if","source":"element","selector":"#missing","timeout":100},{"action":"click","selector":"#missing"},{"action":"else"},{"action":"assert_text","selector":"#content","value":"AIQA"},{"action":"end"},{"action":"click_popup","selector":"#popup","tab":"detail"},{"action":"assert_text","selector":"#content","value":"AIQA"},{"action":"close_tab","tab":"detail"},{"action":"download","selector":"#download","value":"download.txt"},{"action":"open_tab","tab":"second","path":"/other"},{"action":"switch_tab","tab":"main"},{"action":"scroll","selector":"#content"},{"action":"reload"},{"action":"assert_visible","selector":"#content"}],page=page,base_url="http://target.test")
        assert (tmp_path/"download.txt").read_text()=="AIQA download"
        assert len(page.context.pages)==2
        browser.close()
    assert len(mapping)==15 and all(r["status"] in {"passed","skipped"} for r in json.loads(report.read_text()))


def test_capability_catalog_covers_actual_actions():
    from aiqa.steps import ACTIONS
    for kind,actions in ACTIONS.items():
        assert {item["action"] for item in catalog()[kind]}==actions
        result=capabilities(kind)
        assert {a["action"] for a in result["actions"]}==actions
    assert "count" in next(a for a in capabilities("api")["actions"] if a["action"]=="repeat")["fields"]


def test_http_agent_cannot_bypass_visual_policy_and_fallback_is_saved():
    from fastapi.testclient import TestClient
    from sqlalchemy import select
    from aiqa.api import app
    from aiqa.config import settings
    from aiqa.storage import Record,Mutation,session,uid
    from aiqa.agent import permissions
    from aiqa.workspace import project_scope
    client=TestClient(app,headers={"Authorization":"Bearer "+settings.access_token})
    project=client.post("/api/projects",json={"name":"可视化策略测试临时项目"}).json()["id"]
    headers={"X-AIQA-Project":project};task=uid("TASK")
    try:
        with session() as db:db.add(Record(id=task,kind="task",project_id=project,payload={"id":task,"status":"completed","permissionMode":"bypass","goal":"检查健康接口","messages":[{"role":"user","content":"检查健康接口","delivered":True}],"caseIds":[],"runIds":[]}))
        def authorized_post(name,arguments):
            tool_call=dict(id=uid("CALL"),name=name,args=arguments)
            with project_scope(project):
                permissions.review(task,tool_call,None,"test authoring policy",lambda s:s)
                with permissions.execution_permit(task,tool_call):
                    return client.post("/api/tools/"+name,headers=headers,json={"task_id":task,"arguments":arguments})
        args={"name":"policy","kind":"api","code":"def test_ping():\n    assert True\n"}
        denied=client.post("/api/tools/cases_save_revision",headers=headers,json={"task_id":task,"arguments":args})
        assert denied.status_code==403
        denied=authorized_post("cases_save_revision",args)
        assert denied.status_code==400 and "默认可视化" in denied.json()["message"]
        saved=authorized_post("cases_save_revision",{"name":"policy","kind":"api","steps":[{"action":"request","path":"/ping","expected_status":200}]})
        assert saved.status_code==200 and saved.json()["revisions"][-1]["mode"]=="steps"
        with session() as db:
            row=db.get(Record,task);row.payload=dict(row.payload,messages=[{"role":"user","content":"请用原生脚本写一个演示","delivered":True}])
        args["fallback"]={"mode":"user_requested","reason":"用户本轮明确指定要查看原生脚本演示","user_quote":"用原生脚本"}
        saved=authorized_post("cases_save_script",args)
        assert saved.status_code==200 and saved.json()["revisions"][-1]["fallback"]["policy"]=="visual-first-v1"
        # Manual imports remain available; this policy belongs to the built-in Agent.
        manual=client.post("/api/tools/cases_save_revision",headers=headers,json={"arguments":{"name":"import","kind":"api","code":"def test_import(): pass"}})
        assert manual.status_code==200 and manual.json()["revisions"][-1]["mode"]=="script"
    finally:
        with session() as db:
            for row in db.scalars(select(Record).where((Record.project_id==project)|(Record.id==project))):db.delete(row)
            for row in db.scalars(select(Mutation).where(Mutation.scope.startswith(project+":"))):db.delete(row)


def test_nested_data_templates_and_script_schema_are_unambiguous():
    from aiqa.runners.steps import substitute
    from aiqa.tools import SaveScriptCase
    values={"item":{"name":"tester","ids":[3,4]}}
    assert substitute("{{item.name}}",values)=="tester"
    assert substitute("id={{item.ids[1]}}",values)=="id=4"
    with pytest.raises(ValueError):substitute("{{item.missing}}",values)
    schema=SaveScriptCase.model_json_schema()
    assert "steps" not in schema["properties"] and "code" in schema["required"] and "fallback" in schema["required"]


def test_jmeter_conditional_blocks_compile_both_branches():
    from aiqa.visual_performance import jmeter_condition
    steps=[{"action":"if","source":"variable","expression":"state","expected":"OK"},{"action":"request","path":"/good"},{"action":"else"},{"action":"request","path":"/bad"},{"action":"end"}]
    _,source,mapping=compile_steps_details("performance",steps)
    root=ET.fromstring(source);controllers=root.findall(".//IfController")
    assert len(controllers)==2 and len(mapping)==5
    conditions=[c.find("stringProp[@name='IfController.condition']").text for c in controllers]
    assert 'vars.get("state") == "OK"' in conditions[0] and 'vars.put(' in conditions[0]
    assert 'vars.get("state")' not in conditions[1] and '== "false"' in conditions[1]
    assert root.findall(".//HTTPSamplerProxy")[0].get("testclass")=="HTTPSamplerProxy"


@pytest.mark.parametrize("path",["/ping","//other.test/ping","http://other.test/ping"])
def test_full_path_variables_stay_inside_environment(path,report,monkeypatch):
    seen=[]
    client=httpx.Client
    monkeypatch.setattr(runtime.httpx,"Client",lambda **kw:client(transport=httpx.MockTransport(lambda request:(seen.append(request.url.path) or httpx.Response(200,json={"status":"OK"}))),**kw))
    steps=[{"action":"set_variable","variable":"data","expected":{"path":path}},{"action":"request","path":"{{data.path}}","expected_status":200}]
    if path=="/ping":
        execute("api",steps,base_url="http://target.test")
        assert seen==["/ping"]
    else:
        with pytest.raises(AssertionError,match="相对路径"):
            execute("api",steps,base_url="http://target.test")
        assert not seen


def test_regex_condition_uses_selected_capture_group(report,monkeypatch):
    client=httpx.Client
    monkeypatch.setattr(runtime.httpx,"Client",lambda **kw:client(transport=httpx.MockTransport(lambda request:httpx.Response(200,text="state=ready")),**kw))
    execute("api",[
        {"action":"request","path":"/state"},
        {"action":"if","source":"text","query":"regex","expression":"state=(ready)","match_group":1,"expected":"ready"},
        {"action":"assert","source":"status","expected":200},
        {"action":"else"},{"action":"assert","source":"status","expected":500},{"action":"end"},
    ],base_url="http://target.test")
    rows=json.loads(report.read_text())
    assert next(r for r in rows if r["action"]=="if")["conditionMatched"] is True
    assert rows[-2]["status"]=="skipped"
    assert "match_group" in next(a for a in capabilities("api")["actions"] if a["action"]=="if")["fields"]


@pytest.mark.parametrize("kind",["api","web","app"])
def test_invalid_variable_condition_query_rejected_before_execution(kind):
    with pytest.raises(ValueError,match="表达式无效"):
        validate_steps(kind,[{"action":"if","source":"variable","query":"jmespath","expression":"data[","expected":True},{"action":"sleep"},{"action":"end"}])


def test_unused_fixture_and_unregistered_hook_do_not_unlock_scripts():
    fallback={"mode":"capability_gap","reason":"测试需要失败时也执行自定义资源清理","capability":"pytest_lifecycle"}
    unused="import pytest\n@pytest.fixture\ndef unused(): yield\ndef test_real(): assert True\n"
    hook="def pytest_sessionfinish(session): pass\ndef test_real(): assert True\n"
    for code in [unused,hook]:
        with pytest.raises(ValueError,match="实际使用"):
            native_fallback("api",code,fallback,{})
    used=unused.replace("@pytest.fixture", "@pytest.fixture(autouse=True)")
    assert native_fallback("api",used,fallback,{})["capability"]=="pytest_lifecycle"


def test_inactive_control_subtree_cannot_hide_whole_script():
    with pytest.raises(ValueError,match="藏进"):
        visual_authoring([
            {"action":"repeat","enabled":False},
            {"action":"request","path":"/ping"},{"action":"end"},
            {"action":"python","python":"assert True","reason":"把所有真实测试放入自定义代码"},
        ])


def test_disabled_control_keeps_editable_incomplete_steps_without_requiring_files():
    steps=[{"action":"goto","path":"/login"},{"action":"repeat","enabled":False},
           {"action":"upload"},{"action":"python"},{"action":"end"}]
    _,source,mapping=compile_steps_details("web",steps)
    ast.parse(source)
    assert len(mapping)==5
    from aiqa.testing import case_save
    from aiqa.storage import Record,session
    from aiqa.workspace import project_scope
    with project_scope("PROJECT"):
        saved=case_save("自检 · 关闭块内未配置上传", "web", "", steps=steps)
        try:
            assert saved["revisions"][-1]["mode"]=="steps"
        finally:
            with session() as db:db.delete(db.get(Record,saved["id"]))
            import shutil
            from aiqa.config import settings
            root=(settings.data/"cases").resolve()
            folder=(root/saved["id"]).resolve()
            assert folder.parent==root and saved["id"].startswith("TC-")
            shutil.rmtree(folder)


def test_disabled_performance_request_is_not_compiled_or_required():
    steps=[{"action":"request","path":"/ping"}, {"action":"once","enabled":False},
           {"action":"request","path":"unfinished","body_type":"multipart"}, {"action":"end"}]
    _,source,_=compile_steps_details("performance",steps)
    assert len(ET.fromstring(source).findall(".//HTTPSamplerProxy"))==1


def test_only_live_missing_jmeter_features_unlock_script_fallback():
    _,source,_=compile_steps_details("performance",[{"action":"request","path":"/ping"},{"action":"once"},{"action":"request","path":"/other"},{"action":"end"}])
    fallback={"mode":"capability_gap","capability":"jmeter_component","reason":"需要尚未覆盖的 JMeter HTTP 文件上传能力"}
    disabled=ET.fromstring(source)
    control=disabled.find(".//OnceOnlyController");control.tag="ThroughputController";control.set("testclass","ThroughputController");control.set("enabled","false")
    with pytest.raises(ValueError,match="已覆盖"):
        native_fallback("performance",ET.tostring(disabled,encoding="unicode"),fallback,{})
    root=ET.fromstring(source)
    files=ET.SubElement(root.find(".//HTTPSamplerProxy"),"elementProp",name="HTTPsampler.Files",elementType="HTTPFileArgs")
    ET.SubElement(files,"stringProp",name="File.path").text="data.txt"
    assert native_fallback("performance",ET.tostring(root,encoding="unicode"),fallback,{})["detectedComponents"]==["HTTPFileArgs"]


def test_discovery_describes_only_effective_fields_for_each_engine():
    for kind in ["web","app"]:
        fields=next(a for a in capabilities(kind)["actions"] if a["action"]=="extract")["fields"]
        assert set(fields)=={"variable","attribute"}
        fields=next(a for a in capabilities(kind)["actions"] if a["action"]=="if")["fields"]
        assert fields["source"]["pattern"]=="^(variable|element)$" and "regex" not in fields["query"]["pattern"]
    fields=next(a for a in capabilities("performance")["actions"] if a["action"]=="if")["fields"]
    assert set(fields)=={"source","expression","operator","expected"}
    assert fields["source"]["pattern"]=="^variable$" and "length" not in fields["operator"]["pattern"]
    assert "multipart" not in next(a for a in capabilities("performance")["actions"] if a["action"]=="request")["fields"]["body_type"]["pattern"]


@pytest.mark.parametrize("kind",["api","web","app","performance"])
def test_if_omitted_source_matches_discovered_default(kind):
    normalized=validate_steps(kind,[{"action":"if","expression":"ready","expected":True},{"action":"sleep" if kind!="performance" else "request","path":"/ping"},{"action":"end"}])
    assert normalized[0]["source"]==next(a for a in capabilities(kind)["actions"] if a["action"]=="if")["fields"]["source"]["default"]=="variable"


def test_scheme_like_path_cannot_change_target_origin(report,monkeypatch):
    seen=[];client=httpx.Client
    monkeypatch.setattr(runtime.httpx,"Client",lambda **kw:client(transport=httpx.MockTransport(lambda request:(seen.append(request.url) or httpx.Response(200,json={"status":"OK"}))),**kw))
    execute("api",[{"action":"request","path":"/http://other.test/ping","expected_status":200}],base_url="http://target.test")
    assert len(seen)==1 and seen[0].host=="target.test" and seen[0].path=="/http://other.test/ping"
    assert runtime.StepRuntime("http://target.test/base").url("/next")=="http://target.test/base/next"


@pytest.mark.parametrize("kind",["api","web","app","performance"])
def test_entirely_disabled_control_tree_cannot_be_a_passing_test(kind):
    action={"api":"request","web":"goto","app":"click","performance":"request"}[kind]
    with pytest.raises(ValueError,match="至少需要"):
        validate_steps(kind,[{"action":"repeat","enabled":False},{"action":action,"path":"/ping"},{"action":"end"}])
