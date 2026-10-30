import csv
import xml.etree.ElementTree as ET
import pytest
from testpilot.performance import build_plan, materialize, read_metrics, PerformanceConfig, validate_plan
from testpilot.assets import validate_assets, version_hash

def test_execution_copy_enforces_environment_and_staged_load_without_modifying_source():
    source=build_plan([{"name":"查询","path":"/api/info","headers":{"Authorization":"Bearer ${token}"}}])
    result=materialize(source,{"stages":[{"users":2,"seconds":5},{"users":4,"seconds":8}]},"http://target:8065")
    root=ET.fromstring(result)
    assert len(root.findall(".//ThreadGroup"))==2
    assert [n.text for n in root.findall(".//stringProp[@name='ThreadGroup.num_threads']")]==["2","4"]
    assert [n.text for n in root.findall(".//stringProp[@name='ThreadGroup.delay']")]==["0","5"]
    assert {n.text for n in root.findall(".//stringProp[@name='HTTPSampler.domain']")}=={"target"}
    assert len(ET.fromstring(source).findall(".//ThreadGroup"))==1

def write_results(path,rows):
    with path.open("w",newline="") as f:
        writer=csv.DictWriter(f,fieldnames=["timeStamp","elapsed","label","success","responseCode","failureMessage","allThreads"])
        writer.writeheader();writer.writerows(rows)

def test_warmup_is_excluded_and_business_failures_are_not_hidden(tmp_path):
    path=tmp_path/"results.jtl"
    write_results(path,[dict(timeStamp=t,elapsed=e,label="查询",success=s,responseCode="200",failureMessage="",allThreads="2") for t,e,s in [(1000,999,"true"),(2500,30,"true"),(3500,900,"false")]])
    result=read_metrics(path,{"duration_seconds":4,"ramp_seconds":1,"warmup_seconds":1,"p95_ms":500},final=True)
    assert result["samples"]==2 and result["failed"]==1
    assert result["p95"]==900 and result["verdict"]=="failed"

def test_zero_samples_and_no_thresholds_are_not_performance_passes(tmp_path):
    missing=read_metrics(tmp_path/"empty",{"duration_seconds":3})
    assert missing["samples"]==0 and missing["rps"]==0 and missing["errorRate"] is None
    path=tmp_path/"results.jtl"
    write_results(path,[dict(timeStamp=1000,elapsed=10,label="GET",success="true",responseCode="200",failureMessage="",allThreads="1")])
    result=read_metrics(path,{"duration_seconds":3,"p95_ms":None,"max_error_rate":None},final=True)
    assert result["verdict"]=="not_evaluated"

def test_auxiliary_files_and_load_config_are_part_of_immutable_version():
    code="def test_a(): pass"
    assert version_hash(code,{}, {})!=version_hash(code,{"data.csv":"x\na\n"}, {})
    assert version_hash(code,{}, {"users":2})!=version_hash(code,{}, {"users":3})
    with pytest.raises(ValueError):validate_assets("api",code,"test_case.py",{"../data.csv":"x"}, {})

@pytest.mark.parametrize("config",[{"users":101},{"duration_seconds":2,"warmup_seconds":2},{"stages":[{"users":1,"seconds":1800},{"users":2,"seconds":2}]}])
def test_load_limits_cannot_be_bypassed(config):
    with pytest.raises(ValueError):PerformanceConfig.model_validate(config)

def test_invalid_jmx_is_rejected_before_execution():
    with pytest.raises(ValueError):validate_plan("<jmeterTestPlan><hashTree/></jmeterTestPlan>")
    with pytest.raises(ValueError):validate_plan('<!DOCTYPE x><jmeterTestPlan/>')


def test_agent_tool_preserves_version_config_instead_of_langchain_runtime_config():
    from langchain_core.tools import StructuredTool
    from testpilot.tools import SaveCase
    received={}
    def operation(**arguments):
        received.update(arguments)
        return "saved"
    tool=StructuredTool(name="cases_save_revision",description="save",args_schema=SaveCase,func=operation)
    tool.invoke(dict(name="APP",kind="app",code="def test_home(driver): pass",execution_config={"app_id":"APP-1","device_id":"DEVICE-1"}))
    assert received["execution_config"]=={"app_id":"APP-1","device_id":"DEVICE-1"}
    assert SaveCase.model_validate(dict(name="APP",kind="app",code="pass",config={"app_id":"APP-1"})).execution_config=={"app_id":"APP-1"}

def test_app_case_cannot_replace_managed_driver_fixture():
    with pytest.raises(ValueError,match="driver fixture"):
        validate_assets("app","def driver(): pass\ndef test_home(driver): pass","test_case.py",{}, {})
    validate_assets("app","def test_home(driver): assert driver is not None","test_case.py",{}, {})
