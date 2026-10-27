# 众源感知解译工具 v0.5.0 · 前端 UI 重构方案

> 现状基准：`web/index.html`（1038 行，HTML+CSS+JS 全内联） · 设计基准：`ui-redesign/`（已通过质量审查的分层版本）
> 打包：`build.spec` 为 `datas=[("web","web")]` 整目录打包，**拆分文件后打包配置无需改动**

---

## 一、现状梳理

### 1.1 布局骨架

| 区域 | 现状 | 问题 |
|---|---|---|
| 左导航 rail | 深色 236px：品牌区 + v0.5.0 徽章 + 3 导航项（各带副标题）+ 底部模型状态灯 | 徽章用渐变、导航图标有彩色投影，与"精密仪器"调性冲突 |
| 内容区 | `max-width:1280` 单列纵向流；`.grid2` 为 `5fr 6fr` 双栏 | **默认窗口下 max-width 根本没生效**——真实宽度是 1067px，右栏仅约 577px，日志与表格拥挤 |
| 页头 | 图标 + 标题 + 副标题 + 3 步流程指示 + 主按钮 | 步骤指示与 CTA 并列在页头，关系含糊 |

**真实宽度三档**（app.py：`width=1360, height=900, min_size=(1080,700)`，内容区 = 视口 − 236 − padding − 滚动条）：

| 档位 | 视口 | 内容区 | 形态 |
|---|---|---|---|
| A 宽 | ≥1440 | 1156–1280（封顶） | 双栏 5:6 |
| B 标准（默认 1360） | 1184–1439 | 900–1155（**约 1067**） | 双栏 5:6 |
| C 紧凑（最小 1080） | 1080–1183 | **787–899** | 需塌成单栏 |

### 1.2 功能模块（三页，Tab 互斥）

1. **车载影像提取**：数据源目录 / 输出目录 / 置信度 / 最大影像数（0=不限）→ 扫描校验 → 进度（大百分比 + 四格统计 + 错误框）+ 日志 → 结果（SHP 批次统计表 + 打开输出目录）→ 文件队列（通栏独立卡）
2. **正射影像提取**：TIF / 输出目录 / 存量 GDB（可选）/ 最短标线 + 两个开关 → 进度 + 日志 → 结果（统计 + 叠加预览图）
3. **变化比对**：新成果 GDB / 存量 GDB / 底图 TIF（可选，无输出目录，自动落存量同级）→ 进度 + 日志 → 结果（匹配/新增/灭失/改动四色统计 + 分类表 + 预览图）

### 1.3 交互模式

轮询（400–500ms `setInterval` 调 `get_status` / `get_dom_status` / `get_cmp_status`）· Toast · 四态状态 chip · 日志语义着色 + 自动跟随 · 文件队列筛选 · 路径选择器（`pick_folder` / `pick_file`）· `open_folder` 打开目录 · 本地图片 base64 / `file://` 预览

### 1.4 问题清单（按影响排序）

| # | 问题 | 证据 |
|---|---|---|
| 1 | 单文件巨石，无可复用组件 | 11 处手写 `card-head`；三套近乎相同的轮询（`poll`/`pollDom`/`pollCmp`）与三套 chip 字典 |
| 2 | 无间距/字号体系 | 间距全是魔法数字 6/8/10/11/13/14/16/18/22；字号达 14 档（10→38） |
| 3 | 响应式缺位 | 仅靠 `flex-wrap`；日志 300px、文件队列 340px 高度写死；窗口缩小无退化 |
| 4 | 字段宽度规则隐晦 | `w1`/`w2`/`w3` 无语义，新增字段不知用哪个 |
| 5 | 视觉噪音 | 11 个彩色渐变图标块抢戏，压过真正该被看见的进度与统计 |
| 6 | 对比度不达标 | 分类标签 2.82:1、青/橙页 ghost 按钮 3.12–3.28（12px 以下小字） |
| 7 | 三处实现不一致 | ①「打开日志文件」实际打开输出目录 ② 三个主按钮就绪校验不统一 ③ 最大影像数 0=不限 语义隐晦 |

---

## 二、重构目标（冲突时按序号让位）

1. **长时监控的可读性**（最高）——日志与进度是用户盯得最久的东西，任何"为了好看"降低可读性的改动都应否决
2. **状态可辨识性**——一眼回答：在跑吗？跑到哪了？出错没？结果在哪？
3. **操作路径的确定性与防错**——统一"就绪态"表达
4. **一致性与可维护性**——下次加功能页不用复制粘贴
5. **窗口缩放的优雅退化**——787px 塌单栏，高度改弹性
6. **视觉品质**（最低）——这是内业工具不是官网

---

## 三、新 UI 设计方向与设计规范

### 3.1 方向：Tech Utility / 精密仪器感（Linear 语言）

> **纲领：灰阶做结构，等宽做数据，主题色只做「当前页身份 + 主动作」，彩色只做状态与结果。**

核心动作：
- 11 个彩色渐变图标块 → **淡色底 + 1px 同色描边 + 单色图标**；全页仅页头图标与导航激活块保留实色
- 删除全部彩色 glow、`translateY` 位移动画、渐变文字；阴影降到 `sh-1` 级，靠 1px 描边建立层级
- **失败数 = 0 时失败格降级中性灰**（页面不常红）；耗时格改中性（耗时不是状态）
- 蓝色专供「运行中/信息」，`--ac` 只驱动按钮、开关、焦点环、导航激活块、页头图标
- `same 未变` pill 由蓝改中性灰——未变化是中性事实，不该占用状态色

### 3.2 设计令牌（`css/tokens.css`）

| 类别 | 内容 |
|---|---|
| 主色 | **唯一主色**沉靛蓝 `--brand #3E63D8`（配 `-d #2F4BA8` / `-050 #EDF2FE` / `-100 #D8E2FA` / 文本级 `-t #2743A5`）。色相 H226（避开 H220 的"默认链接蓝"）、饱和度 79%→66%——这是去掉廉价感的关键。原蓝/青/橙三主色体系已废止：青完全退场，橙降级为纯语义 warning，紫退场 |
| 语义色 | success / warning / danger 三个色相，只承载「状态与结果」，各含 **fg / bg / border 三件套**；info 与 running 复用主色同色同义 |
| 文本级色 | `--ac-t`（blue `#1E55C8` / teal `#09786F` / amber `#9A5800`）——小字用色，保证 ≥4.5:1；`--ac-btn`（按钮实底专用，仅 teal/amber 加深） |
| 中性 | `--ink-1…5`、`--line-soft/#line/#line-2/#line-strong`、`--bg-app/card/sunken/zebra` |
| 深色区 | `--rail-*`（导航）、`--term-*`（终端日志，含 ok/err/warn/info 四色） |
| 间距 | 4px 阶梯 `--sp-1h(6) 1(4) 2(8) 3(12) 4(16) 5(20) 6(24) 7(32) 8(40)` |
| 字号 | 8 档 `--fs-xs(10.5) sm(11.5) base(12.5) md(13) lg(14.5) xl(19) 2xl(24) 3xl(38)` + `--fs-mono(12)`；补充档 `--fs-nav(13.5)` `--fs-unit(15)` |
| 圆角/阴影 | `--r-sm8 / md12 / lg16 / tag4 / pill999`；`--sh-1`（卡片，几乎不可见）/ `--sh-2`（浮层）/ `--sh-3`（模态） |
| 动效 | `--dur-1…4(120/180/260/420ms)`、`--ease`、`--ease-out`；pulse 1.4s / shine 1.8s；`prefers-reduced-motion` 全局降级 |
| 尺寸 | `--ctrl-h 38 / sm32 / xs28`、行高 32/40/36、`--ic-1…4`、`--log-h:clamp(180px,30vh,340px)`、`--list-h:clamp(200px,32vh,380px)` |

主题化沿用「单变量驱动整页」机制，行内 `style="--ac:var(--blue)"` 升级为 `data-ac="brand"`，三页统一为同一主色。**色彩预算：全 UI 只剩 4 个色相——靛蓝（主色，只出现在导航激活块 / 页头图标 / 主动作按钮 / 进度与开关 / 焦点环 / 空态圈）+ 绿·琥珀·红（只出现在状态与结果）+ 中性灰阶与等宽数字构成其余全部。** 分类标签与批次标签已收为中性灰阶等宽（原四色编码属冗余，是"五颜六色"的噪音峰值）。

### 3.3 组件规范（17 类，详见 `css/components.css` / `pages.css`）

card / card-head / ticon（5 变体 × 3 尺寸）/ page-head + steps（3 态）/ field（语义化宽度）/ btn（primary·sm·xs·ghost·danger + 禁用态不用 opacity）/ chip（4 态 + warn）/ log（终端风 + 工具条 + 跟随回落）/ 进度区（pct·bar·bar-meta·tiles）/ tile·stat / file 队列（fchip·frow·fsess·ftag·catchip）/ switch / pill（4 态）/ table / pathline / empty / hint / error-box / toast（改右下角，错误 6s 带关闭）

### 3.4 响应式规则

```css
.grid2{display:grid;grid-template-columns:minmax(0,5fr) minmax(0,6fr);gap:16px;align-items:start}
@media (max-width:1183px){
  .grid2{grid-template-columns:minmax(0,1fr)}
  :root{--log-h:clamp(220px,38vh,420px); --list-h:clamp(240px,40vh,460px)}
  main{padding:16px 20px 20px}
}
```
- 单栏堆叠顺序：配置 → 进度 → 统计结果 → 日志 → **文件队列（通栏独立卡，排最后）**
- tiles / stat4 用 `repeat(auto-fit,minmax(148px,1fr))`，787 下仍保持 4 列不塌（统计数字是核心诉求）
- C 档：页头换行（steps 转第二行）、表格隐藏次要列 `.col-opt`
- 不做 <787px；溢出铁律：flex 子项 `min-width:0`，路径 `word-break:break-all`

---

## 四、组件拆分与样式组织

### 4.1 目标文件结构（已落地于 `ui-redesign/`）

```
web/                          ← 落地目标
  index.html                  入口名不变（app.py 指向它），仅保留语义化结构 + 相对路径引用
  css/
    tokens.css                仅 :root 变量与 .page[data-ac] 主题覆盖
    base.css                  唯二允许裸标签选择器的文件：reset / 滚动条 / focus-visible / u-* 工具类
    layout.css                rail / page-head / steps / grid2 + 全部 @media
    components.css            btn card ticon field switch chip log 进度 tile stat table pill pathline empty hint err-box toast
    pages.css                 三页差异：文件队列 / 筛选 / 分类标签 / 结果细部
  js/
    api.js                    pywebview 桥接封装（call / pickInto / openDir）
    ui.js                     公共 UI 函数：el / esc / toast / 格式化 / 图标 / 日志面板 / chip / steps / bindReady
    pages/car.js  dom.js  cmp.js   三页各自的字段、校验、轮询与渲染
    app.js                    启动 / 导航 hidden 切换 / 模型状态 / 装配
    mock.js                   仅演示用，真实环境前置守卫 `if (window.pywebview) return;` 完全不生效
```

### 4.2 命名约定

1. 组件根类名**沿用现有短名**（`.card .btn .chip .log .tile .stat .frow .pill .table .pathline .empty .hint .error-box .toast .ticon`）——零迁移成本
2. 修饰符 BEM 双连字符：`.btn--sm` `.chip--run` `.tile--bad` `.ticon--ac`
3. 状态 `is-` 前缀：`.is-on .is-run .is-cur .is-done .is-follow`
4. 工具类 `u-`：`.u-mono .u-num .u-trunc`
5. JS 钩子只用 `data-*`（`data-ac` `data-state`），禁止用 class 做逻辑判断
6. 禁止：components/pages/layout 中出现裸标签选择器；禁止 `!important`（除 reduced-motion）；禁止新增行内 style

### 4.3 JS 去重重点

- 三套轮询（400/500/500ms）→ 参数化公共轮询，页面只传 statusFn / renderFn
- 三套 chip 字典（`CHIP`/`DOM_CHIP`/`CMP_CHIP`）→ 合并为一份 + 告警态
- 三套就绪校验 → `AVT.ui.bindReady(btnId, fieldIds, extra)` 同源

---

## 五、分阶段实施步骤

> 总原则：**每个阶段结束都能独立运行、可回滚**；优先做"结构不动、只换底层"的低风险阶段。

### 阶段 0 · 准备（0.5h）
- `git` 打 tag `ui-refactor-baseline`；备份 `web/index.html` → `web/index.legacy.html`（临时，阶段 6 后删除）
- 确认 `build.spec` 无需改动；确认 `app.py` 入口仍为 `web/index.html`
- **验收**：`git tag` 存在，备份文件可打开

### 阶段 1 · 令牌化（1–2h，视觉几乎不变）
- 新建 `css/tokens.css`，把 `:root` 旧变量替换为新令牌体系，并按映射表替换魔法数字（间距/字号）
- `index.html` 加一行 `<link>`
- **验收**：界面与改造前逐像素对比无明显差异；三页主题色正常
- **回滚**：删 link + 还原 `:root`

### 阶段 2 · 样式分层（2–3h）
- 把 `<style>` 块按职责拆成 `base / layout / components / pages` 四个文件，按序 link
- **验收**：`AVT_SELFTEST` 冻结包自检通过；打包后页面样式完整（重点验证 `datas` 打包的子目录 CSS 能加载）
- **回滚**：还原单文件

### 阶段 3 · 组件语义化改造（3–5h，逐组件推进）
按 一个组件 → 三页同时受益 的顺序：
1. `ticon` 降饱和（11 处）— 视觉变化最大，先做，便于确认方向
2. `w1/w2/w3` → `field--path/text/num/sel`
3. `btn` / `chip` / `table` / `pathline` / `hint` / `empty` / `error-box` / `toast` 统一
4. 页头 steps 三态 + **主 CTA 移到执行卡 card-head 右端**
5. 进度区（pct 去渐变、失败格 0 值降级、耗时格中性）+ 日志面板（工具条、clamp 高度、跟随回落）
- **验收**：每完成一个组件跑一遍三页 × 两态；对比度脚本实测 ≥4.5:1（小字）
- **回滚**：按组件粒度 `git checkout`

### 阶段 4 · JS 模块化（3–4h）
- 抽 `api.js` / `ui.js`；三页逻辑拆到 `js/pages/*.js`；`app.js` 负责装配与导航
- 合并三套轮询与 chip 字典；统一 `bindReady`
- 修三处一致性问题：①「打开日志文件」→「打开输出目录」② 三页主按钮同源就绪校验 ③ 最大影像数 placeholder 标注「0 = 不限」
- **验收**：`grep` 核对 14 个 API 方法名与调用参数全部保留；`node --check` 全过；三页轮询与切页保状态正常
- **回滚**：还原到阶段 3 末的 commit

### 阶段 5 · 响应式与可达性（1–2h）
- `grid2` 断点、`--log-h`/`--list-h` clamp、C 档单栏堆叠与次要列隐藏
- 补全 `focus-visible`（nav / fchip / log-jump / err-box__more / btn）
- **验收**：1080 / 1183 / 1360 / 1600 四档 × 两态无错位；键盘 Tab 全程可见焦点环

### 阶段 6 · 验收与落地（1h）
- 冻结包自检 `AVT_SELFTEST=gdb|rasterio|dom` 三项全 PASS
- 真机跑一次完整流程：车载提取（含失败文件）→ 正射提取 → 变化比对
- 删除 `web/index.legacy.html` 与 `mock.js`（或保留 mock 但确保 `if (window.pywebview) return;` 前置守卫）
- **验收**：`dist/AI-Vision-Tool.app` 启动正常、三页功能与改造前一致

> 快速路径：若不想逐步替换，`ui-redesign/` 已是完整可运行版本，整目录内容拷入 `web/` 即可（build.spec 整目录打包兼容），但**建议仍按阶段 0 → 6 的验收清单跑一遍**。

---

## 六、必须保留不变的部分（红线）

### 6.1 后端契约（api.py 零改动）
14 个方法名与参数一字不改：`ping` `model_status` `pick_folder` `pick_file` `open_folder` `scan_directory` `start_pipeline` `get_status` `start_dom` `get_dom_status` `start_compare` `get_cmp_status` `preview_image` `read_text_file`（后者前端从不调用，保留接口不做 UI）

### 6.2 交互机制
- 轮询 400/500ms `setInterval`，不引入 WebSocket/SSE（避免动 Python 后端）
- **Tab 用 `hidden` 切换、不销毁 DOM、切页保留进度/日志/结果、切页 `scrollTop=0`**（极易被"现代化改造"破坏）
- 日志语义着色正则、增量追加、底部自动跟随（用户上滚不打断）
- 文件队列全部/成功/失败筛选、行内状态图标、spinner、分类标签
- 进度大百分比 + 流光进度条 + 四格统计（总数/成功/失败/已用时）
- Toast、四态状态 chip、路径选择器、`open_folder` 打开目录

### 6.3 业务规则与文案
- 三页字段集合与校验规则不变；最大影像数 0 = 不限；变化比对无输出目录（自动落存量同级）
- 所有中文标题、副标题、提示、按钮文案原样保留（副标题写得很好，是隐性资产）
- 三页功能定位与文案不变；单主色 `#3E63D8` 全站统一（页面可辨识靠导航激活块位置 + 页头图标字形 + 页面标题，不靠颜色）

### 6.4 环境约束
零外部资源（无 CDN、无外链字体/图标，图标全内联 SVG）· 无构建工具链 · **禁用 ES modules**（file:// 下 CORS 会拦），用经典 script + `window.AVT` 命名空间 · 入口仍为 `web/index.html`

---

## 七、质量审查结论（原型已过审）

| 维度 | 评分 | 说明 |
|---|---|---|
| 设计哲学 | 5/5 | 灰阶/等宽/主题色分工彻底贯彻，渐变残留清零 |
| 视觉层次 | 5/5 | 38px 百分比为明确焦点，单栏退化顺序正确 |
| 执行质量 | 4/5 | 对比度 32 项实测全 PASS（修复后），焦点环已补全 |
| 特异性 | 4/5 | 终端日志 + 通栏批次文件队列 + 单主色页身份，明显是测绘内业仪器 |
| 克制 | 5/5 | 无装饰性渐变/blob/glow，动画 ≤260ms |

修复记录：P0 对比度（新增 `--ac-t` / `--ac-btn` 文本级与按钮级色，9 处替换，含日志工具条 toggle 悬停态 2.75→4.92）· P1 焦点可见（4 处）· P1 `domOpenOut` 恢复 `lastDomResult.gdb` 兜底 · P2 `curFile` 补 title。

**已接受的两处豁免**（不再改动）：
- 中性灰系 4.21–4.49（`tile__k` / `chip--idle` / `chip--warn` / `pill--mod` / `ftag--pending` / `th`）——相对原版已改善约 1.0（原 `--ink-3 #7C8AA0` 仅约 3.5），且中性灰明度节奏是「灰阶做结构」纲领本身要求，继续压深会伤害层级
- 装饰性图形 `.ticon--ac` 图标（teal 2.95 / amber 2.82）——信息由卡标题承载，属 WCAG 装饰性豁免

---

## 八、遗留决策与风险

1. **主 CTA 位置**：现放执行卡 head 右端（与状态 chip 并排）。若希望离配置字段更近，可移到配置卡 head 右端
2. ~~C 档文件队列位置~~ **已解决**：文件队列已从进度卡内移出，成为页面级通栏独立卡片。原因是双栏下文件行需约 448px 但仅约 430px 可用，1360 档文件名被裁到只剩 7 个字符；移出后单行元素全部完整，且单栏顺序变为 配置→进度→结果→日志→队列
3. **DOM 页第 2 步**：「指定存量 GDB（推荐）」为可选字段，完成态按是否填充判定
4. **新增文案**（原版没有，需人工过一遍）：结果卡空态句、cmp 结果图例、日志工具条（自动滚动/复制/清空/已暂停跟随）、错误框（提取中断/展开完整信息/收起）、Toast 关闭按钮
5. **演示态去留**：`js/mock.js` 仅用于评审演示，真实环境前置守卫不生效；上线前可删
6. **风险点**：阶段 2 拆分 CSS 后必须实机验证打包产物能加载子目录资源——这是整条链路唯一有环境差异的环节

---

## 九、落地记录（web/ 已替换）

- `web/` 已按 `ui-redesign/` 部署：`index.html` + `css/{tokens,base,layout,components,pages}.css` + `js/{api,ui,app}.js` + `js/pages/{car,dom,cmp}.js`；入口名 `web/index.html` 不变，`app.py`、`build.spec` 无需改动
- 旧版备份：`.workbuddy/backups/index.legacy.html`（也可 `git checkout -- web/index.html` 还原）
- 生产副本**不含** `mock.js` 与演示条（真实环境下有前置守卫也无需保留，避免死代码）
- **修复一处阻断性缺陷**：`api.js` 的 `pickInto` 原先只写 `input.value`，程序化赋值不触发 `input` 事件 → 就绪校验不刷新 → 选完目录主按钮仍是灰的。现已手动派发 `input` 事件，三页 CTA 均验证可正常启用
- 冒烟结果（模拟 pywebview 环境）：CSS/JS 全部加载无 404、控制台 0 报错、模型状态经 `pywebviewready` 正常刷新、目录选择与扫描提示正常、三页切换正常、两列底边差 0px、无横向溢出
