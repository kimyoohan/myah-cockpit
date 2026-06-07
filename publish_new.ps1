# publish_new.ps1
# myah 를 "새 GitHub 저장소"에 올린다. (기존 origin 은 끊고 새 주소로 이동)
# 요구: Windows + git + GitHub CLI(gh) 로그인 상태.
#
# 사용법 (PowerShell 에서 이 파일이 있는 폴더로 이동 후):
#   .\publish_new.ps1 -Repo myah-pro
#   .\publish_new.ps1 -Repo myah-pro -Private        # 비공개로
#
# 처음 실행 시 PowerShell 실행정책 때문에 막히면:
#   PowerShell -ExecutionPolicy Bypass -File .\publish_new.ps1 -Repo myah-pro

param(
    [Parameter(Mandatory = $true)]
    [string]$Repo,                       # 새 저장소 이름 (예: myah-pro)
    [switch]$Private,                    # 주면 비공개, 안 주면 공개
    [string]$Desc = "myah - a Windows dev cockpit: real embedded Chrome + coding-agent terminal"
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot              # 이 스크립트가 있는 폴더(=myah 폴더)에서 동작

function Fail($msg) { Write-Host "`n[X] $msg" -ForegroundColor Red; exit 1 }

# 0) 사전 점검 — git / gh 있는지, gh 로그인 됐는지
Write-Host "== 사전 점검 ==" -ForegroundColor Cyan
if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Fail "git 이 없습니다. https://git-scm.com 에서 설치하세요." }
if (-not (Get-Command gh  -ErrorAction SilentlyContinue)) { Fail "GitHub CLI(gh) 가 없습니다. https://cli.github.com 에서 설치하세요." }
gh auth status 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) { Fail "gh 로그인이 안 돼 있습니다.  먼저:  gh auth login" }

# 로그인 계정(owner) 알아내기
$owner = (gh api user --jq ".login").Trim()
if ([string]::IsNullOrWhiteSpace($owner)) { Fail "GitHub 사용자 정보를 가져오지 못했습니다." }
$full = "$owner/$Repo"
$vis  = if ($Private) { "비공개(private)" } else { "공개(public)" }

Write-Host "  계정 : $owner"
Write-Host "  새 저장소 : $full  ($vis)"
Write-Host ""

# 1) git 저장소 준비 (이 폴더가 아직 git 이 아니면 init)
if (-not (Test-Path ".git")) {
    Write-Host "== git 초기화 ==" -ForegroundColor Cyan
    git init | Out-Null
    git branch -M main
}

# 2) 새 GitHub 저장소 생성 (gh)
Write-Host "== 새 저장소 생성 ==" -ForegroundColor Cyan
$exists = $false
gh repo view $full 2>$null | Out-Null
if ($LASTEXITCODE -eq 0) { $exists = $true }

if ($exists) {
    Write-Host "  이미 존재합니다: $full  → 그 저장소로 푸시합니다." -ForegroundColor Yellow
} else {
    $visFlag = if ($Private) { "--private" } else { "--public" }
    gh repo create $full $visFlag --description $Desc | Out-Null
    if ($LASTEXITCODE -ne 0) { Fail "저장소 생성 실패." }
    Write-Host "  생성 완료: https://github.com/$full" -ForegroundColor Green
}

# 3) origin 을 새 주소로 이동 (기존 origin 은 끊음)
Write-Host "== 원격(origin) 새 주소로 이동 ==" -ForegroundColor Cyan
$newUrl = "https://github.com/$full.git"
$hasOrigin = (git remote) -contains "origin"
if ($hasOrigin) {
    $old = (git remote get-url origin).Trim()
    Write-Host "  기존 origin: $old  →  새 origin: $newUrl"
    git remote set-url origin $newUrl
} else {
    git remote add origin $newUrl
    Write-Host "  origin 추가: $newUrl"
}

# 4) 데모 GIF 자리잡기 — docs\demo.gif 가 없고 루트에 Video.gif 가 있으면 복사
#    (README 가 docs/demo.gif 를 가리키므로, 이미지가 깨지지 않게)
if (-not (Test-Path "docs\demo.gif")) {
    if (Test-Path "Video.gif") {
        if (-not (Test-Path "docs")) { New-Item -ItemType Directory -Path "docs" | Out-Null }
        Copy-Item "Video.gif" "docs\demo.gif" -Force
        Write-Host "== 데모 GIF: Video.gif → docs\demo.gif 복사됨 ==" -ForegroundColor Cyan
    } else {
        Write-Host "  (참고) docs\demo.gif 가 없습니다 — README 상단 데모가 안 보일 수 있어요." -ForegroundColor Yellow
    }
}

# 5) 커밋 + 푸시
Write-Host "== 커밋 & 푸시 ==" -ForegroundColor Cyan
git add -A
# 변경이 있을 때만 커밋 (없으면 건너뜀)
git diff --cached --quiet
if ($LASTEXITCODE -ne 0) {
    $stamp = Get-Date -Format "yyyy-MM-dd HH:mm"
    git commit -m "publish: $stamp" | Out-Null
    Write-Host "  커밋 완료"
} else {
    Write-Host "  새 변경 없음 — 기존 커밋을 푸시합니다." -ForegroundColor Yellow
}

git push -u origin main
if ($LASTEXITCODE -ne 0) { Fail "푸시 실패. (인증/네트워크 또는 원격 충돌 확인)" }

Write-Host "`n[OK] 완료 →  https://github.com/$full" -ForegroundColor Green
Start-Process "https://github.com/$full"
