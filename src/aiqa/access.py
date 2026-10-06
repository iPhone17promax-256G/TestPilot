# 测试证据下载的授权检查：路径在数据目录内还不够，必须属于当前可访问的 Run/观察。
# 原生测试能写自己的工作区，因此要防它留下符号链接指向别的执行目录。

"""Evidence must belong to the selected project as well as stay inside the data directory."""
from .config import settings
from .storage import read_record, list_records

# 先按 runs/inspections 定位业务归属，再 resolve 文件并检查没有逃出该次执行目录。
def authorize_artifact(relative):
    if not relative or "\\" in relative or "\x00" in relative:
        raise ValueError("产物路径不合法")
    parts=relative.split("/")
    if len(parts)<3 or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("产物路径不合法")
    if parts[0]=="runs":
        read_record(parts[1],"run")
    elif parts[0]=="inspections":
        owned=any(i.get("id")==parts[1] for t in list_records("task") for i in t.get("inspections",[]))
        # Android observations use a scoped mobile session directory.
        owned=owned or any(parts[1]==s.get("inspectionId") for s in list_records("mobile_session"))
        if not owned:
            raise ValueError("当前项目中找不到该产物")
    else:
        raise ValueError("当前项目中找不到该产物")
    root=settings.data.resolve()
    folder=root/parts[0]/parts[1]
    target=(root/relative).resolve()
    # A test can create symlinks inside its writable mount. On the host these
    # must never redirect a download into another run or private data directory.
    if folder.resolve()!=folder or not target.is_relative_to(folder):
        raise ValueError("产物路径超出当前执行或观察目录")
    return target
