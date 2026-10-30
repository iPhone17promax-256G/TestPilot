"""旧共享数据库 Worker 入口已移除，避免形成另一条 MySQL Job 调度路线。"""

def main(kind):
    raise RuntimeError("共享数据库 Worker 已移除；请启动 python -m testpilot.local_executor")

if __name__ == "__main__":
    import sys
    main(sys.argv[1] if len(sys.argv)>1 else "agent")
