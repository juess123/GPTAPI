# 规格 → 模型（A′ 流水线 + 本机 Web 界面）

输入一张参考图、一份规格文档、一张材料价目表；输出一个 Blender 模型和一个 Excel 报价表。

模型本身**不直接产出文件**（网关的响应里没有附件字段），而是由模型写出生成脚本，
在本机执行脚本落盘 → 所以交付物是真二进制文件，不是文本伪装。

```
input/          参考图片 + 规格 .md + 价目表 .xlsx
   ↓  fast_workflow.py                     一次请求生成两个脚本
generated/make_blend.py + make_xlsx.py
   ↓  先执行 Blender，再执行 Excel
output/<时间戳>/final_quote.xlsx
output/<时间戳>/final_model.blend
```

Web 任务固定使用快速直出模式：一次模型请求同时生成两个脚本。它可以申请任务级新依赖、
联网获取资源、创建摄影棚、渲染预览和生成附属文件；两个主交付物名称仍然固定。
本机固定先执行 Blender，再执行 Excel，使 Excel 可以读取 Blender 写入输出目录的共享数据。

## 环境

首次使用或迁移到新电脑后，在 PowerShell 执行一次：

```powershell
Set-Location E:\GPTAPI3
& .\setup_environment.ps1
```

脚本会创建两个仅位于项目目录中的依赖层：

- `.venv/`：Web 服务、流水线和 Excel 脚本使用的普通 Python 环境。
- `.blender_packages/`：由 Blender 自带 Python 使用的第三方库目录。
- `webapp/workspaces/<任务号>/task_packages/`：模型按任务声明并自动安装的动态工具箱，
  内含相互隔离的 `python/`、`blender/` 和可复现环境清单 `environment.json`。

`requirements.txt` 与 `requirements-blender.txt` 同时是精确版本锁和自动安装白名单。
任务开始前会检查两套环境；只有白名单中的依赖缺失或版本不符时才自动安装。
安装过程使用 `.dependency-install.lock` 串行化，避免并发任务同时修改环境。
模型能力不受基础白名单限制：响应中的 `DEPENDENCIES` 声明会在脚本执行前自动安装到
当前任务目录。Git/URL 形式的依赖不接受，普通 PyPI requirement（含版本约束和 extras）均可。

- **Python 3.12** — `C:\Users\QRT\AppData\Local\Programs\Python\Python312\python.exe`
  > PATH 上的 `python` 是 WindowsApps 占位符，会报 `ModuleNotFoundError`，请用全路径或 `py -3.12`
- **Blender 5.2 LTS** — `C:\Program Files\Blender Foundation\Blender 5.2\blender.exe`
  （只用于执行生成的脚本，不走 pip）
- 依赖：`pip install -r requirements.txt`

`.env` 里配置网关：

| 变量 | 说明 |
| --- | --- |
| `CNXMAI_API_KEY` | 密钥 |
| `CNXMAI_BASE_URL` | `https://api.qnaigc.com/v1` |
| `CNXMAI_CHAT_URL` | `https://api.qnaigc.com/v1/chat/completions` |
| `CNXMAI_MODEL` | `openai/gpt-6-astra`（支持图片输入） |
| `CNXMAI_MAX_TOKENS` | **上限 128000**，写更大在请求入口就被 400 拒掉 |
| `CNXMAI_TIMEOUT` | `40m` 这类时长写法 |
| `WEBAPP_HOST` / `WEBAPP_PORT` | 可选，默认 `0.0.0.0:8000`（监听全部网卡） |

## 用法一：Web 界面（推荐）

```
E:\GPTAPI3\.venv\Scripts\python.exe webapp\server.py
```

打开 <http://127.0.0.1:8000>，把图片 / `.md` / `.xlsx` 拖进虚线框，点「开始生成」。

版面是左右两栏：**左边投料与进度**，**右边历史记录**（桌面宽度下它一直粘在右侧，自己内部滚动；
窗口窄于 900px 时自动堆成一栏，历史回到页面末尾）。

- 三种投喂方式都行：**拖拽**、**点击选择**、**`Ctrl`+`V` 粘贴**
  - 粘贴：截图（`Win`+`Shift`+`S`）、网页上复制的图、画图工具里的图，都能直接粘进来
  - 剪贴板里只有文字时（比如从 Excel 里选中一片单元格复制），会存成 `粘贴文本-N.md` 当作规格文本
  - 粘贴来的图片若没有名字，自动命名成 `粘贴图片-N.png`
  - 在资源管理器里 `Ctrl`+`C` 复制的**文件**粘贴不了 —— 浏览器不允许读取本地文件路径，那种情况请用拖拽
- 进度、日志、结果都在页面上；完成后直接下载 `.blend` 和 `.xlsx`
  （每块成品磁贴上有一颗蓝色的「下载」按钮；整块磁贴本来也可以点，点哪儿都是下载）
  底部有一颗蓝色的「再次上传」，点完清空上一单、回到投料状态
- 结果卡的第一行写着这一单的**开始时间与结束时间**（`开始 09-22 18:27:44 · 结束 18:35:45`），
  后面跟耗时和文件数；跨天跑的任务会把结束那天的日期也写出来。
  旧任务（`job.json` 里没有 `started` 的那几条）只能拿提交时刻当开始时间，
  排队久了会差几分钟；新跑的任务是准的
- 每个任务在 `webapp/workspaces/<任务号>/` 下有独立工作区（输入、生成的脚本、产物、`job.json`）
- **同时最多 2 个任务在跑**，再提交的自动排队、先到先做。同一个页面可以连着排好几个任务，
  历史列表里点排队中 / 运行中的那一条就能切过去看它的实时进度
- 关掉服务再打开，历史记录仍在（未跑完的标记为「已中断」）
- 数据只留在这台机器上；除调用 `.env` 里配置的模型网关外不外发别处

### 让局域网里的同事也能用

服务默认绑 `0.0.0.0`，启动时控制台会直接打印可分享的地址：

```
  本机访问:   http://127.0.0.1:8000
  局域网访问: http://192.168.2.30:8000
```

把「局域网访问」那一行发给同事即可（同一个 Wi-Fi / 同一个交换机下）。只想自己用就加 `--local`，或设 `WEBAPP_HOST=127.0.0.1`。

几个实际会碰到的前提：

- **并发上限 2**：同时最多 2 个任务在跑，多出来的一律排队，各自的工作目录、脚本、产物互不干扰。
  想调就设 `WEBAPP_PARALLEL`（1–8）：
  ```powershell
  $env:WEBAPP_PARALLEL="3"; & $py webapp\server.py
  ```
  再往上加意义不大 —— 任务的绝大部分时间都花在等模型回包上，只有 Blender 生成那一段吃 CPU，
  同时跑太多会互相抢核，反而每个都变慢。
- **没有登录**：内网里任何人打开地址，都能看到 `webapp/workspaces/` 下**全部历史任务**并下载其中所有报价表 —— 只存可信内网用。
- **网络类别必须是「专用」**：`Get-NetConnectionProfile` 看一眼 `NetworkCategory`。公共网络默认拦入站：
  ```powershell
  Set-NetConnectionProfile -InterfaceAlias "以太网" -NetworkCategory Private
  ```
- **防火墙**：专用网络下 `python.exe` 已有入站放行规则，本机实测正常。同事打不开就补一条（管理员执行一次）：
  ```
  netsh advfirewall firewall add rule name="规格转模型 8000" dir=in protocol=TCP localport=8000 action=allow
  ```
- 只开 `8000` 这一个 TCP 端口，不需要放通其它端口。

### 控制台在说什么

浏览器上的轮询（每 15 秒一次的 `/api/jobs`、静态文件、SSE 长连接）全部**不打印**，
只保留真正有用的任务级信息，`4xx / 5xx` 一律照常显示。

每行都带**任务短号**（任务号后 6 位），并发跑的时候几个任务的阶段行会交错，靠它才分得清哪行是谁的：

```
[18:38:46] [7e961d] ▶ 新任务　来自 192.168.2.21
[18:38:46] [7e961d]   3 个文件 · 40.0 KB
[18:38:46] [7e961d]     · image.png　参考图片　15.1 KB
[18:38:46] [7e961d]     · money.md　规格文档　2.5 KB
[18:38:46] [7e961d]     · price.xlsx　材料价目表　22.3 KB
[18:38:46] [7e961d]   队列: 有空位，马上开始
[18:38:46] [7e961d] ▶ 开始执行　（并发 1/2）
[18:38:47] [7e961d] · 解析上传材料　（已用时 1 秒）
[18:38:47] [7e961d] · 模型生成脚本中　（已用时 1 秒）
[18:39:02] [3b7a10] ▶ 新任务　来自 192.168.2.21
[18:39:02] [3b7a10]   4 个文件 · 4.7 MB
[18:39:02] [3b7a10]     · 粘贴图片-1.png　参考图片　1.5 MB
[18:39:02] [3b7a10]     · 粘贴图片-2.png　参考图片　1.8 MB
[18:39:02] [3b7a10]     [跳过] notes.docx（不支持的格式 .docx）
[18:39:02] [3b7a10]   队列: 有空位，马上开始
[18:39:02] [3b7a10] ▶ 开始执行　（并发 2/2）
[18:41:15] [9c1e58] ▶ 新任务　来自 192.168.2.44
[18:41:15] [9c1e58]   3 个文件 · 40.0 KB
[18:41:15] [9c1e58]   队列: 2 个在跑，前面还有 0 个在等（上限 2）
[18:41:52] [4d2b77] ▶ 新任务　来自 192.168.2.44
[18:41:52] [4d2b77]   3 个文件 · 41.0 KB
[18:41:52] [4d2b77]   队列: 2 个在跑，前面还有 1 个在等（上限 2）
[18:42:05] [7e961d] · 模型脚本已就绪　（已用时 3 分 19 秒）
[18:42:05] [7e961d] · 生成 Excel 报价表　（已用时 3 分 19 秒）
[18:43:11] [3b7a10] · 模型脚本已就绪　（已用时 4 分 9 秒）
[18:43:14] [7e961d] · 已完成　（已用时 4 分 28 秒）
[18:43:14] [7e961d] ✔ 完成　历时 4 分 28 秒
[18:43:14] [7e961d]   产物: final_model.blend 315.6 KB　·　final_quote.xlsx 25.3 KB
[18:43:14] [7e961d]   目录: webapp\workspaces\20260922-183846-7e961d\output\2026-09-22_184314
[18:43:15] [9c1e58] ▶ 开始执行　（并发 2/2）
[18:45:02] [7e961d] ↓ 192.168.2.21 下载 final_model.blend　（308.2 KB）
```

失败时打的是 `✘ 失败　历时 …` 加一行 `原因: …`。
要改这个行为，看 `webapp/server.py` 里的 `note()` 和 `QuietAccessLog`。

## 用法二：命令行

```powershell
$env:PYTHONIOENCODING="utf-8"
$py = "C:\Users\QRT\AppData\Local\Programs\Python\Python312\python.exe"

& $py pipeline\ask_model.py                       # 扫 input/ → 生成两个脚本
& $py pipeline\build.py                           # 执行脚本 → output/<时间戳>/
```

`input/` 里**所有**文件都会被当作输入材料：图片走多模态真发给模型，
`.xlsx` 先用 openpyxl 转成 Markdown 表格（模型看不到二进制），文本原样并入。

常用参数：

```powershell
& $py pipeline\ask_model.py --dry-run             # 只打印投料清单和提示词
& $py pipeline\ask_model.py -t 40m -a 2           # 超时 / 重试次数
& $py pipeline\build.py --run-name trial1         # 固定输出目录名
& $py pipeline\build.py --strict                  # 附属文件多一个就报错
```

## 交付物校验

```powershell
& $py webapp\tools\test_sse.py                    # SSE 回归测试（应输出 PASS）
& $py webapp\tools\verify_out.py                  # 校验 xlsx/blend 的合法性
& $py webapp\tools\watch_job.py                   # 盯着跑，服务器在跑的时候用
```

`webapp\tools\inspect_blend.py` 需要用 Blender 执行，会打印场景里的真实事实
（对象数、材质、单位、包围盒等）：

```powershell
$env:BLEND_IN = "E:\GPTAPI3\webapp\workspaces\<任务号>\output\<时间戳>\final_model.blend"
Start-Process "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe" -Wait -NoNewWindow `
  -ArgumentList '--background','--factory-startup','--python-exit-code','1','--python','webapp\tools\inspect_blend.py'
```

## 已知边界

- 提示词里若要求「把文件作为附件上传」，本网关做不到 —— 文件只能由本机执行脚本产生
- 生成的脚本一律**原样执行**，`build.py` 不做任何文本改写。跨版本的 Blender API 写法
  （引擎枚举 `BLENDER_EEVEE_NEXT` / `BLENDER_EEVEE`、`scene.eevee.*` 已移除的属性）
  由投料口提示词约束，见 `input/money.md` 与 `ask_model.py` 里的技术契约；
  万一模型还是写死某个版本的名字，Blender 会直接报错、任务失败并打印 Traceback，
  错误行号一眼可见，不会静默出问题
- Blender 5.2 保存的 `.blend` 默认是 zstd 压缩，文件头是 `28 b5 2f fd` 而不是 `BLENDER`；
  只有 Blender 自己打开过才算权威验证
- 输入里缺少项目规格时，流水线照样跑通，但模型会如实标注「未提供」并在模型里留白，
  不会虚构尺寸 —— 要出真实数据必须补规格
