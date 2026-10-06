from collections import namedtuple
import pytest
from aiqa.runners import container


def test_output_budget_counts_nested_files_and_rejects_excess(tmp_path):
    nested=tmp_path/'nested'
    nested.mkdir()
    (nested/'first.txt').write_bytes(b'12345')
    (tmp_path/'second.txt').write_bytes(b'123456')
    container.check_storage(tmp_path,11)
    with pytest.raises(RuntimeError,match='产物超过'):
        container.check_storage(tmp_path,10)


def test_entry_budget_also_limits_empty_files_and_directories(tmp_path,monkeypatch):
    monkeypatch.setattr(container,'MAX_OUTPUT_FILES',2)
    (tmp_path/'a').mkdir()
    (tmp_path/'a'/'b').touch()
    (tmp_path/'c').touch()
    with pytest.raises(RuntimeError,match='产物超过'):
        container.check_storage(tmp_path)


def test_low_disk_space_stops_before_launch(tmp_path,monkeypatch):
    Usage=namedtuple('Usage','total used free')
    monkeypatch.setattr(container.shutil,'disk_usage',lambda folder:Usage(1000,999,1))
    with pytest.raises(RuntimeError,match='可用空间不足'):
        container.check_storage(tmp_path)


def test_symlinks_are_not_followed_or_packaged_and_targets_survive(tmp_path):
    workspace=tmp_path/'run'
    private=tmp_path/'private'
    workspace.mkdir();private.mkdir()
    secret=private/'secret.txt'
    secret.write_text('outside data')
    (workspace/'link-dir').symlink_to(private,target_is_directory=True)
    (workspace/'link-file').symlink_to(secret)
    with pytest.raises(RuntimeError,match='不允许符号链接'):
        container.check_storage(workspace)
    entries=list(container.workspace_entries(workspace))
    assert len(entries)==2 and all(link for _,link,_ in entries)
    assert container.discard_workspace_links(workspace)==2
    assert list(workspace.iterdir())==[] and secret.read_text()=='outside data'
    container.check_storage(workspace)


@pytest.mark.parametrize('workspace,status,deadline,removed',[
    ('workspaces/TASK-TEST','running','9999999999',False),
    ('workspaces/TASK-TEST','running','1',True),
    ('workspaces/TASK-TEST','completed','9999999999',True),
    ('runs/RUN-TEST','running','9999999999',False),
    ('runs/RUN-TEST','running','1',True),
    ('runs/RUN-TEST','cancelled','9999999999',True),
])
def test_cleanup_recovers_expired_auxiliary_and_test_containers(monkeypatch,workspace,status,deadline,removed):
    class Execution:
        labels={'aiqa.workspace':workspace,'aiqa.execution.deadline':deadline}
        deleted=False
        def remove(self,force=False):
            assert force
            self.deleted=True
    execution=Execution()
    class Containers:
        def list(self,filters):
            assert filters=={'label':'aiqa.execution=true'}
            return [execution]
    class Client:
        containers=Containers()
        closed=False
        def close(self):self.closed=True
    client=Client()
    monkeypatch.setattr(container.docker,'from_env',lambda:client)
    monkeypatch.setattr(container,'read_record',lambda identity,kind:{'status':status})
    container.cleanup_execution_containers()
    assert execution.deleted==removed and client.closed
