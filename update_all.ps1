function Invoke-Git {
    $Command = "git $($Args -join ' ')"

    git @Args

    if ($LASTEXITCODE -ne 0) {
        throw "Git command failed with exit code $LASTEXITCODE: $Command"
    }
}

Write-Host "=== Updating main project ===" -ForegroundColor Cyan
Invoke-Git pull --rebase origin main

Write-Host "=== Initializing submodules ===" -ForegroundColor Cyan
Invoke-Git submodule update --init --recursive

Write-Host "=== Updating official hhru/api submodule ===" -ForegroundColor Cyan
Invoke-Git submodule update --remote --checkout external/hhru-api

Write-Host "=== Git status ===" -ForegroundColor Cyan
Invoke-Git status

Write-Host ""
Write-Host "Update finished." -ForegroundColor Green
Write-Host "If external/hhru-api changed, commit only the pointer in your repo:"
Write-Host "git add external/hhru-api"
Write-Host "git commit -m `"Update hhru API docs`""
Write-Host "git push"
