/** 一次性迁移浏览器里的项目选择、模型选择和执行器配对资料。
 * 只改存储键名，值按原样搬迁；成功后删除旧键，不在业务读取路径保留兼容分支。
 * 原始令牌不会上传到平台或写入日志。已有新键优先，重复加载不会覆盖新设置。
 */
export function migrateBrandStorage(storage: Storage): void {
  const legacyKeys: string[] = []
  for (let index = 0; index < storage.length; index++) {
    const key = storage.key(index)
    if (key?.startsWith('aiqa-')) legacyKeys.push(key)
  }
  for (const legacyKey of legacyKeys) {
    const value = storage.getItem(legacyKey)
    const targetKey = 'testpilot-' + legacyKey.slice('aiqa-'.length)
    if (value !== null && storage.getItem(targetKey) === null) storage.setItem(targetKey, value)
    storage.removeItem(legacyKey)
  }
}

// 在 App/store 导入前完成迁移，避免首次加载时读取到空的项目或执行器选择。
if (typeof window !== 'undefined') {
  try {
    migrateBrandStorage(window.localStorage)
    migrateBrandStorage(window.sessionStorage)
  } catch {
    // 浏览器禁止存储时不搬迁；写入失败也不会删除对应旧键。
  }
}
