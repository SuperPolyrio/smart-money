# Smart Money 首页

参考附件 `homepage-reference.png` 的独立前端实现。仅新增本目录；不修改仓库原有未提交内容、Python 业务规则、MAS、模拟跟随或发布规则。没有提交 Git、公开部署、连接钱包或下单。

**当前状态：页面已实现，数据为 Interactive demo，真实只读接口未接通，业务后台未启动。**

## 运行

从仓库根目录执行；本次环境为 Node 24.11.0、npm 11.6.1。

```bash
cd frontend
npm ci
npm run dev -- --port 5173 --strictPort
```

打开 <http://127.0.0.1:5173>。只监听本机，不需要 Python 环境、数据库、密钥或账户。

```bash
npm run typecheck
npm run build
npm run preview -- --port 4173 --strictPort
```

构建预览为 <http://127.0.0.1:4173>。入口是 `index.html → src/main.tsx → src/App.tsx`，没有新增后端路由。

## 文件与依赖

| 文件 | 职责 |
| --- | --- |
| `package.json`、`package-lock.json`、`vite.config.ts`、`tsconfig.json`、`.gitignore`、`index.html` | 独立前端安装、构建与入口；精确锁定依赖 |
| `src/main.tsx`、`src/App.tsx` | React 严格模式、首页构图、导航和页内状态 |
| `src/content.ts`、`src/Icon.tsx` | 首页主文案、自制 SVG 图标 |
| `src/GlobeStage.tsx`、`src/createGlobeScene.ts`、`src/motion.ts` | 单 Canvas 地球、资源生命周期、统一动效与暂停条件 |
| `src/Panels.tsx` | Explorer、搜索、原生模态框、详情与焦点管理 |
| `src/homeData.ts`、`public/demo-snapshot.json` | 唯一演示数据源、格式校验、统计；读取失败不补造数据 |
| `src/styles.css`、`src/tokens.css` | 黑灰视觉、卡片和各断点布局；token 起点复用附件 |
| `public/assets/` | 本地陆地 mask、场景导出的 poster、自制品牌标记 |
| `playwright.config.ts`、`tests/home.spec.ts` | 可重复的浏览器功能、边界与截图检查 |
| `verification/` | 本轮截图、实际录像和工具产生的验证结果 |

运行依赖为 React、React DOM、Three.js、Motion；开发依赖为 Vite、React 插件、TypeScript、类型声明与 Playwright。没有加入第二套动画、地球、路由、图表或状态库。版本以锁文件为准。

## 数据与交互边界

- 本地 JSON 包含 4 个合成钱包、4 个合成市场、5 条观察、3 个可读合成研究预览。固定截止时间为 **2026-09-28 09:30 UTC**，`(截止时间 − 24h, 截止时间]` 内有 4 条观察；较早的第 5 条只进入完整活动列表。
- 四项统计由同一集合计算。未知显示 `—`，成功读取空集合才显示 `0`。没有真实 API 模式或失败后改用 demo 的通道；API 导航明确说明未接入。
- 金额单位为 USDC，并明确为合成数据；没有 ROI、信心分或真实地址。详情直接展示 fixture 的历史资格、前向状态、跟随资格及研究状态，前端不重新判定。
- 搜索支持点击、Ctrl/Cmd+K、清空、加载、失败重试、无结果、方向键、Enter、Esc、焦点循环与恢复；搜索词只作为文本处理。快速输入取消旧的本地检索任务。
- Markets / Traders / Insights、主 CTA、三卡进入同一个页内 Explorer；支持板块、动作过滤和排序。观察浮层与列表打开同一个详情抽屉。没有收藏、登录、钱包连接、模型调用或订单入口。
- 地球仅为示意，弧线不关联钱包、转账或用户地理位置；页面明确标注 `Illustrative globe · not wallet locations`。

## 地球、动画与素材

一个动态导入的 Three.js 场景：24,358 个均匀球面采样陆地点、暗球面、薄边缘光、5 条装饰弧线、2 条淡轨道、最多 2 个移动亮点。初始化一次几何；每帧只更新时钟、旋转、少量顶点，不进行 React 状态更新。

自转约 0.3°/秒，指针偏转上限 2.5°，亮点循环约 3.5 秒。用户暂停、页面隐藏、舞台离屏或模态打开时停止循环；恢复不追赶暂停时间。卸载销毁 renderer、geometry、material、observer 和监听器。系统减少动态效果使用可订阅的 `matchMedia`，同时控制 DOM 与 WebGL。

手机、减少动态效果、资源/WebGL 失败或 context lost 使用同场景导出的 `globe-poster.webp`；手机可主动启用同一场景的低档模式。持续慢帧会先降低一次 DPR 和均匀抽样点数，仍慢则切静态图。没有用参考截图作网页背景或旋转素材。

- 陆地数据：[Natural Earth v5.1.2 的 110m land GeoJSON](https://github.com/nvkelso/natural-earth-vector/blob/v5.1.2/geojson/ne_110m_land.geojson)，[公共领域许可](https://www.naturalearthdata.com/about/terms-of-use/)。用等距圆柱投影栅格化为 1024×512 单色 PNG，陆地白、海洋黑；已检查撒哈拉、澳大利亚、太平洋、大西洋采样方向。约 9 KB。
- Poster：从实际场景导出，仅含球体与装饰线，无文字、地址或指标；约 161 KB，无需联网加载。
- 图标与卡片 SVG 为本轮自制；字体使用 Arial/Helvetica/系统回退，没有复制或分发系统字体文件，没有运行时字体 CDN。
- 参考 PNG 只用于视觉核对，未作为站点资源分发。其他旧原型附件不作为视觉方向。

## 浏览器验证与录屏

测试使用本机 Google Chrome。没有安装 Chrome 时可先执行 `npx playwright install chrome`。测试命令会复用本项目运行中的 5173 服务，或启动自己的本机 Vite 服务。

```bash
npm test
```

开发 URL 加 `?visual-test` 可固定真实 Three 时钟、姿态及 DOM 入场。该开关仅在开发构建存在，不改变数据或资格；生产构建不暴露场景调试对象。测试同时检查固定帧的连续截图一致性，没有自动生成一份基线来宣称与参考图逐像素一致。

截图、实际录屏与性能结果作为本轮验收产物保留在本地 `verification/` 目录，后续验收直接操作浏览器并按需录制。

| 交付 | 文件 |
| --- | --- |
| 桌面与平板 | `verification/home-1536x1024.png`、`home-1440x900.png`、`home-1280x800.png`、`home-768x1024.png` |
| 手机 | `verification/home-390x844.png`、`home-360x800.png`、`mobile-detail.png` |
| 搜索与详情 | `verification/search.png`、`observation-detail.png`、`research-detail.png` |
| 回退与错误 | `verification/reduced-motion.png`、`webgl-fallback.png`、`mask-fallback.png`、`data-error.png` |
| 实际动画 | `verification/homepage-animation.mp4`，1536×1024，12.12 秒 |
| 验证原始结果 | `verification/test-results.json`、`capture-results.json` |

## 实测与未完成项

类型检查、生产构建及 16 个 Playwright 测试通过；涵盖六种视口、搜索/导航/详情、筛选、时间窗、空值与读取失败、暂停/恢复、受控页面隐藏事件、离屏、系统动效设置变更、重复场景创建销毁、WebGL/context lost/mask 失败。正常路径无页面 JS 异常，测试期间没有外站请求。正式生产预览另做首页与交互 smoke 检查；没有运行 Python 业务测试或业务任务。

生产构建初始 JS 约 **104 KB gzip**，延迟地球模块约 **134 KB gzip**。Vite 对地球模块超过 500 KB 的未 gzip 体积给出建议警告；没有提高阈值来隐藏警告。初始及地球 gzip 体积均在附件建议预算内。

录屏测量环境：Linux 6.8.0-124-generic、Chrome 149.0.7827.196、ANGLE/SwiftShader 软件渲染、1536×1024、DPR 1、Vite 开发构建、同时录屏。2.32 秒观察窗内约 **16.8 rendered fps**，8 draw calls；**未达到 60fps 目标**。该次本地 LCP 约 **1.81s**、CLS **0.00029**，不代表真实用户 p75、INP 或其他设备表现。记录见 `capture-results.json`。

剩余限制：真实只读服务未接入，真机 GPU 流畅度、Safari/Firefox、移动设备软键盘及真实用户性能未验收。页面隐藏验证使用受控 visibility 事件，未声称完成操作系统级后台标签测试。没有声称完整无障碍认证。与参考图相比，系统字体字形、点阵明暗和弧线姿态存在差异；整体保留近黑底、两行灰白大字、右侧点阵地球、四统计及三卡构图。
