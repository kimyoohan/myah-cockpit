# to_gif.ps1
# mp4(또는 다른 영상) → 고품질 GIF 변환. ffmpeg 2단계(팔레트 생성 → 적용)로
# 화질 좋고 용량 작게 뽑는다. GitHub README 용에 맞춤.
#
# 요구: ffmpeg 이 PATH 에 있어야 함.
#
# 사용법 (PowerShell):
#   .\to_gif.ps1 Video.mp4
#       → docs\demo.gif (너비 800px, 12fps)
#   .\to_gif.ps1 Video.mp4 -Width 1000 -Fps 15
#       → 옵션 조절
#   .\to_gif.ps1 Video.mp4 -Out demo.gif
#       → 출력 경로 지정
#   .\to_gif.ps1 Video.mp4 -Start 2 -Duration 20
#       → 2초 지점부터 20초만 변환(앞뒤 잘라내기)
#
# 실행정책 막히면:
#   PowerShell -ExecutionPolicy Bypass -File .\to_gif.ps1 Video.mp4

param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$Src,                    # 입력 영상 (예: Video.mp4)
    [int]$Width = 800,               # GIF 가로 px (세로는 비율 자동). 용량 줄이려면 600
    [int]$Fps = 12,                  # 초당 프레임. 부드럽게는 15, 용량 줄이려면 10
    [string]$Out = "docs\demo.gif",  # 출력 경로 (README 용 기본)
    [double]$Start = 0,              # 시작 지점(초) — 앞부분 잘라내기
    [double]$Duration = 0            # 길이(초). 0 이면 끝까지
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Fail($m) { Write-Host "`n[X] $m" -ForegroundColor Red; exit 1 }

# 0) 점검
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    Fail "ffmpeg 이 없습니다. https://ffmpeg.org 에서 설치하고 PATH 에 추가하세요."
}
if (-not (Test-Path $Src)) { Fail "입력 파일을 찾을 수 없습니다: $Src" }

# 출력 폴더 만들기 (docs\ 등)
$outDir = Split-Path -Parent $Out
if ($outDir -and -not (Test-Path $outDir)) {
    New-Item -ItemType Directory -Path $outDir -Force | Out-Null
}

# 구간 옵션 (-ss / -t)
$trim = @()
if ($Start -gt 0)    { $trim += @("-ss", "$Start") }
if ($Duration -gt 0) { $trim += @("-t",  "$Duration") }

# 공통 필터: fps 낮추고, 너비 맞춰 축소(세로 자동, 짝수 보정), lanczos 보간
$filter = "fps=$Fps,scale=${Width}:-1:flags=lanczos"

$palette = Join-Path $env:TEMP ("palette_" + [guid]::NewGuid().ToString("N") + ".png")

Write-Host "== mp4 → GIF 변환 ==" -ForegroundColor Cyan
Write-Host "  입력 : $Src"
Write-Host "  출력 : $Out   (너비 ${Width}px, ${Fps}fps)"
if ($trim.Count) { Write-Host "  구간 : start=$Start  duration=$Duration" }
Write-Host ""

try {
    # 1단계: 팔레트 생성 (그 영상에 최적인 256색 추출 → 화질↑)
    Write-Host "[1/2] 팔레트 생성…" -ForegroundColor Yellow
    & ffmpeg -y @trim -i $Src -vf "$filter,palettegen=stats_mode=diff" $palette 2>$null
    if ($LASTEXITCODE -ne 0) { Fail "팔레트 생성 실패." }

    # 2단계: 팔레트 적용해서 GIF 출력 (디더링으로 그라데이션 부드럽게)
    Write-Host "[2/2] GIF 생성…" -ForegroundColor Yellow
    & ffmpeg -y @trim -i $Src -i $palette `
        -lavfi "$filter [x]; [x][1:v] paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle" `
        $Out 2>$null
    if ($LASTEXITCODE -ne 0) { Fail "GIF 생성 실패." }
}
finally {
    if (Test-Path $palette) { Remove-Item $palette -Force -ErrorAction SilentlyContinue }
}

# 결과 용량 표시 + GitHub 가이드
$sizeMB = [math]::Round((Get-Item $Out).Length / 1MB, 2)
Write-Host "`n[OK] 완료 → $Out  ($sizeMB MB)" -ForegroundColor Green
if ($sizeMB -gt 10) {
    Write-Host "  ⚠ 10MB 초과 — GitHub 에서 느릴 수 있어요. 줄이려면:" -ForegroundColor Yellow
    Write-Host "     .\to_gif.ps1 $Src -Width 600 -Fps 10   (또는 -Duration 으로 길이 단축)"
} else {
    Write-Host "  README 에 넣기:  ![myah demo]($($Out -replace '\\','/'))"
}
