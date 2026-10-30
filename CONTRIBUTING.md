# 参与 TestPilot

欢迎反馈问题、补充测试和改进代码。提交前请先搜索现有 Issue，避免重复。

## 报告问题

请提供部署模式（local / server）、操作步骤、预期与实际结果，以及相关 Task / Run ID。日志和截图请先移除 API Key、密码、Token、个人资料及被测系统的私密数据。

## 开发与检查

运行环境及首次证书信任见 [README](README.md)。日常开发使用：

```bash
bash scripts/start.sh --local --rebuild
python3 scripts/check.py
```

前端独立开发见 [web/README.md](web/README.md)。检查脚本使用临时 MySQL 数据库及独立 Redis 键前缀，不调用真实模型。

修改逻辑时补充能够验证行为的测试；PR 简要说明问题、改动和验证结果。界面改动可附截图。不要提交 `.env`、安装器生成的私有配置、证书私钥、运行数据或个人学习文档。

## 发布

安装镜像清单集中在 `deploy/package/images.conf`。改动发布包时同时验证安装器的离线预检和归档白名单，避免开发机器已有镜像掩盖新机器的缺项。

公开前检查目标分支及其历史，只推送准备公开的分支；本地备份分支和个人笔记不应通过 `--all` 或 `--mirror` 一起发布。
