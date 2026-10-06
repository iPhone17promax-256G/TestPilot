# 控制块的结构分析：if/repeat 等开始标记需要与 end 配对。
# 这里不执行测试，只计算开始、else、结束之间的位置关系，供校验与编译共用。

"""Flat start/end controls keep every operation editable and independently mapped."""
STARTS = {"if", "repeat", "foreach", "once", "transaction"}

# 用栈处理嵌套：遇开始入栈，遇 end 出栈；超层数、无对应开始、重复 else 都是配置错误。
def block_links(steps):
    stack, links, alternatives = [], {}, {}
    for i, s in enumerate(steps):
        action = s.get("action")
        if action in STARTS:
            if len(stack) >= 6: raise ValueError("控制块最多嵌套 6 层")
            stack.append(i)
        elif action == "else":
            if not stack or steps[stack[-1]]["action"] != "if" or stack[-1] in alternatives:
                raise ValueError("否则步骤必须位于一个尚无否则分支的条件块中")
            alternatives[stack[-1]] = i
        elif action == "end":
            if not stack: raise ValueError("结束步骤没有对应的条件/循环/控制器")
            start = stack.pop()
            if i == start+1: raise ValueError("控制块中至少需要一个步骤")
            links[start] = (i, alternatives.get(start))
    if stack: raise ValueError("条件、循环和控制器必须有对应的 end 结束步骤")
    return links


# 返回真正启用的步骤下标；外层控制块禁用时，其内部步骤也不能算作有效执行内容。
def active_step_indices(steps):
    """Potentially executed steps, excluding every disabled control subtree."""
    links = block_links(steps)
    blocked_until = -1
    active = set()
    for index, step in enumerate(steps):
        if index < blocked_until:
            continue
        if not step.get("enabled", True):
            if index in links:
                blocked_until = links[index][0] + 1
            continue
        active.add(index)
    return active
