[CmdletBinding()]
param(
    [switch]$Uninstall,
    [switch]$Extract,
    [switch]$NoRestart,
    [switch]$PauseAtEnd
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$packDir = Join-Path $scriptDir "translated-zh-CN"
$backupDir = Join-Path ([System.IO.Path]::GetTempPath()) "claude-zh-cn-backup"
$utf8NoBom = [System.Text.UTF8Encoding]::new($false)

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Ensure-Administrator {
    param(
        [string[]]$Arguments = @()
    )

    if (Test-IsAdministrator) {
        return
    }

    $argumentList = @(
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        "`"$PSCommandPath`""
    ) + $Arguments

    Start-Process -FilePath "powershell.exe" -Verb RunAs -ArgumentList $argumentList | Out-Null
    exit
}

function Wait-BeforeExit {
    if (-not $PauseAtEnd) {
        return
    }

    Write-Host ""
    [void](Read-Host "按回车关闭窗口")
}

function Write-Utf8File {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Content
    )

    $parent = Split-Path -Parent $Path
    if ($parent) {
        [System.IO.Directory]::CreateDirectory($parent) | Out-Null
    }

    [System.IO.File]::WriteAllText($Path, $Content, $utf8NoBom)
}

function Find-ClaudePath {
    try {
        $pkg = Get-AppxPackage -Name Claude -ErrorAction Stop |
            Sort-Object Version -Descending |
            Select-Object -First 1
        if ($pkg -and $pkg.InstallLocation -and (Test-Path -LiteralPath $pkg.InstallLocation)) {
            return $pkg.InstallLocation
        }
    }
    catch {
    }

    try {
        $deployments = Get-ChildItem "HKLM:\Software\Microsoft\Windows\CurrentVersion\Appx\AppxAllUserStore\Deployments" -ErrorAction Stop |
            Where-Object { $_.PSChildName -like "Claude*" } |
            Sort-Object PSChildName -Descending

        foreach ($deployment in $deployments) {
            $candidate = Join-Path ${env:ProgramFiles} "WindowsApps\$($deployment.PSChildName)"
            if (Test-Path -LiteralPath $candidate) {
                return $candidate
            }
        }
    }
    catch {
    }

    $windowsApps = Join-Path ${env:ProgramFiles} "WindowsApps"
    if (Test-Path -LiteralPath $windowsApps) {
        $candidate = Get-ChildItem -LiteralPath $windowsApps -Directory -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -like "Claude*" } |
            Sort-Object Name -Descending |
            Select-Object -First 1

        if ($candidate) {
            return $candidate.FullName
        }
    }

    return $null
}

function Get-ResourcesPath {
    param(
        [Parameter(Mandatory = $true)][string]$ClaudePath
    )

    $resourcesPath = Join-Path $ClaudePath "app\resources"
    if (Test-Path -LiteralPath $resourcesPath) {
        return $resourcesPath
    }

    return $null
}

function Grant-WriteAccess {
    param(
        [Parameter(Mandatory = $true)][string]$Path
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }

    try {
        $item = Get-Item -LiteralPath $Path -ErrorAction Stop
        $takeownArgs = @("/f", $Path, "/a")
        if ($item.PSIsContainer) {
            $takeownArgs += @("/r", "/d", "Y")
        }

        & takeown.exe @takeownArgs | Out-Null

        $identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
        if ($identity) {
            & icacls.exe $Path "/grant" "${identity}:(F)" "/t" "/c" | Out-Null
        }
    }
    catch {
    }
}

function Backup-File {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $false)][string]$RelativeTo = ""
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return
    }

    # 保留相对路径结构，避免多个同名文件互相覆盖
    if ($RelativeTo -and $Path.StartsWith($RelativeTo, [System.StringComparison]::OrdinalIgnoreCase)) {
        $relative = $Path.Substring($RelativeTo.Length).TrimStart("\")
    }
    else {
        $relative = Split-Path $Path -Leaf
    }

    $dest = Join-Path $backupDir $relative
    [System.IO.Directory]::CreateDirectory((Split-Path -Parent $dest)) | Out-Null
    Copy-Item -LiteralPath $Path -Destination $dest -Force
}

function Get-BackupPath {
    param(
        [Parameter(Mandatory = $true)][string]$OriginalPath,
        [Parameter(Mandatory = $false)][string]$RelativeTo = ""
    )

    if ($RelativeTo -and $OriginalPath.StartsWith($RelativeTo, [System.StringComparison]::OrdinalIgnoreCase)) {
        $relative = $OriginalPath.Substring($RelativeTo.Length).TrimStart("\")
    }
    else {
        $relative = Split-Path $OriginalPath -Leaf
    }

    return Join-Path $backupDir $relative
}

function Patch-JsLanguage {
    param(
        [Parameter(Mandatory = $true)][string]$ResourcesPath
    )

    $assetsDir = Join-Path $ResourcesPath "ion-dist\assets\v1"
    if (-not (Test-Path -LiteralPath $assetsDir -PathType Container)) {
        Write-Host "  [警告] 未找到 assets 目录，跳过 JS 补丁" -ForegroundColor Yellow
        return $false
    }

    $jsFiles = Get-ChildItem -LiteralPath $assetsDir -Filter "*.js" -File -ErrorAction SilentlyContinue
    if (-not $jsFiles) {
        Write-Host "  [警告] 未找到前端 JS，跳过 JS 补丁" -ForegroundColor Yellow
        return $false
    }

    # 只用正则匹配，不依赖硬编码变量名（每次 Claude 打包变量名都会变）
    $regexAdd = [regex]'((?:[\w$]+)=\["en-US"(?:,"[^"]+")+)\]'

    $patched = $false

    foreach ($jsFile in $jsFiles) {
        Grant-WriteAccess -Path $jsFile.FullName

        $content = [System.IO.File]::ReadAllText($jsFile.FullName)
        if ($content.Contains('"zh-CN"')) {
            Write-Host "  已注册: $($jsFile.Name)"
            $patched = $true
            continue
        }

        Backup-File -Path $jsFile.FullName -RelativeTo $ResourcesPath

        $newContent = $regexAdd.Replace($content, '$1,"zh-CN"]', 1)
        if ($newContent -ne $content) {
            Write-Utf8File -Path $jsFile.FullName -Content $newContent
            Write-Host "  JS补丁已应用: $($jsFile.Name)"
            $patched = $true
            continue
        }

        # 新版 Claude 会把语言白名单拆到非 index chunk；普通业务 chunk 不包含白名单是正常的。
    }

    if (-not $patched) {
        Write-Host "  [警告] 未匹配到语言列表 (Claude 可能已更新)" -ForegroundColor Yellow
    }

    return $patched
}

function Unpatch-JsLanguage {
    param(
        [Parameter(Mandatory = $true)][string]$ResourcesPath
    )

    $assetsDir = Join-Path $ResourcesPath "ion-dist\assets\v1"
    if (-not (Test-Path -LiteralPath $assetsDir -PathType Container)) {
        Write-Host "  [警告] 未找到 assets 目录" -ForegroundColor Yellow
        return
    }

    $jsFiles = Get-ChildItem -LiteralPath $assetsDir -Filter "*.js" -File -ErrorAction SilentlyContinue
    # 只用正则移除，不依赖硬编码变量名
    $regexRemove = [regex]'((?:[\w$]+)=\[(?:"[^"]+",)+)"zh-CN"\]'

    foreach ($jsFile in $jsFiles) {
        $backupPath = Get-BackupPath -OriginalPath $jsFile.FullName -RelativeTo $ResourcesPath

        if (Test-Path -LiteralPath $backupPath -PathType Leaf) {
            Grant-WriteAccess -Path $jsFile.FullName
            Copy-Item -LiteralPath $backupPath -Destination $jsFile.FullName -Force
            Write-Host "  从备份恢复: $($jsFile.Name)"
            continue
        }

        Grant-WriteAccess -Path $jsFile.FullName
        $content = [System.IO.File]::ReadAllText($jsFile.FullName)

        if (-not $content.Contains('"zh-CN"')) {
            Write-Host "  无需恢复: $($jsFile.Name)"
            continue
        }

        $newContent = $regexRemove.Replace($content, '$1]', 1)
        if ($newContent -ne $content) {
            Write-Utf8File -Path $jsFile.FullName -Content $newContent
            Write-Host "  语言注册已恢复: $($jsFile.Name)"
            continue
        }

        Write-Host "  [警告] 无法移除 zh-CN: $($jsFile.Name)" -ForegroundColor Yellow
        Write-Host "  建议重新安装 Claude Desktop" -ForegroundColor Yellow
    }

    if (Test-Path -LiteralPath $backupDir -PathType Container) {
        Remove-Item -LiteralPath $backupDir -Recurse -Force -ErrorAction SilentlyContinue
        Write-Host "  备份已清理"
    }
}

function Update-Config {
    param(
        [Parameter(Mandatory = $true)][string]$Locale
    )

    $packageBase = Join-Path ${env:LOCALAPPDATA} "Packages\Claude_pzs8sxrjxfjjc"
    $configPaths = @(
        (Join-Path ${env:APPDATA} "Claude\config.json"),
        (Join-Path ${env:APPDATA} "Claude-3p\config.json"),
        (Join-Path ${env:LOCALAPPDATA} "Claude\config.json"),
        (Join-Path ${env:LOCALAPPDATA} "Claude-3p\config.json"),
        (Join-Path $packageBase "LocalCache\Roaming\Claude\config.json"),
        (Join-Path $packageBase "LocalCache\Roaming\Claude-3p\config.json")
    )

    foreach ($configPath in ($configPaths | Select-Object -Unique)) {
        if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
            continue
        }

        try {
            $raw = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8

            # 用正则原地替换 locale 值，保留原始格式和其他字段不变
            if ($raw -match '"locale"\s*:') {
                $newRaw = $raw -replace '("locale"\s*:\s*)"[^"]*"', "`$1`"$Locale`""
            }
            else {
                # locale 字段不存在，在最后一个 } 前插入
                $newRaw = $raw -replace '(\s*)\}(\s*)$', ",`r`n  `"locale`": `"$Locale`"`$1}`$2"
            }

            Write-Utf8File -Path $configPath -Content $newRaw
            Write-Host "  $(Split-Path $configPath -Leaf)"
        }
        catch {
            Write-Host "  [警告] 配置更新失败: $(Split-Path $configPath -Leaf) ($($_.Exception.Message))" -ForegroundColor Yellow
        }
    }
}

function Get-ClaudeApplicationId {
    param(
        [Parameter(Mandatory = $true)][string]$ClaudePath
    )

    $manifestPath = Join-Path $ClaudePath "AppxManifest.xml"
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        return $null
    }

    try {
        [xml]$manifest = Get-Content -LiteralPath $manifestPath -Raw
        $application = @($manifest.Package.Applications.Application | Select-Object -First 1)[0]
        if ($application -and $application.Id) {
            return [string]$application.Id
        }
    }
    catch {
    }

    return $null
}

function Get-ClaudePackageFamilyName {
    param(
        [Parameter(Mandatory = $true)][string]$ClaudePath
    )

    try {
        $resolvedClaudePath = [System.IO.Path]::GetFullPath($ClaudePath).TrimEnd("\")
        $pkg = Get-AppxPackage -Name Claude -ErrorAction Stop |
            Sort-Object Version -Descending |
            Where-Object {
                $_.InstallLocation -and
                ([System.IO.Path]::GetFullPath($_.InstallLocation).TrimEnd("\") -ieq $resolvedClaudePath)
            } |
            Select-Object -First 1

        if ($pkg -and $pkg.PackageFamilyName) {
            return [string]$pkg.PackageFamilyName
        }
    }
    catch {
    }

    try {
        [xml]$manifest = Get-Content -LiteralPath (Join-Path $ClaudePath "AppxManifest.xml") -Raw
        $identityName = [string]$manifest.Package.Identity.Name
        $folderName = Split-Path -Leaf $ClaudePath
        if ($identityName -and ($folderName -match "__([^_\\]+)$")) {
            return "$identityName`_$($Matches[1])"
        }
    }
    catch {
    }

    return $null
}

function Get-ClaudeAppUserModelId {
    param(
        [Parameter(Mandatory = $true)][string]$ClaudePath
    )

    $packageFamilyName = Get-ClaudePackageFamilyName -ClaudePath $ClaudePath
    $applicationId = Get-ClaudeApplicationId -ClaudePath $ClaudePath

    if ($packageFamilyName -and $applicationId) {
        return "$packageFamilyName!$applicationId"
    }

    return $null
}

function Start-ClaudeWithExplorer {
    param(
        [Parameter(Mandatory = $true)][string]$Target
    )

    try {
        $argument = $Target
        if ($Target -notlike "shell:*") {
            $argument = "`"$Target`""
        }

        Start-Process -FilePath "explorer.exe" -ArgumentList $argument | Out-Null
        return $true
    }
    catch {
        return $false
    }
}

function Start-ClaudeWithWmi {
    param(
        [Parameter(Mandatory = $true)][string]$ExePath
    )

    try {
        $workingDirectory = Split-Path -Parent $ExePath
        $result = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
            CommandLine      = "`"$ExePath`""
            CurrentDirectory = $workingDirectory
        }

        return ($result.ReturnValue -eq 0)
    }
    catch {
        return $false
    }
}

function Start-ClaudeDetached {
    param(
        [Parameter(Mandatory = $true)][string]$ClaudePath
    )

    $appUserModelId = Get-ClaudeAppUserModelId -ClaudePath $ClaudePath
    if ($appUserModelId) {
        if (Start-ClaudeWithExplorer -Target "shell:AppsFolder\$appUserModelId") {
            return $true
        }
    }

    $exe = Join-Path $ClaudePath "app\claude.exe"
    if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) {
        return $false
    }

    if (Start-ClaudeWithExplorer -Target $exe) {
        return $true
    }

    return (Start-ClaudeWithWmi -ExePath $exe)
}

function Restart-Claude {
    try {
        Stop-Process -Name "claude" -Force -ErrorAction SilentlyContinue
    }
    catch {
    }

    Start-Sleep -Seconds 2

    $claudePath = Find-ClaudePath
    if (-not $claudePath) {
        return
    }

    if (Start-ClaudeDetached -ClaudePath $claudePath) {
        Start-Sleep -Seconds 3
        Write-Host "Claude Desktop 已重启"
    }
    else {
        Write-Host "  [警告] 自动启动 Claude 失败，请手动打开 Claude Desktop" -ForegroundColor Yellow
    }
}

function Get-RequiredTranslationFiles {
    $required = @(
        [pscustomobject]@{ Name = "ion-dist"; Path = (Join-Path $packDir "ion-dist\zh-CN.json") },
        [pscustomobject]@{ Name = "desktop-shell"; Path = (Join-Path $packDir "desktop-shell\zh-CN.json") },
        [pscustomobject]@{ Name = "statsig"; Path = (Join-Path $packDir "statsig\zh-CN.json") }
    )

    foreach ($item in $required) {
        if (-not (Test-Path -LiteralPath $item.Path -PathType Leaf)) {
            $legacyPath = Join-Path $scriptDir "$($item.Name)\zh-CN.json"
            if (Test-Path -LiteralPath $legacyPath -PathType Leaf) {
                $item.Path = $legacyPath
            }
        }
    }

    return $required
}

function Resolve-ClaudeResources {
    $claudePath = Find-ClaudePath
    if (-not $claudePath) {
        throw "未检测到 Claude Desktop"
    }

    $resourcesPath = Get-ResourcesPath -ClaudePath $claudePath
    if (-not $resourcesPath) {
        throw "未找到 resources 目录"
    }

    return [pscustomobject]@{
        ClaudePath = $claudePath
        ResourcesPath = $resourcesPath
    }
}

function Print-Banner {
    Write-Host ""
    Write-Host "  Claude Desktop 简体中文语言包" -ForegroundColor Cyan
    Write-Host "  https://github.com/LifeActor/Claude_zh-CN_LanguagePack" -ForegroundColor DarkGray
    Write-Host "  作者: LifeActor  |  基于 Pheo Hu / RICK 的工作  |  CC BY-NC-SA 4.0" -ForegroundColor DarkGray
    Write-Host "  本项目与 Anthropic 无关，仅供个人学习使用。" -ForegroundColor DarkGray
    Write-Host ""
}

function Install-LanguagePack {
    Print-Banner
    Write-Host ""
    Write-Host "=== Claude Desktop 中文语言包安装 ==="
    Write-Host ""
    Write-Host "无需 Python，正在直接使用 PowerShell 安装。"

    $required = Get-RequiredTranslationFiles
    foreach ($item in $required) {
        if (-not (Test-Path -LiteralPath $item.Path -PathType Leaf)) {
            throw "缺少翻译文件: $($item.Path)"
        }

        $sizeKb = [math]::Floor((Get-Item -LiteralPath $item.Path).Length / 1KB)
        Write-Host ("  {0}: OK ({1}KB)" -f $item.Name, $sizeKb)
    }

    Write-Host ""
    Write-Host "[1/5] 查找 Claude Desktop..."
    $resolved = Resolve-ClaudeResources
    Write-Host "  Claude: $($resolved.ClaudePath)"

    Write-Host ""
    Write-Host "[2/5] 获取写入权限..."
    $pathsToGrant = @(
        $resolved.ResourcesPath,
        (Join-Path $resolved.ResourcesPath "ion-dist"),
        (Join-Path $resolved.ResourcesPath "ion-dist\i18n"),
        (Join-Path $resolved.ResourcesPath "ion-dist\i18n\statsig"),
        (Join-Path $resolved.ResourcesPath "ion-dist\assets"),
        (Join-Path $resolved.ResourcesPath "ion-dist\assets\v1")
    )

    foreach ($path in $pathsToGrant) {
        Grant-WriteAccess -Path $path
    }

    $assetsDir = Join-Path $resolved.ResourcesPath "ion-dist\assets\v1"
    if (Test-Path -LiteralPath $assetsDir -PathType Container) {
        Get-ChildItem -LiteralPath $assetsDir -Filter "*.js" -File -ErrorAction SilentlyContinue |
            ForEach-Object { Grant-WriteAccess -Path $_.FullName }
    }

    Write-Host "  权限处理完成"

    Write-Host ""
    Write-Host "[3/5] 安装翻译文件..."
    $targets = @(
        [pscustomobject]@{
            Name   = "ion-dist"
            Source = $required[0].Path
            Target = (Join-Path $resolved.ResourcesPath "ion-dist\i18n\zh-CN.json")
        },
        [pscustomobject]@{
            Name   = "desktop-shell"
            Source = $required[1].Path
            Target = (Join-Path $resolved.ResourcesPath "zh-CN.json")
        },
        [pscustomobject]@{
            Name   = "statsig"
            Source = $required[2].Path
            Target = (Join-Path $resolved.ResourcesPath "ion-dist\i18n\statsig\zh-CN.json")
        }
    )

    foreach ($target in $targets) {
        [System.IO.Directory]::CreateDirectory((Split-Path -Parent $target.Target)) | Out-Null
        $relativeTarget = $target.Target.Substring($resolved.ResourcesPath.Length).TrimStart("\")
        Copy-Item -LiteralPath $target.Source -Destination $target.Target -Force
        Write-Host "  → $relativeTarget"
    }

    # 安装 overrides 文件（前端对非 en-US 语言会额外 fetch 此文件）
    $overridesSource = Join-Path $packDir "ion-dist\zh-CN.overrides.json"
    $overridesTarget = Join-Path $resolved.ResourcesPath "ion-dist\i18n\zh-CN.overrides.json"
    if (Test-Path -LiteralPath $overridesSource -PathType Leaf) {
        Copy-Item -LiteralPath $overridesSource -Destination $overridesTarget -Force
        Write-Host "  → ion-dist\i18n\zh-CN.overrides.json"
    }
    else {
        # overrides 文件不存在时写入空对象，避免前端 fetch 404 后静默失败
        Write-Utf8File -Path $overridesTarget -Content "{}`n"
        Write-Host "  → ion-dist\i18n\zh-CN.overrides.json (空占位)"
    }

    Write-Host ""
    Write-Host "[4/5] 注册中文语言..."
    [void](Patch-JsLanguage -ResourcesPath $resolved.ResourcesPath)

    Write-Host ""
    Write-Host "[5/5] 更新配置..."
    Update-Config -Locale "zh-CN"

    Write-Host ""
    Write-Host "=== 语言包安装完成 ==="
    if ($NoRestart) {
        Write-Host "请手动重启 Claude Desktop 使更改生效。"
    }
    else {
        Write-Host ""
        Restart-Claude
    }
}

function Uninstall-LanguagePack {
    Print-Banner
    Write-Host ""
    Write-Host "=== Claude Desktop 中文语言包卸载 ==="
    Write-Host ""

    Write-Host "[1/4] 查找 Claude Desktop..."
    $resolved = Resolve-ClaudeResources
    Write-Host "  Claude: $($resolved.ClaudePath)"

    Write-Host ""
    Write-Host "[2/4] 删除翻译文件..."
    foreach ($path in @(
            (Join-Path $resolved.ResourcesPath "ion-dist\i18n\zh-CN.json"),
            (Join-Path $resolved.ResourcesPath "ion-dist\i18n\zh-CN.overrides.json"),
            (Join-Path $resolved.ResourcesPath "zh-CN.json"),
            (Join-Path $resolved.ResourcesPath "ion-dist\i18n\statsig\zh-CN.json")
        )) {
        if (Test-Path -LiteralPath $path -PathType Leaf) {
            Grant-WriteAccess -Path $path
            Remove-Item -LiteralPath $path -Force
        }
    }
    Write-Host "  翻译文件已删除"

    Write-Host ""
    Write-Host "[3/4] 恢复语言注册..."
    Unpatch-JsLanguage -ResourcesPath $resolved.ResourcesPath

    Write-Host ""
    Write-Host "[4/4] 恢复配置..."
    Update-Config -Locale "en-US"

    Write-Host ""
    Write-Host "=== 语言包卸载完成 ==="
    if ($NoRestart) {
        Write-Host "请手动重启 Claude Desktop 使更改生效。"
    }
    else {
        Write-Host ""
        Restart-Claude
    }
}

function Extract-EnglishFiles {
    Print-Banner
    Write-Host ""
    Write-Host "=== Claude Desktop 英文文本提取 ==="
    Write-Host ""

    Write-Host "[1/3] 查找 Claude Desktop..."
    $resolved = Resolve-ClaudeResources
    Write-Host "  Claude: $($resolved.ClaudePath)"

    $enDir = Join-Path $scriptDir "extracted-en-US"
    $templateDir = Join-Path $scriptDir "translation-template"
    $targets = @(
        [pscustomobject]@{ Name = "ion-dist"; Source = (Join-Path $resolved.ResourcesPath "ion-dist\i18n\en-US.json") },
        [pscustomobject]@{ Name = "desktop-shell"; Source = (Join-Path $resolved.ResourcesPath "en-US.json") },
        [pscustomobject]@{ Name = "statsig"; Source = (Join-Path $resolved.ResourcesPath "ion-dist\i18n\statsig\en-US.json") }
    )

    Write-Host ""
    Write-Host "[2/3] 提取 en-US 原文..."
    foreach ($target in $targets) {
        if (-not (Test-Path -LiteralPath $target.Source -PathType Leaf)) {
            Write-Host "  [警告] 未找到: $($target.Source)" -ForegroundColor Yellow
            continue
        }

        $enOut = Join-Path $enDir "$($target.Name)\en-US.json"
        $templateOut = Join-Path $templateDir "$($target.Name)\zh-CN.json"
        [System.IO.Directory]::CreateDirectory((Split-Path -Parent $enOut)) | Out-Null
        [System.IO.Directory]::CreateDirectory((Split-Path -Parent $templateOut)) | Out-Null
        Copy-Item -LiteralPath $target.Source -Destination $enOut -Force
        Copy-Item -LiteralPath $target.Source -Destination $templateOut -Force
        Write-Host "  $($target.Name): OK"
    }

    # overrides 文件是各语言自有的覆盖条目，不存在通用英文原文
    # zh-CN.overrides.json 初始为空，后续按需手动添加需要覆盖的条目
    $overridesTplOut = Join-Path $templateDir "ion-dist\zh-CN.overrides.json"
    if (-not (Test-Path -LiteralPath $overridesTplOut -PathType Leaf)) {
        [System.IO.Directory]::CreateDirectory((Split-Path -Parent $overridesTplOut)) | Out-Null
        Write-Utf8File -Path $overridesTplOut -Content "{}`n"
        Write-Host "  ion-dist/overrides: 已创建空占位模板"
    }
    else {
        Write-Host "  ion-dist/overrides: 已存在，跳过"
    }

    Write-Host ""
    Write-Host "[3/3] 提取完成"
    Write-Host ""
    Write-Host "英文原文目录: extracted-en-US/"
    Write-Host "待翻译模板目录: translation-template/"
    Write-Host ""
    Write-Host "翻译说明:"
    Write-Host "  1. 翻译 translation-template 目录中的 zh-CN.json"
    Write-Host "  2. 只修改 JSON 的 value，不要修改 key"
    Write-Host "  3. 不要删除 {count}、{name}、%s、<b>...</b> 等占位符"
    Write-Host "  4. 翻译完成后放到 translated-zh-CN 目录"
    Write-Host "  5. 然后运行安装中文语言包.bat 重新安装"
}

$scriptArgs = @()
if ($Uninstall) {
    $scriptArgs += "-Uninstall"
}
if ($Extract) {
    $scriptArgs += "-Extract"
}
if ($NoRestart) {
    $scriptArgs += "-NoRestart"
}
if ($PauseAtEnd) {
    $scriptArgs += "-PauseAtEnd"
}

if ((-not (Test-IsAdministrator)) -and (-not $Extract)) {
    if ($Uninstall) {
        Update-Config -Locale "en-US"
    }
    else {
        Update-Config -Locale "zh-CN"
    }
}

Ensure-Administrator -Arguments $scriptArgs

$exitCode = 0
try {
    if ($Extract) {
        Extract-EnglishFiles
    }
    elseif ($Uninstall) {
        Uninstall-LanguagePack
    }
    else {
        Install-LanguagePack
    }
}
catch {
    Write-Host ""
    Write-Host "[错误] $($_.Exception.Message)" -ForegroundColor Red
    $exitCode = 1
}
finally {
    Wait-BeforeExit
}

exit $exitCode
