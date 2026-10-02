# 人设编辑器（独立 exe，不是插件）

给 DSH 的 agent preset 换人设提示词的小工具。**完全跑在 DSH 外面**：不装插件、不往页面注入任何东西、不占端口，只是读写 DSH 自己的 `cordis.patch.yml`——DSH 自己会在 1～2 秒内热加载（已存在的会话保留原人设，**新开一个对话**才用上新的）。

**换台机器直接能用**：不需要改任何配置，程序自己去找 DSH 的 yml。

## 两个 exe

| 文件 | 用途 |
|---|---|
| `dist\人设编辑.exe` | 双击即用：图形界面 |
| `dist\人设编辑-cli.exe` | 带控制台的版本，供脚本/批处理调用（输出也会同时写进 `人设编辑.log`） |

两个都是免安装单文件（约 10 MB），换台机器直接拷过去也能用。人设文件默认放在 **exe 同目录的 `personas\`**（整个文件夹拷走，人设跟着走）。

## 它怎么自己找到 DSH 的 yml

按顺序找，找到就用：

1. 环境变量 `DSH_PROFILE_DIR`（当前 profile，优先级最高）；
2. 环境变量 `DSH_HOME` 下的 `profiles\<名字>\cordis.patch.yml`；
3. `%USERPROFILE%\.dsh\profiles\...`（DSH 默认家目录）；
4. exe 自己所在的目录（把 exe 丢进 DSH 家目录或它旁边也能认出来）；
5. 都没找到时，在用户目录和各个盘根目录下**有限度地翻两层**，找带 `profiles\` 的 DSH 家目录（最多看 4000 个目录，不会变成全盘扫描）。

同时也会认出 profile 里 `node_modules\` 下**插件包自带的补丁**（用插件管理器装的人设预设通常在这里），以及 `web` / `desktop` 等所有 profile。界面上是一个下拉框，可以在这些文件之间切换。

**preset 和 persona 行也不写死**：

- preset 按「配置里指定的 → DSH 注册表里的 `selectedDefault`（当前选中的那个）→ 第一个带 persona 的 preset」自动挑；
- persona 行按包名 `@deepseek-ai/dsh-persona`、行 id、正文字段（`prefix`；旧文件的 `text` 也认）自动识别；
- 配置文件里写的 preset/行 id 如果在这台机器上不存在，**自动降级**为自动识别，不会报错——别人拷来一份 `config.json` 也不影响使用。

## 界面

- **顶部**：`补丁文件` 下拉框（自动找到的候选）+ `重新扫描` + `选择 yml…`（手动挑文件，会记住到 `config.json`）。**换文件后 preset 下拉会立刻跟着换**；某个文件里没有可编辑的 preset 时，下拉会清空并说明原因，不会留着上一个文件的 preset 骗人。
- **第二行**：`preset` 下拉框（显示「有人设 / 无人设」和字符数），旁边是 **`新建预设…`**、**`编辑信息…`**、**`删除此预设…`**，右边显示实际写入的文件路径。每个文件各自记住你选过的 preset，来回切换不会串。
- **左边**：当前 preset 的人设正文，可直接编辑。`Ctrl+S` 保存。
- **右边**：`personas\` 里的人设文件列表，双击或点「载入到编辑框」把它读进来（**不会**自动写入；要落盘得点「保存到预设」）。
- **底部**：`保存到预设` / `重新载入` / `撤销上次保存` / `复制人设` / `退出`。
- 状态栏会说明「正在编辑哪个 preset、为什么选它、persona 行是哪个、用哪个字段」。
- **另存为**：把当前编辑框的内容存成新的人设文件（自动补 `.md`）。
- 标题栏出现 `*` 表示有未保存改动；关窗时会问一次。
- 没找到 yml 时不会瞎写：状态栏提示，用「重新扫描」或「选择 yml…」指定。

## 编辑预设信息（id、显示名、说明、排序）

选中一个 preset，点 **`编辑信息…`** 就能改：

- **preset id**：小写字母/数字/连字符。id 一改，加载行（`- id: preset-…`）和 `config.id` 一起改，DSH 注册表里的 `selectedDefault` 如果正指着它也会跟着更新；id 会先查重，别人用了就拒绝。
- **显示名 / 说明**：直接改；说明留空就删掉这一行。
- **排序（order）**：数字，越小越靠前；留空删掉。

写入前照常整份校验（只允许这几类行变化、preset 队列和 persona 行必须原样）+ 自动备份。

## 新建预设（把一个 preset 变成属于你自己的）

点 **`新建预设…`**：填一个 preset id、显示名，选一个**模板**，确定后程序会在当前补丁文件末尾追加一个全新的 preset 声明，人设用编辑框里的正文（也可以让它生成一段开头）。创建完自动选中新 preset，DSH 一两秒内热加载，马上能编辑、能保存。

**模板就是插件表**：新 preset 的插件清单（bash、编辑器这些工具）从模板**原样复制**——优先用这台机器上已经能跑的 preset（比如你现在用的那个），其次用 DSH 自带的（`standard` 等，直接从安装目录的 `app.asar` 里读，桌面版硬盘上没有解包也能读到）。这样保证插件包名和本机 DSH 版本匹配——**绝不凭空编插件名**，写错包名 DSH 会整个会话起不来。本机一个模板都找不到时，会明确拒绝并告诉你怎么办，而不是写一个必坏的 preset 出来。

同一台机器上可以有任意多个 preset，互不干扰：在 DSH 新开对话时选哪个就用哪个。

**`删除此预设…`** 是新建的后悔药：从当前文件移除这个 preset（整个 `- insert:` 块一起拿走），删之前确认、写之前备份；如果 DSH 的选中项正指着它，会自动落到注册行自己的 `default` 上，不会留一个指向空气的选中项。

## 保存时的四道保险

1. **规范化**：YAML 字面块表达不了末尾空行和行尾空格，粘贴文本通常带末尾换行——先统一到同一个规范形式（避免"内容不一致"的误判）。
2. **回读比对**：写回后立刻用同一个读取器把文本读回来，与输入逐字比较，不一致就放弃。
3. **结构校验**：`- id:` 行数与 `plugins:` 键数必须不变（新建/删除则必须恰好差那么多），防止改坏文档结构；新建必须是纯追加（或正好替换空列表 `[]`），删除后其他 preset 必须逐字不变。
4. **时间戳备份 + 原子写**：先备份到 `personas\_backups`（保留最近 40 份），再用同目录临时文件 `os.replace` 覆盖。任何一步失败，原文件一个字节都不动。

**字节级保真**：只替换那一个正文块，其余每一行（注释、`!!js` 标签、缩进）原样保留；文件原本是 CRLF（记事本编辑过的）就还是 CRLF，文件末尾的换行也不会被吃掉。

「撤销上次保存」是**弹栈**语义：撤销一次消耗一份备份，再点就继续往前退；备份用完会提示「没有可用备份」。

## 命令行

```powershell
人设编辑-cli.exe --list                        # 列出补丁文件、preset，以及可以复制为模板的 preset
人设编辑-cli.exe --check                       # 打印当前状态，什么都不改
人设编辑-cli.exe --print                       # 把当前人设打到标准输出
人设编辑-cli.exe --set-file D:\...\某某.md      # 用某个人设文件覆盖
人设编辑-cli.exe --revert                      # 撤销上一次保存
人设编辑-cli.exe --preset mine                 # 指定要编辑的 preset（默认自动选择）
人设编辑-cli.exe --patch <文件> --personas <目录> --persona-row persona

# 新建一个 preset（模板自动挑最合适的；人设用 --set-file 的正文）
人设编辑-cli.exe --create-preset my-persona --preset-name 我的人设 --set-file D:\...\某某.md
人设编辑-cli.exe --create-preset my-persona --template standard --select   # 指定模板并设为 DSH 当前项
人设编辑-cli.exe --delete-preset my-persona                                # 删除（留备份）

# 编辑 preset 的 id / 显示名 / 说明 / 排序
人设编辑-cli.exe --rename-preset new-id                     # 改 id（加载行、config.id、选中项一起改）
人设编辑-cli.exe --preset test --preset-name 新名字          # 只改显示名（id 不动）
人设编辑-cli.exe --rename-preset new-id --preset-name 名字 --preset-description 说明 --preset-order 3
```

## 配置（可选）

`config.json` 放在 exe 同目录，**可以完全不存在**。这台机器上写了就用，写了但路径不存在会自动忽略：

```json
{
  "patchFile": "C:/Users/你/.dsh/profiles/desktop/cordis.patch.yml",
  "personasDir": "D:/我的人设",
  "presetId": "mine",
  "personaRowId": "persona"
}
```

- `patchFile` / `personasDir`：覆盖自动发现；
- `presetId` / `personaRowId`：只是"偏好"，不存在就退回自动识别；
- `appAsar`（可选）：DSH 桌面版 `resources\app.asar` 的路径。新建预设找自带模板时通常会自己定位到（运行中的 DSH 进程、常见安装位置、有限度的目录搜索），个别装在奇怪位置的可以用它指一下；
- 在界面里点「选择 yml…」会把选中的文件写进 `config.json`，下次直接用它；
- 目录/文件在**这台机器上不存在**时会被忽略（比如别人拷来的 `config.json`），不会报错也不会在别的盘乱建目录；
- exe 在 `dist\` 里，所以它读的是 `dist\config.json`。想把这个文件夹当纯便携版发出去，删掉 `dist\config.json` 即可。

## 自己重新打包

```powershell
powershell -File build.ps1      # 需要 python（3.10+）+ pyinstaller 6.x，产出 dist\ 下的两个 exe
python selftest.py              # 115 项自测：发现、解析、写入、零漂移、CRLF、撤销、配置、
                                # 新建/删除/编辑 preset、asar 读取、GUI 切换
```

自测全部在临时目录里对**副本**操作，从不写真实的 `cordis.patch.yml`；最后一项检查只读地验证本机真实文件能否自动识别。
