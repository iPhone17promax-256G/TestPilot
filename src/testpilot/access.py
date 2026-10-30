# 测试证据下载的授权检查：路径在数据目录内还不够，必须属于当前可访问的 Run/观察。
# 原生测试能写自己的工作区，因此要防它留下符号链接指向别的执行目录。

"""Evidence must belong to the selected project as well as stay inside the data directory."""
from .config import settings
from .storage import read_record, list_records

# 先按 runs/inspections 定位业务归属，再 resolve 文件并检查没有逃出该次执行目录。
# 功能：验证测试证据确实属于当前可访问的 Run 或观察会话，再防止符号链接越过本次目录。
# 输入：relative 是相对 settings.data 的 runs/RUN-ID/... 或 inspections/观察ID/... 文件路径；归属从运行、Task 或 mobile_session 记录检查。
# 输出/影响：返回解析后的真实 pathlib.Path；路径或归属不合法抛 ValueError，只做授权和路径检查，不读取文件正文，也不保证文件已经存在。
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
        # Android 即时观察的截图和页面 XML 存在对应 mobile_session 的独立目录，下载时也按该设备会话的账号/项目归属检查，不开放整个设备产物目录。
        owned=owned or any(parts[1]==s.get("inspectionId") for s in list_records("mobile_session"))
        if not owned:
            raise ValueError("当前项目中找不到该产物")
    else:
        raise ValueError("当前项目中找不到该产物")
    root=settings.data.resolve()
    folder=root/parts[0]/parts[1]
    target=(root/relative).resolve()
    # 测试脚本能在自己的可写工作区创建符号链接；链接名在目录内不代表实际文件也在目录内，
    # 所以下载时先解析真实路径，再检查仍属于本次 Run 的目录，防止读到别的运行证据或私有账号文件。
    if folder.resolve()!=folder or not target.is_relative_to(folder):
        raise ValueError("产物路径超出当前执行或观察目录")
    return target
