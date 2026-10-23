# game-translator

通用游戏运行时汉化框架。核心不含任何游戏名、路径或选择器；每款游戏只是 `profiles/` 下的一份数据。

当前实现：Electron/CDP、RPG Maker MV/MZ、Ren'Py、ERA (EraElectron) 四条运行时适配线可用，老 ERA (Emuera) 走离线改写 ERB 与 CSV。Unity、Unreal、OCR 只留了接口。

## 快速开始

```bat
gt.cmd game add "D:\Some Game\Game.exe"   :: 认引擎、挑端口、建 profile
gt.cmd game list                          :: 列表：引擎、词库条数、上次运行
gt.cmd game remove some-game              :: 删掉 profile 与词库

gt.cmd export --profile mygame             :: 未命中的句子导成 Word
gt.cmd import --profile mygame back.docx   :: 回传的 Word 并进词库

gt.cmd doctor --profile mygame            :: 检查可执行文件、记忆库、后端
gt.cmd tm build --profile mygame          :: 从 JSONL 建 SQLite 索引
gt.cmd run --profile mygame               :: 启动游戏并翻译
gt.cmd run --profile mygame --attach      :: 游戏已在跑，附加进去

gt.cmd pack export --profile mygame      :: 把一款游戏的配置、术语表、词库打成 .gtpack
gt.cmd pack import mygame.gtpack         :: 还原或并入另一台机器
gt.cmd update check                     :: 查有没有新版本
gt.cmd update                         :: 下载、校验、退出后自动替换
gt.cmd gui --start mygame               :: 开窗口并直接开始翻译这款游戏
```

图形界面（`GameTranslator.exe`，或 `python launch.py`）是同一套东西的外壳：列表、添加、修改、删除，以及开始与停止。

进游戏后按 `Ctrl+Shift+T` 在译文与原文之间切换。退出时自动还原原文。

## 添加游戏与引擎识别

游戏不会自报引擎，所以识别靠 exe 旁边有哪些文件，规则在 `gt/games.py` 的 `detect_engine()`：

| 引擎 | 判据 | 能跑吗 |
|---|---|---|
| RPG Maker MZ | `js/rmmz_core.js`，或 `data/System.json` 加 `nw.dll` | 能 |
| RPG Maker MV | `www/js/rpg_core.js` 或 `www/data/System.json` | 能 |
| RPG Maker VX Ace / VX / XP | `*.rgss3a`、`*.rgss2a`、`*.rgssad`、`RGSS*.dll` | 不能，RGSS 是 Ruby |
| ERA (EraElectron) | `resources/app.asar`，加 `game/csv` 与 `game/ere` 两个数据目录 | 能，页面里的 span 就是正文 |
| Electron | `resources/app.asar` 或 `resources/app/` | 不能，选择器要按游戏配 |
| NW.js | `nw.dll`、`node.dll` 或 `package.nw` | 不能，同上 |
| Ren'Py | `renpy/`、`lib/py3-*`，或 `game/*.rpyc` | 能，走 `RENPY_SEARCHPATH` 外部注入 |
| Unity | `UnityPlayer.dll`、`GameAssembly.dll` 或 `<程序名>_Data/` | 不能，缺 BepInEx 插件 |
| Unreal | `Binaries/Win64` 下的程序，且旁边有 `Content/Paks/` | 不能，缺 UE4SS 脚本 |

识别出来的引擎会写进 `profile.json` 的 `engine` 字段。只有能跑的引擎才会建 profile；其余的在界面上说明原因后拒绝，不会建一个跑不起来的空壳。

新 profile 的调试试端口从 9222 起自动挑：跳过别的 profile 占着的，也跳过当前有人监听的。名字重复时 id 加 `-2`、`-3`。`state.json` 记上次运行时间，列表里显示。

## RPG Maker MV / MZ

MV 与 MZ 把文字画进 canvas，DOM 里没有可替换的节点，所以适配器换了个位置下手：把引擎自己的三个出字口拦下来，译文交给引擎自己画。

| 钩子 | 管什么 |
|---|---|
| `Window_Base.drawTextEx` | 选项、名牌、帮助文字、物品说明 |
| `Window_Base.drawText` | 菜单项与普通标签 |
| `Game_Message.add` | 对话正文（它是逐字画的，不走 `drawTextEx`） |

好处是排版、换行、字号全由引擎负责，`\C[3]`、`\N[1]` 这类控制码照旧生效。代价是这些字符串只在**重画**时才会经过钩子，所以适配器做了两件事：

- 附加时强制重画当前场景一次（`__GT.rescan()`），否则静态界面一条都不上报。
- 译文到达后重画窗口层。没有 `refresh` 的窗口（`Window_Help` 一类）用 `setText(原字符串)` 触发重画。

窗口挂在 `scene._windowLayer` 上，不在 `scene.children` 里，遍历错了会以为没有可刷新的窗口。

对话窗口不能用 refresh 换字：进度存在 `_textState` 里，只能重开这条消息（`_textState = null` 再 `startMessage()`）。所以后端新译的台词会从头重打一遍，缓存与记忆命中的台词一次到位。消息正在关闭时不能重开，否则留下一个空白框。

`_textState` 只在「正在打字」时非空。一页画完后（尤其是 `\>` 快进的行）它是 null，窗口却还停在屏幕上等按键。这时候到达的译文同样要应用，否则慢后端追不上已经在等的台词。例外是页面上压着一个选项窗口：在它下面重打整页会让光标和分支对不上，这种情况留到下一页。

切回原文（`setMode("original")`）时先还原文本行，再靠同一条重开路径重画；重画时不能再走一遍改写，否则译文马上盖回来，切换看起来像没反应。三条都在 `tests/test_rpgmaker_runtime.py` 里用 Node 跑真脚本锁着。

`adapterConfig` 里的字段：

- `skipPatterns`：正则数组，命中的串不上报。存档列表里的时间戳、金额每帧都变，不挡掉会一条一条喂给模型。
- `restartMessage`：设 `false` 可关掉对话重开，只对之后新出现的台词生效。
- `stripPattern`：查表时用来剥掉样式码的正则，默认值在适配器里（`RPGMakerAdapter.DEFAULT_STRIP_PATTERN`）。见下面「离线批量翻译」。

### Ren'Py

Ren'Py 不开调试端口，没有 CDP 那样的附加通道。它能用的外部口子是 `RENPY_SEARCHPATH`：这个环境变量把额外目录追加进引擎的加载搜索路径，目录里的 `.rpy` 会被编译并执行，和游戏自带脚本没有区别，而游戏目录一个字节都不用动。

流程：

1. 适配器在 `profiles/<id>/renpy_mod/` 写一份 `zz_gt_inject.rpy`，并从系统字体里拷一个中文字体过去（`Deng.ttf` 优先）。
2. 起一条本地 HTTP 桥（127.0.0.1，端口取 profile 的 `port`），翻译请求由运行时管线回答：缓存 → 词库 → 后端。
3. 设置 `RENPY_SEARCHPATH`、`GT_RENPY_PORT`、`GT_RENPY_FONT` 后启动游戏。
4. 注入脚本把 `renpy.translation.translate_string` 换成本进程的查询函数，并把 `store._` 指向它。台词、选项、主菜单与存档界面都经这一个口子，绘制前逐条取译文；任何异常都回落到原文，后端死了游戏照常能玩。

代价与边界：

- 覆盖对话、选项与引擎自带界面。实测主菜单是「开始／读取／设置／关于／帮助／退出」，快捷菜单里「保存」也是中文。
- 没命中时要等一会儿才画：单条最多等 `GT_RENPY_FIRST_WAIT`（默认 1.2 秒），一串最多花 `GT_RENPY_WAIT_BUDGET`（默认 2 秒），超出就按原文画，译文到了再重画。词库与缓存命中是立即的。
- 重画只重跑当前画面，已经画在屏幕上的那句台词不会跟着换语言；切回原文要等下一句。
- 字体是从外部目录加载的，`config.font_replacement_map` 与 `style.default.font` 同时改，避免游戏自带字体缺中文字形。
- 热键 `Ctrl+Shift+T` 双向切换，注册在 `config.underlay` 的 Keymap 上。

**驱动被测游戏不用本机鼠标键盘。** 桥的 `/poll` 回包里带命令，注入脚本在 Ren'Py 自己的事件队列里执行：`start` 走 `jump_out_of_context("start")`（和主菜单的开始按钮同一条路），`event dismiss` 推进一句，`event ctrl_shift_K_t` 触发热键。截图用引擎内的 `renpy.screenshot`，不截桌面，所以窗口在后台也能测。用真实点击行不通：Windows 不批前台切换，点击会落在最上层窗口上，而后台窗口本来也点不到。

实测（Ren'Py 8.6.0 SDK 的 `the_question`，2026-10-22）：主菜单全中文；四句台词依次是中文；按热键切原文后下一句是英文，再按一次回到中文；结束后没有残留进程。游戏目录无改动。

### ERA (EraElectron)

新的 ERA 系游戏（例如 UmaERA 那一支）跑在 [EraElectron](https://gitgud.io/umaera/engine/era-electron) 上：Electron 外壳，界面由 Vue 3 与 Element Plus 写成，GPLv2 开源。游戏自己的数据在 `game/` 下，`csv` 与 `ere` 是文本，`ere.config.json` 是排版与窗口配置。

正文不是 canvas，是页面里的 `<span>`：每一行由 `print-block` 组件排出，滚动容器 `.el-scrollbar__view` 里装着全部对白、选项按钮与状态条。所以这一类不需要单独的适配器，挂 CDP 加一条选择器就能用。判据是 `resources/app.asar` 加 `game/csv` 与 `game/ere` 两个目录。旧包还带 `game/ere.config.json`，新包（3.131 起）不再附，引擎首次运行时自己生成，所以判据不依赖它。

添加游戏时 `detect_engine()` 认出 `era_electron`，profile 里自动写好：

- `selectors`：`.el-scrollbar__view`
- `skipSelectors`：`.el-input`、`.el-textarea`、`.el-scrollbar__bar`、`.el-loading-mask`、`.el-message`、`.el-notification`

实测两版：

- EraUma v2.21（EraElectron 4.7.1，2026-10-23）：标题页两行文字进未命中表；手工写进词库后重跑，页面上的 span 换成词条，`seen` 与 `applied` 都是 2。
- EraUma v3.131（EraElectron 5.0.0，同日本地按官方 CI 构建）：版权声明那一屏 39 个文本节点全部采到，38 条进未命中表。

边界：

- 只覆盖 EraElectron。erArk 是 Python 写的另一套（默认 Tk 绘制，开 `web_draw` 才走 Flask 加浏览器），还没适配。老的 Emuera（GDI 画字，脚本是 ERB 加 CSV）不走这条线，见下一节。
- 模态框能不能翻取决于它内部的容器：版权声明用了 `el-scrollbar__view`，因此被覆盖；设置面板不在这个容器里，不翻。
- 每追加一行，Vue 会重建滚动区里的节点。运行时把节点被改写当成新文本重新采集，所以句子上屏的瞬间可能先看到原文，词库命中后立刻换成译文。

### ERA (Emuera) 老引擎

老 ERA 游戏跑在 Emuera 上，它是 C# 写的 WinForms 程序，用 GDI 画字，没有 DOM 也没有调试端口。能翻的地方在脚本：玩家看到的每一句都是 ERB 文件里的字面量或者 CSV 的一列。所以这条线走文件，不走窗口。

```bash
gt.cmd era extract   --game D:\ERA\eraAL-K --out data/eraAL-K.jsonl
gt.cmd era translate --game D:\ERA\eraAL-K --out data/eraAL-K-tm.jsonl --batch 8
gt.cmd era apply     --game D:\ERA\eraAL-K --tm data/eraAL-K-tm.jsonl
```

- `extract` 认 `PRINTFORM[L/W]`、`PRINT[L/W]`、`PRINTBUTTON`、`PRINTDATA` 块，也扫 CSV。ERB 里整行成一段，不按 `%表达式%` 切开：切开之后「の」会单独成为一句，翻出来拼不回去。
- CSV 只翻显示列，键列不动。定义表取第 2 列；Chara 表只翻 `名前`、`呼び名`，以及 `CSTR` 行里 `名字`、`名前`、`普通ネーム`、`識別名`、`ＳＬＧ解説_0` 到 `_9` 这几项；`GameBase.csv` 只翻 `ウィンドウタイトル`、`タイトル`、`作者`、`追加情報`。`種族`、`所属`、`初期タグ`、`CLOTH:*` 是脚本读回去的数据，留原文。
- 占位符（`%...%`、`{名字}`、`\n` 这类转义、`[[...]]`）在送模型之前换成 `⟦0⟧` 形式的标记，回来后还原。校验比的是标记的多重集：同一种标记出现几次要对上，先后的顺序变了不算错，除非标记本身是 `RAND`、`RANDOM`、`RESULT`、`GETCHARA`、`GETSPCHARA` 这几个（它们读的是参数位置）。不过的丢弃该条，重试一次单条；仍不对的不入库。私用区字符做标记不行，实测本地模型会把它们吃掉。
- `translate` 逐条追加写盘，中断重跑会跳过已有译文。后端取 `backends.json` 的 active，可以用 `--backend` 指定。
- `apply` 只改有译文的片段，其余字节不动，保留原编码、BOM 与换行。原文件是 cp932 而译文是中文时，该文件会被转成 UTF-8 带 BOM，计数写在返回的 `converted` 里。行数逐文件不变。
- 已经汉化的句子不会被重复翻：有假名的算日文；没有假名的汉字行看文件整体的假名比例；含简体专有字的直接跳过。假名判定不把 `・`（U+30FB）和 `ー`（U+30FC）算进去，否则一个 `・` 就能让整行中文被判成日文，重跑提取会多出上万条假阳性。

实测（eraAL-K，2026-10-23）：5,374 个 ERB 加 CSV 提取出 42,201 条唯一文本。DeepSeek 全量跑完（06:13 到 08:16）译出 41,414 条，占 98.1%，787 条因占位符校验不过被丢弃；`apply` 改写 1,257 个 ERB、52,103 处，改完仍有 1,536 处日文。第二轮补译 980 条并合并词库后，剩余未翻的唯一日文串降到 23 条，这一轮 `apply` 动了 469 个文件、1,873 处，没有文件需要转编码。

边界：

- 模板串对不上：`ファイル %1` 这种，游戏运行时把 `%1` 换成数字，画出来的是「ファイル 1」，词库里查不到。
- `HTML_PRINT` 不翻，它是排版标签。
- 改的是游戏文件本身，不是运行时替换。仓库是 git 克隆的话，`git checkout -- .` 就是回滚。

## 离线批量翻译

边玩边译不如先离线翻完再查表。流程是：提取 → 交给文档翻译服务 → 回填 → 建库。

```bash
python scripts/extract_mz.py <游戏>/data -o data/corpus.jsonl   # 提取
python scripts/import_docx.py data/mygame_to_translate.jsonl back.docx -o data/mygame_translated.jsonl
gt tm build --profile mygame --source data/mygame_translated.jsonl
```

`import_docx.py` 按段落序号把回传的译文对回映射表，还原 ` ↵ ` 换行、按 `〔n〕` 填回 `\V[3]` 这类运行时引用；对不上的行不猜，写进同名 `.report.json`。

**查表要查两次。** 样式码（`\C[17]`、`\PX[30]`）发出去之前就剥掉了，因为翻译服务不认，而错位的颜色码比没有更难读。于是记忆库里的键是**不含样式码**的正文，引擎画出来的却是带码的原串。`Pipeline` 因此先按原串查，查不到再把样式码剥掉查一次；校验用的也是剥过的串，否则占位符守恒那一关会把 94% 的命中判死。动态引用 `\V[3]`、`\N[1]` 不剥，它们带的是运行时的数值，记忆条目必须和它们对上。

启动参数是 `--remote-debugging-port=<port>`，NW.js 与 Electron 都认。附加进去时要留意页面可能挂着扩展的 URL，那种情况下 `url` 是 `chrome-extension://…` 而不是游戏本身，按 `urlMatch` 匹配即可。

## 翻译策略

每款游戏在「修改…」里单独选，存在 profile 的 `strategy` 字段：

| 策略 | 查表顺序 |
|---|---|
| 只查词库 | 缓存 → 词库，没命中的留原文，不建后端链 |
| 词库优先，后端兜底 | 缓存 → 词库 → 后端（默认） |
| 全走后端 | 跳过词库直接翻；缓存照用，翻过的不再花第二次钱 |

旁边的「后端」决定用哪条链：跟随默认（`backends.json` 的 `active`），或钉死某一个。profile 里的 `backends` 写成数组就钉死，缺这个键才跟随默认。没有 `strategy` 的旧 profile 按 `backends` 推断：空数组算「只查词库」，其余算「词库优先」。

## 补译文：导出与导入

运行时没翻出来的句子，每次停止时写进 `profiles/<id>/unmatched.jsonl`。界面「导出未命中…」把它变成三份文件，放在 `profiles/<id>/export/`：

- `unmatched_<时间>.docx`：一段一句，句内换行写成 ` ↵ `
- 同名 `.jsonl`：映射表，导入时配对用，不要交给服务
- 同名 `.txt`：纯文本备份

把 Word 交给文档翻译服务，翻完用「导入回传译文…」选回来。段落数变了先试整体位移（服务在头尾加标题），再拿带数字、字母或 `〔n〕` 的段落当路标重新对齐；对上的行还原换行与引用，缺占位符、引用越界、换行数不符的不进库，逐条写进 `export/unmatched_translated.report.json`。

进库是 upsert：新条目加入，同一原文重译时更新成新译文，别的条目不动。命令行同一套：`gt export --profile mygame`、`gt import --profile mygame 回传.docx`。

## 模型后端与设置

端点配置在 `backends.json`，与 `profiles/` 同级；没有这个文件时用内置默认值，界面里「模型设置…」写的就是它。内置两条：

| 名字 | 默认地址 | 说明 |
|---|---|---|
| `local_llm` | `http://127.0.0.1:1234/v1` | LM Studio，密钥留空，模型要自己选 |
| `deepseek` | `https://api.deepseek.com/v1` | 密钥自己填，模型默认 `deepseek-chat` |

每条一户的字段：`type`（`openai_compatible`、`youdao_doc`、`youdao_ui`）、`base`、`apiKey`、`model`、`temperature`、`maxTokens`、`timeout`、`concurrency`。`active` 是没单独指定后端的游戏默认走哪个；profile 里写了 `backends` 就以 profile 为准，空数组表示只查词库、不调模型。

`concurrency` 决定 pipeline 起几个工作线程（取整条链上的最大值）。LM Studio 单模型一次只答一个请求，保持 1；DeepSeek 这类接口可以设 2 到 4。工作线程共享一个队列，同一串不会重复送出。

密钥是明文存在 `backends.json` 里的，这个文件别外传。有道两条后端目前仍是占位，选了也不会工作。

### 本地模型（LM Studio）

`local_llm` 直接打 LM Studio 的 OpenAI 兼容接口（`http://127.0.0.1:1234/v1`），不经过 `llm.cmd`。开跑前模型得先加载好，LM Studio 不会自动 JIT 加载：

```bat
lms server start
lms load qwen3.8-4b-distill-heretic-abliterated --gpu max --ttl 600
```

profile 里 `model` 填的是 LM Studio 的模型标识符（`lms ls` 第一列）。写错了不会报错：单模型驻留时 LM Studio 一律回落到已加载的那个，所以标识符只在同一进程里换了模型时才起作用。临时换模型可以设环境变量 `LOCAL_LLM_MODEL`，不必改 profile。

`maxTokens` 必须留足。这两个模型关不掉思考模式（`enable_thinking=false` 无效），思考占满额度时正文为空、JSON 尾部被截断，表现是「后端跑了但一条都没翻出来」。实测同一批 6 条：

| 模型 | maxTokens=2048 | maxTokens=8192 |
|---|---|---|
| 4b（`qwen3.8-4b-distill-heretic-abliterated`，全 GPU） | 1/6，7.2s | **6/6，6.7s** |
| writer（`qwen3.8-9b-distill-slerp-f451-pro-writer-uncensored`，`--gpu 0.8`） | 0/6，107.5s | 6/6，136.0s |

4b 是默认值：一批 6 条约 7 秒，够跟手。writer 译文语气更顺，但一批两分多钟，只适合离线预翻，不适合边玩边翻。返回条数少于送出的条数时后端会打一条 warning。

### 术语表

`profiles/<id>/glossary.tsv`，TSV：`原文<TAB>译文[<TAB>注释]`，`#` 开头是注释。界面里「模型设置… → 术语表」能增删改，只动对应那一行，注释和注释列都保留。

后端翻译时，把这一批文本里真正出现的术语写进 system prompt（最多 40 条），术语表再大也不会白占上下文。改完要重启一次翻译才会生效。

## 窗口与系统集成

界面关掉不是退出：窗口缩到托盘，右键图标可以显示主窗口、开始/停止、开关悬浮窗、打开运行记录、退出。托盘用 ctypes 直接调 `Shell_NotifyIcon`，全局热键用 `RegisterHotKey`，两者共用一个隐藏窗口和消息循环（`gt/tray.py`），不引入 pystray/Pillow 这类依赖。托盘起不来（比如没有桌面会话）时，关窗口就是退出，原有行为不变。

- **热键**：开始翻译时按 profile 的 `hotkey`（默认 Ctrl+Shift+T）注册全局热键，由 `RegisterHotKey` 吞掉按键再调 `runtime.toggle_mode()`；注册失败时退回页面内的 keydown 监听，游戏窗口有焦点时照常可用。热键必须在拥有窗口的线程上注册，所以请求由托盘线程执行。
- **悬浮窗**：置顶无边框小窗，显示命中率（词库 + 缓存 ÷ 已看）、已看/已替换条数、三类命中数，可拖动、可关闭；位置存在 `ui.json`。
- **运行记录**：主窗口「运行记录…」，两个分页。一页是 `logs/gt.log` 的尾部，可按级别过滤；另一页是当前游戏 `unmatched.jsonl` 的表格，双击复制原文。
- **首次启动**：列表为空时弹一页三步引导（添加游戏 → 模型设置 → 开始翻译），模型是否填好以 `backends.json` 的 `active` 判断，不探测服务在不在跑。

## 分发：游戏包、安装包、自动更新

**游戏包**（`.gtpack`）是一款游戏的可搬走部分：`profile.json`、`glossary.tsv`、`tm.sqlite`，加一份 `manifest.json` 记格式版本与每件的 sha256。词库是拿 SQLite 的备份接口取的快照，翻译正在跑时导出也不会截到半条事务。

- 导入时包里没有的本地不动；本地已有的同 id 游戏默认保留本地配置与术语表，只并词库（命令行加 `--replace-profile` 才覆盖）。
- 词条按规范化后的原文 upsert，新条目加入，同原文不同译文的更新成包里那条。

**安装包**（`GameTranslator_Setup.exe`）是 PyInstaller 打的单个 exe，内嵌 `payload.zip`。装到 `%LOCALAPPDATA%\Programs\GameTranslator`，只写 HKCU 和用户目录，不弹 UAC；建桌面与开始菜单快捷方式，写卸载项，并生成同目录的 `uninstall.cmd`。卸载脚本是 ASCII 批处理加一份 UTF-8 BOM 的 PowerShell，批处理只负责转交，中文快捷方式名在 PowerShell 里处理。

**自动更新**靠一份静态 `update.json`：

```json
{"version": "0.2.0", "url": "https://…/GameTranslator.exe", "sha256": "…", "size": 20900000, "notes": "…"}
```

`gt update` 取它、比版本、下载、校验 sha256 与字节数，然后把新 exe 写成 `.new.exe`，交给一个脱离的批处理等旧进程退出后替换并重启。地址存在 `update.json`（与 `profiles/` 同级）的 `manifestUrl`，界面「工具 → 更新设置…」写得进去；`autoCheck` 打开时每次启动查一次，24 小时内不重复。`tools/build_release.py` 打完整包时会顺带按当前 exe 生成一份 `dist/update.json` 模板，发布说明用 `--notes` 传进去，安装包内嵌的那份也是同一份。

## 结构

```
gt/
  config.py       profile 读取与路径解析
  games.py        引擎识别、建档、改名改路径删除、列表元数据
  settings.py     backends.json：端点、密钥、并发，以及拉模型与测连通
  tray.py         托盘图标与全局热键（ctypes，隐藏窗口 + 消息循环）
  overlay.py      置顶命中率小窗
  logview.py      运行日志与未命中列表的只读窗口
  wizard.py       首次启动的三步引导
  uistate.py      ui.json：悬浮窗位置、托盘提示是否看过
  text.py         归一化（记忆库键）
  placeholders.py 占位符与标记保护
  tm.py           翻译记忆（SQLite 索引 + 查询）
  cache.py        运行时产物缓存（跨进程）
  glossary.py     术语表
  pipeline.py     查表顺序：缓存 → 记忆 → 后端链
  runtime.py      适配器与管线的接线、主循环
  cli.py          命令行
  pack.py         游戏包：导出快照、校验、并入词库
  update.py       自动更新：取 manifest、校验、替换后重启
  adapters/       宿主接入：electron_cdp 与 rpgmaker_mv_mz 已实现，其余为占位
  backends/       翻译来源：local_llm 已实现，有道两路为占位
  js/runtime.js   注入页面内的通用运行时，无游戏专属逻辑
installer/        安装程序（PyInstaller 入口 + 卸载脚本模板）
tools/build_release.py  一次打出 exe、安装包、便携包与 update.json
profiles/<id>/    单款游戏的全部数据：profile.json、glossary.tsv、tm.sqlite
data/             翻译记忆的 JSONL 原文（由 scripts/ 下的抓取脚本生成）
tests/            纯核心测试，不需要游戏与网络
```

## 数据流

```
页面 MutationObserver
      │  __gt_report(json)          CDP binding
      ▼
electron_cdp 适配器 ──► 运行时 ──► 管线.register()
      ▲                                  │
      │                                  ├─ 缓存命中 → 立即 apply
      │                                  ├─ 记忆命中 → 立即 apply
      │                                  └─ 未命中 → 队列 → 后端线程
      └────────── apply([{id,text}]) ◄──────┘（译文到达后）
```

页面侧为每个文本节点保存原文，所以还原不依赖 Python 进程是否还活着。

## 关键约束

- **不改游戏文件**。运行时替换 DOM 文本，退出还原。
- **占位符守恒**。`<b>`、`[name]`、`%s`、`[rand|a|b]` 的 `|` 数量、换行数都要对得上，校验不过就丢弃译文、保留原文。
  - 括号里是纯标识符（`[name]`、`[pc.name]`）时按名严格比对。
  - 括号里带 `|` 的宏（`[tps|s]`）只比对结构：社区译文本就会把它改写成 `[复数|]`。
- **后端可插拔**。记忆库覆盖不到时才走后端；后端不可用时静默跳过，界面照常显示原文。

## 已知边界

- 翻译记忆的来源是社区补丁的字符串表，里面大量条目含游戏宏（`[rand|...]`、`[pc.hasCock|...]`）。这些字符串在 DOM 里已经被展开，**按原文比对不可能命中**，只能交给后端。按条目统计：47,342 条中 26,570 条（56.1%）不含宏也不含标签，可被 DOM 直接命中。
- 一条正文通常是一个完整文本节点，能整段命中；标题页那种被行内 `<a>`/`<b>` 切碎的句子会拆成碎片，命中率低。这是碎片化本身的代价，不是匹配算法的缺陷。
- 游戏重启后 CDP 页目标会变；适配器会重连并重新注入，已翻过的节点会重新走一遍缓存/记忆。

## 待办

见 `C:\Users\Administrator\.claude\TODO.md`。
