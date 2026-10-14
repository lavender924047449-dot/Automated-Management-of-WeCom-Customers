# 组装目录分发包：启动.exe、python\、程序\、data\、vendor\
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Out = Join-Path $Root "dist-portable"
$Cache = Join-Path $PSScriptRoot "cache"
$PyVersion = "3.11.9"
$EmbedUrl = "https://www.python.org/ftp/python/$PyVersion/python-$PyVersion-embed-amd64.zip"
$EmbedZip = Join-Path $Cache "python-$PyVersion-embed-amd64.zip"
$GetPip = Join-Path $Cache "get-pip.py"
$programName = -join @([char]0x7A0B, [char]0x5E8F)
$launcherName = (-join @([char]0x542F, [char]0x52A8)) + ".exe"

New-Item -ItemType Directory -Force -Path $Cache | Out-Null
if (Test-Path -LiteralPath $Out) {
    Remove-Item -LiteralPath $Out -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $Out | Out-Null

$csc = Join-Path $env:WINDIR "Microsoft.NET\Framework64\v4.0.30319\csc.exe"
if (-not (Test-Path -LiteralPath $csc)) {
    throw "未找到 csc.exe"
}
& $csc /nologo /codepage:65001 /target:winexe /r:System.Windows.Forms.dll `
    "/out:$(Join-Path $Out $launcherName)" `
    (Join-Path $PSScriptRoot "portable_launcher.cs")
if ($LASTEXITCODE -ne 0) { throw "启动器编译失败" }

if (-not (Test-Path -LiteralPath $EmbedZip)) {
    Write-Host "下载嵌入式 Python $PyVersion"
    Invoke-WebRequest -Uri $EmbedUrl -OutFile $EmbedZip
}
$pythonDir = Join-Path $Out "python"
Expand-Archive -LiteralPath $EmbedZip -DestinationPath $pythonDir -Force
$pth = Get-ChildItem -LiteralPath $pythonDir -Filter "python*._pth" | Select-Object -First 1
if (-not $pth) { throw "未找到 python._pth" }
$utf8 = New-Object System.Text.UTF8Encoding $false
[System.IO.File]::WriteAllLines($pth.FullName, @(
    "python311.zip"
    "."
    ("..\{0}" -f $programName)
    "Lib\site-packages"
    "import site"
), $utf8)

if (-not (Test-Path -LiteralPath $GetPip)) {
    Invoke-WebRequest -Uri "https://bootstrap.pypa.io/get-pip.py" -OutFile $GetPip
}
$py = Join-Path $pythonDir "python.exe"
& $py $GetPip
if ($LASTEXITCODE -ne 0) { throw "get-pip 失败" }
& $py -m pip install --disable-pip-version-check -r (Join-Path $Root "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "依赖安装失败" }

$program = Join-Path $Out $programName
New-Item -ItemType Directory -Force -Path $program | Out-Null
foreach ($name in @("gui", "app", "runtime")) {
    & robocopy (Join-Path $Root $name) (Join-Path $program $name) /E /NFL /NDL /NJH /NJS /XD __pycache__ artifacts .pytest_cache | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "复制 $name 失败: $LASTEXITCODE" }
}
Copy-Item -LiteralPath (Join-Path $Root "config.yaml") -Destination (Join-Path $Out "config.yaml")
Copy-Item -LiteralPath (Join-Path $Root "pyproject.toml") -Destination (Join-Path $Out "pyproject.toml")

& robocopy (Join-Path $Root "vendor") (Join-Path $Out "vendor") /E /NFL /NDL /NJH /NJS /XD __pycache__ | Out-Null
if ($LASTEXITCODE -ge 8) { throw "复制 vendor 失败: $LASTEXITCODE" }

$distData = Join-Path $Root "dist\data"
$repoData = Join-Path $Root "data"
$dataDst = Join-Path $Out "data"
if (Test-Path -LiteralPath $distData) {
    & robocopy $distData $dataDst /E /NFL /NDL /NJH /NJS | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "复制 dist\data 失败: $LASTEXITCODE" }
} elseif (Test-Path -LiteralPath $repoData) {
    & robocopy $repoData $dataDst /E /NFL /NDL /NJH /NJS | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "复制 data 失败: $LASTEXITCODE" }
} else {
    New-Item -ItemType Directory -Force -Path $dataDst | Out-Null
}

Copy-Item -LiteralPath (Join-Path $PSScriptRoot "使用说明-目录分发.txt") -Destination (Join-Path $Out "使用说明.txt")

Write-Host "完成: $Out"
