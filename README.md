# B站视频下载工具

一个基于 Python 的命令行 B站视频下载工具，支持**单视频、多P视频、合集**的批量下载，默认通过 DASH 分离音视频流并用 ffmpeg 合并。

---

## 一、功能特性

| 功能 | 说明 |
|------|------|
| 单视频下载 | 输入 BV 号即可下载单个视频 |
| 多P视频 | 自动识别分P（pagelist），按 P 编号分别下载 |
| 合集下载 | 支持 UGC 合集，可通过 `seasons_archives_list` 接口拿到**完整视频列表**（解决大合集跨 section 只拿到当前 section 的问题） |
| 选集下载 | 合集可按序号选择（如 `1,3`、`2-5`），回车=全部，`q`=跳过 |
| 多合集批量 | 一次输入多个 BV 号，空格/逗号/换行分隔 |
| 清晰度选择 | 支持 360P ~ 4K 多档清晰度（`VIDEO_QUALITY` 配置） |
| 断点跳过 | 已存在的文件自动跳过 |
| 流校验 | 下载内容校验为 fMP4 而非 HTML 反爬页，避免下载到错误页面 |
| ffmpeg 合并 | DASH 分离的视频流与音频流自动合并为 mp4 |
| 防风控 | 每集之间可配置睡眠间隔（`SLEEP_BETWEEN`） |

---

## 二、环境要求

- **Python** 3.7+
- **requests** 库
- **ffmpeg**（音视频合并必需，需在系统 PATH 中）

> 仅下载单段视频流时可不依赖 ffmpeg，但 DASH 分离流必须用 ffmpeg 合并。

### 安装依赖

```bash
pip install requests
```

ffmpeg 安装：
- Windows：从 [ffmpeg 官网](https://ffmpeg.org/download.html) 下载，将 `bin` 目录加入系统 PATH。
- 验证：在终端执行 `ffmpeg -version`，能输出版本信息即安装成功。

---

## 三、快速开始

```bash
python B站视频.py
```

运行后按提示输入视频链接，例如：

```
请输入B站视频链接（支持合集/单视频，多个合集用空格/逗号/换行分隔）:
https://www.bilibili.com/video/BV1xx411c7mD](https://www.bilibili.com/video/BV1GJ411x7h7
```

支持直接输入 BV 号（如 `BV1xx411c7mD`），脚本会用正则自动提取。

### 多视频/多合集

```
BV1xx411c7mD, BV2yy411c8nE
```

脚本会逐个解析，对于合集会列出分集列表供选择。

---

## 四、配置项说明

打开 `B站视频.py`，顶部 `# -------- 配置 --------` 区块可调整：

```python
SAVE_DIR = "./downloads"            # 下载保存目录
VIDEO_QUALITY = 64                  # 清晰度：16=360P 32=480P 64=720P 80=1080P 112=1080P+ 116=1080P60 120=4K 125=HDR
USE_FFMPEG = True                   # 是否使用 ffmpeg 合并音视频
SESSDATA = ""                       # 登录 Cookie（1080P+ / 4K 必需）
SLEEP_BETWEEN = 2                   # 每集下载间隔秒数，用于避免风控
```

### 清晰度取值参考

| qn | 分辨率 |
|----|--------|
| 16 | 360P |
| 32 | 480P |
| 64 | 720P |
| 80 | 1080P |
| 112 | 1080P+（高码率） |
| 116 | 1080P60 |
| 120 | 4K |
| 125 | HDR |

> **注意**：1080P+、4K、HDR 需要登录态，必须填入有效的 `SESSDATA`，否则接口返回的最高清晰度通常为 720P。

### SESSDATA 获取方法

1. 浏览器登录 B站。
2. F12 打开开发者工具 → Application/存储 → Cookies → `https://www.bilibili.com`。
3. 找到 `SESSDATA`，复制其值填入 `SESSDATA`。

---

## 五、输出目录结构

脚本会根据视频类型自动组织输出目录：

```
downloads/
├── 合集标题/                         # 合集场景
│   ├── 01_第1集标题/
│   │   └── P01_分P标题.mp4           # 多分P
│   ├── 02_第2集标题.mp4              # 单分P
├── 单视频标题/
│   ├── P01_分P1.mp4                  # 单视频多分P
│   └── P02_分P2.mp4
└── 单视频标题.mp4                     # 单视频单分P（不建文件夹）
```

临时文件统一放在 `SAVE_DIR` 根目录，以 `.tmp_` 开头，下载完成后自动清理；若上次运行异常残留，启动时 `cleanup_tmp()` 会自动清理。

---

## 六、核心逻辑说明

### 1. 合集分集获取（`get_collection_episodes`）

为解决 B站 `view` 接口对分章节大合集只返回当前 BV 所在 section 的问题，脚本采用**两级策略**：

1. **首选** `seasons_archives_list` 接口（分页 `page_num`/`page_size=30`），拿到合集全部视频的 bvid，再逐个调 `pagelist` 获取 cid。
2. **回退** 到 `view.ugc_season.sections[].episodes`，适合小合集。
3. 都失败时当作单视频处理。

### 2. 播放地址获取（`get_play_url`）

调用 `x/player/playurl` 接口，参数 `fnval=16` 请求 DASH 格式返回。

- **DASH 格式**：视频流 `dash.video` 与音频流 `dash.audio` 分离。
  - 视频流：取 `id <= VIDEO_QUALITY` 的最高一档。
  - 音频流：取 `bandwidth` 最高的一档。
- **durl 格式**（旧格式，兼容）：可能多段，用 ffmpeg `concat` 合并。

### 3. 下载与合并（`_download_stream`）

- 分别下载视频 `.m4s` 与音频 `.m4s` 临时文件。
- 调用 `ffmpeg -c copy` 无重编码合并为 mp4。
- 合并完成后删除临时文件。
- 若音频下载失败，退化为只输出视频流。

### 4. 流有效性校验（`dl` / `is_fmp4_stream`）

B站对未登录或高频请求可能返回 HTML 反爬页。脚本通过两层校验避免下载到错误内容：

1. 检查响应 `Content-Type`，若为 `text/json/html` 则报错。
2. 检查文件头是否为 fMP4（`[4字节长度] + 'ftyp'`）。

### 5. 交互输入解析（`parse_choice`）

支持 `1,3`、`2-5`、`1,3-5` 等混合格式，自动去重并升序排序，回车表示全部。

---

## 七、常见问题

**Q: 提示"下载内容不是视频流（疑似 HTML 反爬页）"？**
A: 大概率是请求过于频繁触发风控，或 Cookie 失效。可：
- 增大 `SLEEP_BETWEEN`。
- 检查 `SESSDATA` 是否过期并重新填写。
- 稍后再试。

**Q: 下载到的视频只有画面没有声音？**
A: 音频流下载失败时会退化为只输出视频。请检查网络或重新运行该集。

**Q: 清晰度达不到配置值？**
A: B站根据登录态与大会员权限返回可获取的最高清晰度。`1080P+` 及以上需有效 `SESSDATA`；4K/HDR 还需大会员身份。

**Q: ffmpeg 报错？**
A: 确认 ffmpeg 已安装且在 PATH 中，执行 `ffmpeg -version` 验证。

**Q: 合集只下载到部分视频？**
A: 旧版 B站 `view` 接口对分章节大合集只返回当前 section。本脚本已改用 `seasons_archives_list` 获取完整列表，若仍异常可检查该接口是否被风控。

---

## 八、注意事项

1. **本工具仅供个人学习与离线观看使用**，下载的内容请勿二次传播或用于商业用途，遵守 B站用户协议及相关版权法规。
2. 请勿短时间内高频大量下载，以免账号被风控或封禁。
3. `SESSDATA` 属于敏感登录凭证，请勿提交到公开仓库或分享给他人。
4. 清晰度受视频本身及账号权限限制，并非所有视频都支持 4K/HDR。
