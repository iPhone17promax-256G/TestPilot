# 发布者构建，使用已经锁定依赖的基础镜像；用户安装时只导入/拉取成品镜像。
ARG AIQA_BASE_IMAGE=aiqa-runtime:0.1
FROM ${AIQA_BASE_IMAGE}
COPY src /app/src
COPY skills /app/skills
COPY web/dist /app/web/dist
ENV PYTHONPATH=/app/src
