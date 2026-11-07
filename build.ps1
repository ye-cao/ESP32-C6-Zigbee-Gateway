<#
.SYNOPSIS
    ESP32-C6 协调器固件的并行构建入口（Windows / PowerShell 5.1）。

.DESCRIPTION
    只负责「配置 + 编译」，不碰任何业务代码。等价于原来的 `idf.py build`，
    但显式控制 ninja 的并行度，并处理 Windows 上并发写 depfile 的瞬时锁。

    为什么不用 `idf.py -j N build`：本机 ESP-IDF v5.5.1 的 idf.py 把
    `ninja all` 写死在动作里，命令行没有可透传的并行参数；而 CMake 的
    `--parallel N` 是标准入口，会由 CMake 把 -j N 传给 ninja，依赖图与
    目标顺序完全不变（仍然是同一个 build/build.ninja）。

.PARAMETER Jobs
    并行编译任务数。默认 min(逻辑核数, 8)。上限 32。
    8 的取舍：本机 16 逻辑核，单个 riscv32 gcc 进程约占 200–400 MB；
    再往上并行，Windows 上多个进程同时删除/重建 .obj.d 容易撞瞬时文件锁，
    反而触发重编。8 并行是吞吐与稳定性的平衡点。

.PARAMETER NoFallback
    默认在并行构建失败时会用 -j 1 串行收尾一次；传此开关则关闭收尾。

.PARAMETER VerifyOnly
    只做一次「应无工作可做」的增量构建校验，用来确认依赖图完整、
    没有因为并发产生重复编译。

.EXAMPLE
    .\build.ps1                # 8 并行（本机默认）
    .\build.ps1 -Jobs 16       # 手动提高并行度
    .\build.ps1 -VerifyOnly    # 校验是否 no-op
#>
[CmdletBinding()]
param(
    [ValidateRange(1, 32)]
    [int]$Jobs = 0,
    [switch]$NoFallback,
    [switch]$VerifyOnly,
    [string]$IdfPath = "C:/Espressif/frameworks/esp-idf-v5.5.1",
    [string]$BuildDir = "build"
)

$ErrorActionPreference = "Stop"
$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path

# ── 1. 并行度 ────────────────────────────────────────────────────────────
$cores = [Environment]::ProcessorCount
# ★ 2026-11-07：上限从 8 提到 16（= 逻辑核数）。
#   原先保守取 8，理由是「多个 gcc 同时删 .obj.d 会撞瞬时文件锁」——但那个
#   问题已经被两件事消掉了：① ninja 的 `deps = gcc` 规则已移除（不再删
#   depfile）；② ccache 已禁用（不再多一层文件操作）。
#   实测 16 并行下全量编译稳定，产物与 -j 8 完全一致（只影响调度顺序）。
if ($Jobs -le 0) { $Jobs = [Math]::Min($cores, 16) }
if ($Jobs -gt $cores) {
    Write-Warning "并行数 $Jobs 超过逻辑核数 $cores，可能引起调度争抢；建议 <= $cores"
}
Write-Host "构建目录 : $BuildDir"
Write-Host "逻辑核数 : $cores    并行任务数 : $Jobs"

# ── 2. IDF 环境 ──────────────────────────────────────────────────────────
# 不能在 export.ps1 后面接 Out-Null / 重定向：PowerShell 5.1 下会让后续
# 语句被吞掉（项目已知坑）。这里直接让它输出，用 Write-Host 分段隔开。
if (-not (Test-Path "$IdfPath/export.ps1")) {
    throw "找不到 ESP-IDF：$IdfPath/export.ps1"
}
$prevErrPref = $ErrorActionPreference
$ErrorActionPreference = "Continue"
. "$IdfPath/export.ps1"
$ErrorActionPreference = $prevErrPref

# ★★ ccache：直接禁用（2026-11-07 实测）。
#   原来保留 IDF 默认（启用 ccache）。但只要 sdkconfig 一动，所有编译参数就变，
#   ccache 全部 miss —— 此时它不但不省时间，还要为每个文件算 hash、查/写缓存，
#   实测降到 **3 个文件/分钟**（1066 个目标要 5 小时）。关掉后 ccache.exe 变成
#   纯透传，开销可忽略。
#   正常增量改代码时 ccache 是有用的，但那种场景本来只要几分钟，得失不敏感；
#   而全量重编（改 sdkconfig / 换 IDF 版本）才是真痛的地方。
#   要恢复：把下面这行注释掉即可。
$env:CCACHE_DISABLE = "1"

# ccache 保持 IDF 默认行为：不改 CCACHE_SLOPPINESS、不改缓存 hash 规则。
# 这样并行与否只影响调度顺序，不改变任何编译输入 → 产物字节一致。
# 只固定缓存目录，避免不同终端会话落到不同默认路径而白白 miss。
if (-not $env:CCACHE_DIR) {
    $env:CCACHE_DIR = "$env:USERPROFILE/.ccache"
}

Push-Location $projectDir
$sw = [System.Diagnostics.Stopwatch]::StartNew()
# ★ 整个构建段内必须把 ErrorActionPreference 降成 Continue：
#   PowerShell 5.1 会把原生命令写到 stderr 的**任何**一行包装成
#   NativeCommandError，在 Stop 下直接终止脚本。而 cmake/ninja 经常往
#   stderr 打正常提示（典型："Symbol ZB_ENABLED defined in multiple
#   locations" 这类 INFO），于是出现「构建立刻失败、其实一步都没编」。
#   成败一律由下面的 $LASTEXITCODE 判定，不靠这个开关。
$prevEapBuild = $ErrorActionPreference
$ErrorActionPreference = "Continue"

# ★★ 构建期间摘掉代理变量。
#   本机 shell 里常驻 HTTP_PROXY/HTTPS_PROXY=127.0.0.1:<port>（WorkBuddy 的
#   本地代理）。CMake 的组件管理器（idf_component_manager）在配置阶段会去
#   联网检查依赖，走这个代理就会**死等**——现象是 "-- Configuring done"
#   之后十几分钟一个字都不输出、build.ninja 一个字节都不更新，ninja 进程
#   在但只用了 0.2 s CPU。2026-11-07 就是这样白等了 19 分钟。
#   依赖早已在 managed_components/ 里，构建根本不需要网络，摘掉最稳。
#   同时关掉生成期的组件在线检查（IDF_COMPONENT_CHECK_ON_BUILD），
#   双保险。
$__savedProxy = @{}
foreach ($n in @("HTTP_PROXY","HTTPS_PROXY","http_proxy","https_proxy","ALL_PROXY","all_proxy")) {
    $v = [Environment]::GetEnvironmentVariable($n)
    if ($null -ne $v) {
        $__savedProxy[$n] = $v
        [Environment]::SetEnvironmentVariable($n, $null)
    }
}
$env:IDF_COMPONENT_CHECK_ON_BUILD = "0"
try {
    # ── 3. 只做一次配置 + 构建 ──────────────────────────────────────────
    # cmake --build 会自己处理 build.ninja 里的 RERUN_CMAKE 目标，
    # 因此不会像「先 idf.py build 再 ninja」那样重复触发 CMake 生成。
    # -k 0：某个目标因 Windows 文件锁失败时继续推进其它独立目标，
    #       不中断整轮；失败的目标由下面的串行收尾补上。
    if (-not $VerifyOnly) {
        Write-Host "`n[1/2] 并行构建（-j $Jobs）…"
        & cmake --build $BuildDir --parallel $Jobs -- -k 0
        $rc = $LASTEXITCODE

        if ($rc -ne 0 -and -not $NoFallback) {
            Write-Warning "并行构建返回 $rc，通常是 .obj.d 瞬时锁；改用 -j 1 串行收尾。"
            & cmake --build $BuildDir --parallel 1 -- -k 0
            $rc = $LASTEXITCODE
        }
        if ($rc -ne 0) { throw "构建失败，退出码 $rc" }
    }

    # ── 4. 一致性校验：再跑一次必须 no-op ─────────────────────────────
    # 若这里还有目标被重编，说明依赖图被并发破坏或出现了重复编译。
    Write-Host "`n[2/2] 增量一致性校验（期望 no work to do）…"
    # 不要用 `2>&1` 捕获：在 5.1 下它会把 stderr 行转成 ErrorRecord，
    # 既污染输出，也会让 $LASTEXITCODE 的语义变得难以确认。
    & cmake --build $BuildDir --parallel $Jobs
    $vrc = $LASTEXITCODE
    if ($vrc -ne 0) { throw "一致性校验构建失败，退出码 $vrc" }

    $sw.Stop()
    # ★ BuildDir 可能是绝对路径（例如 -BuildDir C:/Temp/b552x）。
    #   Join-Path 遇到绝对的第二段会把它当成相对路径拼到 $projectDir 后面，
    #   于是打印出 "…\esp32c6-zigbee\C:\Temp\b552x\…" 这种不存在的路径，
    #   编译明明成功了却报「找不到产物」。先判一次是否已是绝对路径。
    $bin = "$BuildDir/zigbee-vital-sensor.bin"
    if (-not [System.IO.Path]::IsPathRooted($bin)) {
        $bin = Join-Path $projectDir $bin
    }
    Write-Host "`n耗时 : $([Math]::Round($sw.Elapsed.TotalSeconds, 1)) s"
    if (Test-Path $bin) {
        $f = Get-Item $bin
        Write-Host "产物 : $($f.FullName)  $($f.Length) B  $($f.LastWriteTime)"
    } else {
        throw "构建结束但找不到产物 $bin"
    }

    $elf = "$BuildDir/zigbee-vital-sensor.elf"
    if (-not [System.IO.Path]::IsPathRooted($elf)) {
        $elf = Join-Path $projectDir $elf
    }
    if (Test-Path $elf) {
        Write-Host "ELF  : $elf"
    }
}
finally {
    $ErrorActionPreference = $prevEapBuild
    # 还原代理变量，别污染这个 shell 后续的联网命令
    foreach ($k in $__savedProxy.Keys) {
        [Environment]::SetEnvironmentVariable($k, $__savedProxy[$k])
    }
    Pop-Location
}
