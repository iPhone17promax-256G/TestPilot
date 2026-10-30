import json
import xml.etree.ElementTree as ET
import httpx
import pytest
from testpilot.steps import compile_steps_details, validate_steps
from testpilot.runners import visual_runtime as runtime
from testpilot.runners.steps import assertion

def execute(kind, steps, **fixtures):
    normalized, source, mapping=compile_steps_details(kind,steps)
    scope={}
    exec(compile(source,"test_case.py","exec"),scope)
    scope["test_visual_case"](**fixtures)
    return normalized,source,mapping

@pytest.fixture
def api_runtime(tmp_path,monkeypatch):
    seen=[]
    def target(request):
        seen.append(request)
        if request.url.path=="/login":
            return httpx.Response(200,json={"data":{"token":"secret-token","users":[{"id":1,"active":True},{"id":2,"active":False}]}},headers={"set-cookie":"session=abc"})
        if request.url.path=="/me":
            assert request.headers["authorization"]=="Bearer secret-token"
            assert request.headers["cookie"]=="session=abc"
            assert request.url.params["ids"]=="1"
            return httpx.Response(200,text="ticket=42; done")
        if request.url.path=="/form":
            assert request.content==b"name=A+B"
            return httpx.Response(201,json={"ok":True})
        return httpx.Response(404,json={})
    client=httpx.Client
    monkeypatch.setattr(runtime.httpx,"Client",lambda **kw:client(transport=httpx.MockTransport(target),**kw))
    monkeypatch.setattr(runtime,"Path",lambda value:tmp_path/"steps.json")
    return seen,tmp_path/"steps.json"

def test_queries_cookies_and_variables_execute_in_generated_source(api_runtime):
    seen,report=api_runtime
    normalized,source,mapping=execute("api",[
        {"action":"request","path":"/login","expected_status":200},
        {"action":"extract","query":"jmespath","expression":"data.users[?active].id","variable":"ids"},
        {"action":"assert","query":"jsonpath","expression":"$.data.users[*].id","expected":[1,2]},
        {"action":"extract","expression":"data.token","variable":"token"},
        {"action":"assert","expression":"data.absent","operator":"not_exists"},
        {"action":"python","python":"rt.variables['first_id'] = rt.variables['ids'][0]"},
        {"action":"request","path":"/me","params":{"ids":"{{first_id}}"},"headers":{"authorization":"Bearer {{token}}"}},
        {"action":"extract","source":"text","query":"regex","expression":"ticket=(\\d+)","match_group":1,"variable":"ticket"},
        {"action":"assert","source":"variable","expression":"ticket","expected":"42"},
        {"action":"request","path":"/form","method":"POST","body_type":"form","body":{"name":"A B"},"expected_status":201},
        {"action":"assert","expression":"ok","operator":"type","expected":"boolean"},
    ],base_url="http://target.test")
    assert len(seen)==3
    rows=json.loads(report.read_text())
    assert len(rows)==len(mapping)==len(normalized)
    assert all(r["status"]=="passed" and m["start"]<=r["sourceLine"]<=m["end"] for r,m in zip(rows,mapping))
    assert "secret-token" not in report.read_text()
    assert "json.loads" not in source and "execute_steps(" not in source
    assert "rt.request('POST'" in source

def test_failure_and_disabled_steps_preserve_identity(api_runtime):
    seen,report=api_runtime
    with pytest.raises(AssertionError,match="第 3 步"):
        execute("api",[
            {"action":"request","path":"/login"},
            {"id":"disabled","action":"request","path":"/should-not-run","enabled":False},
            {"id":"bad","name":"检查缺失字段","action":"assert","expression":"missing","expected":1},
            {"action":"request","path":"/form"},
        ],base_url="http://target.test")
    rows=json.loads(report.read_text())
    assert len(seen)==1 and [r["status"] for r in rows]==["passed","skipped","failed"]
    assert rows[-1]["id"]=="bad" and rows[-1]["sourceLine"]>0

@pytest.mark.parametrize("steps",[
    [{"action":"request","enabled":False}],
    [{"action":"python","python":"return"}],
    [{"action":"extract","variable":"x","query":"jmespath","expression":"["}],
    [{"action":"request","id":"x"},{"action":"request","id":"x"}],
])
def test_invalid_steps_rejected(steps):
    with pytest.raises((ValueError,SyntaxError)):validate_steps("api",steps)

def test_assertion_semantics():
    assertion(False,"type","boolean")
    assertion([1,2],"length",2)
    assertion(None,"not_exists",None)
    with pytest.raises(AssertionError):assertion(False,"type","number")
    with pytest.raises(AssertionError):assertion([1,2],"length",3)

def test_jmeter_mapping_preserves_params_form_timeout_redirects():
    _,source,mapping=compile_steps_details("performance",[
        {"id":"request-one","action":"request","path":"/login","method":"POST","params":{"next":"{{page}}"},"body_type":"form","body":{"name":"A B"},"timeout":2500,"follow_redirects":False},
        {"id":"disabled","action":"request","enabled":False},
        {"id":"request-two","action":"request","path":"/ping","body_type":"text","body":"hello"},
    ])
    root=ET.fromstring(source)
    sampler=root.findall(".//HTTPSamplerProxy")[0]
    props={p.get("name"):p.text for p in sampler.iter()}
    assert props["HTTPSampler.path"]=="/login?next="+"$"+"{page}"
    assert props["Argument.name"]=="name" and props["Argument.value"]=="A B"
    assert props["HTTPSampler.connect_timeout"]==props["HTTPSampler.response_timeout"]=="2500"
    assert props["HTTPSampler.follow_redirects"]=="false"
    assert len(mapping)==3 and mapping[1]["start"] is None
    for m in [mapping[0],mapping[2]]:
        block="\n".join(source.splitlines()[m["start"]-1:m["end"]])
        assert "<HTTPSamplerProxy" in block and m["id"].encode().hex() in block
    assert "text/plain" in source

def test_preview_pure_and_saved_source_matches():
    from fastapi.testclient import TestClient
    from testpilot.api import app
    from testpilot.config import settings
    from testpilot.storage import session,Record
    client=TestClient(app, base_url="https://testserver",headers={"Authorization":"Bearer "+settings.access_token})
    project=client.post("/api/projects",json={"name":"映射验证临时项目"}).json()["id"]
    headers={"X-TestPilot-Project":project}
    try:
        steps=[{"action":"set_variable","variable":"count","expected":2},{"action":"python","python":"assert rt.variables['count'] == 2"}]
        preview=client.post("/api/cases/preview",headers=headers,json={"kind":"api","steps":steps})
        assert preview.status_code==200
        assert client.get("/api/state",headers=headers).json()["cases"]==[]
        saved=client.post("/api/tools/cases_save_revision",headers=headers,json={"arguments":{"name":"映射","kind":"api","steps":steps}}).json()
        rev=saved["revisions"][-1]
        assert rev["code"]==preview.json()["code"] and rev["stepMap"]==preview.json()["stepMap"]
    finally:
        from sqlalchemy import select
        with session() as db:
            for row in db.scalars(select(Record).where((Record.project_id==project)|(Record.id==project))):db.delete(row)

def test_generated_web_wait_frame_upload_dialog(tmp_path,monkeypatch):
    from playwright.sync_api import sync_playwright
    from testpilot.runners import steps as legacy
    report=tmp_path/"steps.json";original=runtime.StepRuntime
    monkeypatch.setattr(legacy,"StepRuntime",lambda *args,**kw:original(*args,report_path=str(report),**kw))
    (tmp_path/"sample.txt").write_text("sample")
    monkeypatch.chdir(tmp_path)
    html="""<input aria-label="账号"><input type=checkbox id=accept>
    <select id=options><option value=a>A</option><option value=b>B</option></select>
    <input id=file type=file><button id=dialog onclick="confirm('Continue?')">Dialog</button>
    <div class=item>One</div><div class=item>Two</div>
    <div id=hidden style="display:none">Ready</div>
    <iframe id=frame srcdoc="<input aria-label='Frame input'>"></iframe>"""
    with sync_playwright() as p:
        browser=p.chromium.launch(executable_path="/usr/lib/chromium/chromium",args=["--no-sandbox"])
        page=browser.new_page();page.set_content(html)
        _,source,mapping=execute("web",[
            {"action":"fill","by":"label","selector":"账号","value":"tester"},
            {"action":"assert_value","by":"label","selector":"账号","value":"tester"},
            {"action":"check","selector":"#accept"},
            {"action":"assert_checked","selector":"#accept"},
            {"action":"uncheck","selector":"#accept"},
            {"action":"assert_checked","selector":"#accept","expected":False},
            {"action":"select","selector":"#options","value":"b"},
            {"action":"extract","selector":"#options","attribute":"value","variable":"choice"},
            {"action":"assert_count","selector":".item","expected":2},
            {"action":"assert_text","selector":".item","nth":1,"value":"Two","text_exact":True},
            {"action":"hover","selector":".item","nth":0},
            {"action":"dblclick","selector":".item","nth":1},
            {"action":"fill","by":"label","selector":"Frame input","frame":"#frame","value":"frame ok"},
            {"action":"upload","selector":"#file","value":"sample.txt"},
            {"action":"wait","selector":"#hidden","state":"hidden"},
            {"action":"assert_visible","selector":"#hidden","expected":False},
            {"action":"python","python":"page.evaluate(\"setTimeout(() => document.querySelector('#hidden').style.display = 'block', 100)\")"},
            {"action":"wait","selector":"#hidden","state":"visible","timeout":1500},
            {"action":"assert_text","selector":"#hidden","value":"Ready"},
            {"action":"dialog","value":"accept"},
            {"action":"click","selector":"#dialog"},
            {"action":"python","python":"assert rt.variables['choice'] == 'b'\nassert page.locator('#file').evaluate('(e) => e.files[0].name') == 'sample.txt'"},
        ],page=page,base_url="http://target.test")
        assert page.frame_locator("#frame").get_by_label("Frame input").input_value()=="frame ok"
        browser.close()
    rows=json.loads(report.read_text())
    assert len(rows)==len(mapping) and all(row["status"] in {"passed","skipped"} for row in rows)
    assert "page.frame_locator(" in source and "locator.wait_for(state='hidden'" in source

def test_app_source_uses_platform_driver_and_wait_conditions():
    _,source,mapping=compile_steps_details("app",[
        {"action":"set_variable","variable":"name","expected":"tester"},
        {"action":"fill","by":"id","selector":"input","value":"{{name}}"},
        {"action":"wait","by":"id","selector":"spinner","state":"detached"},
        {"action":"assert_visible","by":"id","selector":"spinner","expected":False},
        {"action":"extract","by":"id","selector":"title","attribute":"text","variable":"title"},
    ])
    compile(source,"test_case.py","exec")
    assert "driver, base_url" in source and "WebDriverWait(driver," in source
    assert "not d.find_elements" in source and "invisibility_of_element_located" in source
    assert "element.get_attribute('text')" in source and len(mapping)==5


def test_python_skip_is_recorded_as_skipped_not_passed(tmp_path,monkeypatch):
    from testpilot.runners import steps as legacy
    original=runtime.StepRuntime
    report=tmp_path/"steps.json"
    monkeypatch.setattr(legacy,"StepRuntime",lambda *args,**kw:original(*args,report_path=str(report),**kw))
    with pytest.raises(pytest.skip.Exception):
        execute("app",[{"action":"python","python":"import pytest\npytest.skip('请补充测试')"}],driver=None,base_url="http://target.test")
    assert json.loads(report.read_text())[0]["status"]=="skipped"
