# 发布者构建，使用已经锁定依赖的基础镜像；用户安装时只导入/拉取成品镜像。
ARG TESTPILOT_BASE_IMAGE=testpilot-runtime:0.1
FROM ${TESTPILOT_BASE_IMAGE}
COPY src /app/src
COPY pyproject.toml README.md LICENSE THIRD_PARTY_NOTICES.md /app/
RUN pip install --no-cache-dir --no-deps .
COPY skills /app/skills
COPY examples /app/examples
COPY web/dist /app/web/dist
COPY scripts/setup_https.py /app/scripts/setup_https.py
ENV PYTHONPATH=/app/src
CMD ["python", "-m", "testpilot.serve"]
