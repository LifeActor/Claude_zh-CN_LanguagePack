# Claude Desktop 简体中文语言包
## Windows 已验证：Claude Desktop 6.2

为 Claude Desktop（Windows / macOS）界面添加简体中文支持。

本仓库基于 [LifeActor](https://github.com/LifeActor) 维护的版本继续适配新版 Claude Desktop；LifeActor 版本基于 [Pheo Hu](https://github.com/pheohu-42/Claude_zh-CN_LanguagePack) 的派生版本迭代，原始语言包由 [RICK](https://linux.do/u/lbls888) 创建（[原帖](https://linux.do/t/topic/2040184)）。本项目保留原许可与致谢，仅供个人学习使用。

<img width="75%" alt="image" src="https://github.com/user-attachments/assets/1b29619a-2b95-4968-b230-36c170627fc7" />

## 版本说明

- 当前仓库已在 **Windows Claude Desktop 6.2** 上验证。
- 说明文档已更新为 Claude Desktop 6.2 适配状态，便于向上游提交 PR。
- Windows 安装脚本已兼容新版 Claude 前端：语言白名单可能不再位于 `index-*.js`，脚本会扫描 `ion-dist/assets/v1/*.js` 并自动注册 `zh-CN`。
- Windows 配置路径已兼容新版 `Claude` / `Claude-3p` 本地配置目录。
- Claude Desktop 更新后会覆盖资源文件；更新客户端后请重新运行安装脚本。

## 前提

- [Claude Desktop](https://claude.ai/download) 已安装
- Windows 10/11 或 macOS
- macOS 需要 Python 3

## 安装

### Windows

1. 下载本仓库（git clone 或 zip）
2. 完全关闭 Claude Desktop
3. 双击 `安装中文语言包.bat`，在管理员权限弹窗中点击「是」
4. 安装完成后重启 Claude Desktop；如界面仍为英文，在 Claude 设置中将语言切换为「中文（简体）」

### macOS

1. 下载本仓库
2. 完全关闭 Claude Desktop
3. 双击 `安装中文语言包.command`，输入登录密码
4. 脚本会自动备份、补丁、重签名并拉起 Claude

`.command` 无法双击时先执行：

```bash
chmod +x ./安装中文语言包.command ./卸载中文语言包.command
```

## 卸载

- **Windows**：双击 `卸载中文语言包.bat`
- **macOS**：双击 `卸载中文语言包.command`（恢复最近一次完整备份）

## Cowork 3P 模式

官方订阅账号不支持直接登录，需使用 3P 模式：

1. 打开 Claude Desktop，不要登录账号
2. `帮助 → 故障排除 → 启用开发者模式`
3. `开发者 → 配置第三方推理`
4. 填写 Gateway base URL（结尾不带 `/v1`）、API key 和模型列表
5. 点击「本地应用」，Claude 重启后即可使用

<img width="50%" alt="image" src="https://github.com/user-attachments/assets/72341911-a76a-45a7-9d42-5b0d35654d3f" />

## 命令行用法

**Windows（PowerShell，需管理员）**

```powershell
powershell -ExecutionPolicy Bypass -File .\LanguagePack.ps1            # 安装
powershell -ExecutionPolicy Bypass -File .\LanguagePack.ps1 -Uninstall # 卸载
powershell -ExecutionPolicy Bypass -File .\LanguagePack.ps1 -Extract   # 提取英文原文
```

**macOS**

```bash
sudo python3 ./LanguagePack.mac.py --user-home "$HOME" --launch            # 安装
sudo python3 ./LanguagePack.mac.py --uninstall --user-home "$HOME" --launch # 卸载
sudo python3 ./LanguagePack.mac.py --extract --app /Applications/Claude.app # 提取英文原文
```

## AI 翻译工具链

由 [LifeActor](https://github.com/LifeActor) 新增，用于在 Claude Desktop 更新后增量补全新增文案。

**环境准备**

```bash
pip install openai python-dotenv
```

在项目根目录创建 `.env`：

```
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_API_KEY=sk-xxxxxxxx
OPENAI_MODEL=gpt-4o
```

兼容任何 OpenAI 协议的推理端点（DeepSeek、阿里云百炼、OpenRouter 等）。

**翻译流程**

```bash
# 1. 从当前安装的 Claude 提取英文原文和翻译模板
powershell -ExecutionPolicy Bypass -File .\LanguagePack.ps1 -Extract   # Windows
sudo python3 ./LanguagePack.mac.py --extract --app /Applications/Claude.app  # macOS

# 2. 拆分 ion-dist 大文件（首次或模板更新后执行）
python split_template.py

# 3. AI 翻译
python translate_ai.py desktop-shell
python translate_ai.py statsig
python translate_ai.py ion-dist all    # 全部分片，--workers N 控制并发数

# 4. 质量检查（可选）
python translate_ai.py check all              # 检查所有目标，日志写入 check-reports/
python translate_ai.py check all --fix        # 删除问题条目，下次翻译时重新生成
python translate_ai.py check all --no-log     # 不写日志
python translate_ai.py check all --log-dir ./reports  # 自定义日志目录

# 5. 重新运行安装脚本使更改生效
```

check 检测项：回退（译文==原文）、缺失、纯英文、译文膨胀、ICU 结构不一致、占位符丢失/多余、HTML 标签丢失/多余、转义序列不匹配。品牌名、URL、路径、纯技术词等不需要翻译的条目会自动跳过，不计入问题。每次检查结果自动写入 `check-reports/` 目录（JSON 格式，含完整原文/译文对照）。

## 常见问题

**安装后界面没变中文**：确认 Claude 已完全退出并重新打开；如仍为英文，在设置中手动切换语言为「中文（简体）」。

**提示未匹配到语言列表**：旧脚本只扫描 `index-*.js`，新版 Claude 可能把语言列表拆到其他 JS chunk。本仓库的 Windows 脚本已改为扫描全部前端 JS 文件。

**脚本报权限错误**：Windows 需管理员权限；macOS 需要 `sudo`、`codesign`、`xattr`。

**Claude 更新后中文消失**：更新会覆盖资源文件，重新运行安装脚本即可。

**macOS 提示「已损坏，无法打开」**：权限问题，参考[解决方案](https://linux.do/t/topic/2044773)。

## 许可

[CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0)。仅供个人学习使用，本项目与 Anthropic 无关。

## 致谢

- 原始语言包：[RICK](https://linux.do/u/lbls888)（[原帖](https://linux.do/t/topic/2040184)）
- 基础派生版本：[Pheo Hu](https://github.com/pheohu-42/Claude_zh-CN_LanguagePack)
- 3P 模式教程：[开启 Claude 3P 模式](https://linux.do/t/topic/2032192)、[自定义模型映射](https://linux.do/t/topic/2034445)
- [Linux Do 社区](https://linux.do/) [![](https://ldo.betax.dev/badge/community)](https://linux.do/)
