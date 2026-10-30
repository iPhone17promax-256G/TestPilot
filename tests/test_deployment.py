"""双模式真实认证边界、账号隔离、加密存储与无源码安装配置。"""
import json
import os
import subprocess
import sys
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from testpilot.api import app
from testpilot.config import settings
from testpilot.storage import User,Record,session,uid
from testpilot.authentication import password_hash,token_create


@pytest.fixture
def users():
    identities=[uid('USER'),uid('USER')]
    with session() as db:
        for identity,role in zip(identities,['admin','member']):db.add(User(id=identity,username=identity,password_hash=password_hash('AcceptanceOnly2026!'),role=role))
    tokens=[token_create(identity,'dual-mode-check')['token'] for identity in identities]
    yield identities,tokens
    from testpilot.storage import ApiToken
    with session() as db:
        for row in db.scalars(select(Record).where(Record.kind.in_(['project','model_profile']))):
            if row.payload.get('ownerId') in identities:db.delete(row)
        for row in db.scalars(select(ApiToken).where(ApiToken.user_id.in_(identities))):db.delete(row)
        for identity in identities:db.delete(db.get(User,identity))


def test_local_has_identity_without_login_and_rejects_network_host(monkeypatch):
    monkeypatch.setattr(settings,'deployment_mode','local')
    client=TestClient(app, base_url="https://testserver")
    identity=client.get('/api/auth/me').json()
    assert identity['user']['id']=='LOCAL' and not identity['loginRequired'] and not identity['setupRequired']
    assert client.get('/api/state').status_code==200
    assert client.post('/api/auth/login',json=dict(username='ignored',password='ignored-password')).status_code==409
    assert client.post('/api/auth/password',json=dict(current_password='ignored-password',new_password='new-password')).status_code==409
    assert client.get('/api/state',headers={'Host':'malicious.example'}).status_code==403
    assert client.post('/api/tools/cases_list',json={},headers={'Origin':'https://malicious.example'}).status_code==403


def test_server_still_requires_login_and_ignores_claimed_account(monkeypatch,users):
    monkeypatch.setattr(settings,'deployment_mode','server')
    monkeypatch.setattr(settings,'managed_executor',True)
    monkeypatch.setattr(settings,'managed_token','internal-secret-with-more-than-32-characters')
    client=TestClient(app, base_url="https://testserver")
    assert client.get('/api/state',headers={'X-TestPilot-Managed-Account':users[0][0]}).status_code==401
    assert client.get('/api/internal/accounts',headers={'Authorization':'Bearer '+users[1][0]}).status_code==403
    internal={'Authorization':'Bearer '+settings.managed_token,'X-TestPilot-Managed-Account':users[0][0]}
    assert client.get('/api/executors/identity',headers=internal).json()['id']==users[0][0]
    assert client.get('/api/executors/identity',headers=dict(internal,**{'X-TestPilot-Managed-Account':'NO-SUCH-ACCOUNT'})).status_code==403
    assert client.get('/api/internal/accounts',headers={'Authorization':'Bearer '+settings.managed_token}).status_code==200


def test_project_members_and_install_admin_boundary(monkeypatch,users):
    monkeypatch.setattr(settings,'deployment_mode','server')
    monkeypatch.setattr(settings,'managed_executor',True)
    owner=TestClient(app, base_url="https://testserver",headers={'Authorization':'Bearer '+users[1][0]})
    other=TestClient(app, base_url="https://testserver",headers={'Authorization':'Bearer '+users[1][1]})
    project=owner.post('/api/projects',json={'name':'private'}).json()
    headers={'X-TestPilot-Project':project['id']}
    assert other.get('/api/state',headers=headers).status_code==403
    assert project['id'] not in [p['id'] for p in other.get('/api/state').json()['projects']]
    assert other.post('/api/projects',json={'project_id':project['id'],'name':'overwrite'}).status_code==403
    assert owner.post('/api/projects/'+project['id']+'/members',json={'members':[users[0][1]]}).status_code==200
    assert other.get('/api/state',headers=headers).status_code==200
    assert other.post('/api/projects/'+project['id']+'/members',json={'members':[]}).status_code==403
    assert other.post('/api/runtime/components/install',json={'component':'performance'}).status_code==403
    assert other.post('/api/admin/accounts',json={'username':'new_member','password':'new-member-password'}).status_code==403
    assert owner.get('/api/runtime/pair').status_code==404


def test_server_vault_encrypts_migrates_and_binds_account_and_model(tmp_path,monkeypatch):
    from testpilot.local_vault import LocalVault,ServerVault
    monkeypatch.setattr(settings,'client_root',tmp_path)
    old=LocalVault('USER-A');old.keyring=None
    old.put('MODEL-A','acceptance-demo-secret','https://provider.invalid/v1')
    vault=ServerVault('USER-A')
    assert not old.path.exists()
    assert 'acceptance-demo-secret' not in vault.path.read_text()
    assert vault.for_profile(dict(id='MODEL-A',baseUrl='https://provider.invalid/v1'))=='acceptance-demo-secret'
    with pytest.raises(ValueError):vault.for_profile(dict(id='MODEL-A',baseUrl='https://different.invalid'))
    other=ServerVault('USER-B');other.root.mkdir(parents=True,exist_ok=True)
    other.path.write_text(vault.path.read_text())
    with pytest.raises(ValueError):other.get('MODEL-A')
    assert vault.path.stat().st_mode&0o777==0o600
    assert (tmp_path/'.secrets/model-master.key').stat().st_mode&0o777==0o600
    vault.delete('MODEL-A');assert vault.get('MODEL-A')==''


@pytest.mark.parametrize('mode',['local','server'])
def test_installer_private_source_free_config_and_no_mode_bypass(tmp_path,mode):
    package=Path(__file__).parents[1]/'deploy/package'
    env=dict(os.environ,TESTPILOT_CONFIG_ROOT=str(tmp_path),TESTPILOT_INSTALL_MODE=mode,TESTPILOT_INSTALL_HOST_PATH='/opt/中文 TestPilot',TESTPILOT_INSTALL_PORT='8099',TESTPILOT_INSTALL_NAME='testpilot-check')
    def run():return subprocess.run([sys.executable,str(package/'generate_config.py')],env=env,capture_output=True,text=True)
    assert run().returncode==0
    original=(tmp_path/'api.env').read_bytes()
    assert run().returncode==0 and (tmp_path/'api.env').read_bytes()==original
    values=dict(line.split('=',1) for line in (tmp_path/'install.env').read_text().splitlines())
    assert values['TESTPILOT_BIND']==('127.0.0.1' if mode=='local' else '0.0.0.0')
    assert (tmp_path/'executor.env').stat().st_mode&0o777==0o600
    assert (tmp_path/'test.env').stat().st_mode&0o777==0o600
    assert 'TEST_PASSWORD=\n' in (tmp_path/'test.env').read_text()
    assert 'TESTPILOT_DATABASE_URL' not in (tmp_path/'executor.env').read_text()
    env['TESTPILOT_INSTALL_MODE']='server' if mode=='local' else 'local'
    assert run().returncode!=0
    compose=(package/'compose.yaml').read_text()
    assert 'build:' not in compose and '../src' not in compose and '8021:8021' not in compose


def test_installer_component_allowlist():
    from testpilot.components import install
    with pytest.raises(ValueError):install('../../arbitrary-shell')
    with pytest.raises(ValueError):install('performance','start')


def test_first_admin_setup_can_return_identity_after_commit(monkeypatch):
    """回归首次安装真实故障：事务已提交，但关闭会话后读取身份造成 HTTP 500。"""
    from testpilot import authentication
    from testpilot.storage import ApiToken,LoginSession
    saved=[]
    # 测试数据库是独立临时库；暂时移走测试账号，验证一次真实首个管理员事务后恢复。
    with session() as db:
        for user in db.scalars(select(User).where(User.id!='LOCAL')):
            saved.append(dict(id=user.id,username=user.username,password_hash=user.password_hash,role=user.role))
            db.delete(user)
    created=None
    try:
        created=authentication.setup_account('first_admin_check','AcceptanceOnly2026!')
        assert created['username']=='first_admin_check' and created['role']=='admin'
        result=authentication.login_account('first_admin_check','AcceptanceOnly2026!')
        assert result and result[1]['id']==created['id']
    finally:
        with session() as db:
            found=db.scalar(select(User).where(User.username=='first_admin_check'))
            if found:
                for row in db.scalars(select(LoginSession).where(LoginSession.user_id==found.id)):db.delete(row)
                db.delete(found)
            for data in saved:db.add(User(**data))
