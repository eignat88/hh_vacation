Write-Host "=== Updating main project ===" -ForegroundColor Cyan
git pull --rebase origin main

Write-Host "=== Initializing submodules ===" -ForegroundColor Cyan
git submodule update --init --recursive

Write-Host "=== Updating official hhru/api submodule ===" -ForegroundColor Cyan
git submodule update --remote --checkout external/hhru-api

Write-Host "=== Git status ===" -ForegroundColor Cyan
git status

Write-Host ""
Write-Host "Update finished." -ForegroundColor Green
Write-Host "If external/hhru-api changed, commit only the pointer in your repo:"
Write-Host "git add external/hhru-api"
Write-Host "git commit -m `"Update hhru API docs`""
Write-Host "git push"