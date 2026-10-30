import { createRouter, createWebHistory } from 'vue-router'

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/agent' },
    { path: '/agent/:id?', component: () => import('./views/AgentView.vue'), meta: { title: 'Agent 工作台' } },
    { path: '/cases/:id?', component: () => import('./views/CasesView.vue'), meta: { title: '测试用例' } },
    { path: '/runs/:id?', component: () => import('./views/RunsView.vue'), meta: { title: '运行记录' } },
    { path: '/mobile', component: () => import('./views/MobileView.vue'), meta: { title: '应用与设备' } },
    { path: '/projects', component: () => import('./views/ProjectsView.vue'), meta: { title: '项目与环境' } },
    { path: '/environment', component: () => import('./views/EnvironmentView.vue'), meta: { title: '运行环境' } },
    { path: '/settings', component: () => import('./views/SettingsView.vue'), meta: { title: '设置' } },
    { path: '/:pathMatch(.*)*', redirect: '/agent' },
  ],
  scrollBehavior: () => ({ top: 0 }),
})

// A deploy changes hashed chunk URLs. Route guards already handle unsaved changes.
router.onError((error,to)=>{
  if(/Failed to fetch dynamically imported module|Importing a module script failed|Loading chunk/.test(String(error))){
    const key='testpilot-chunk-reload:'+to.fullPath
    if(!sessionStorage.getItem(key)){sessionStorage.setItem(key,'1');window.location.assign(to.fullPath)}
  }
})
