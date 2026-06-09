function Invoke-Git {
    $Command = "git $($Args -join ' ')"

    git @Args

    if ($LASTEXITCODE -ne 0) {
        throw "Git command failed with exit code $LASTEXITCODE: $Command"
    }
}

function Invoke-GitOutput {
    param(
        [Parameter(ValueFromRemainingArguments = $true)]
        [string[]] $GitArgs
    )

    $Command = "git $($GitArgs -join ' ')"
    $Output = git @GitArgs

    if ($LASTEXITCODE -ne 0) {
        throw "Git command failed with exit code $LASTEXITCODE: $Command"
    }

    return ($Output | Out-String).Trim()
}

Write-Host "=== Updating main project ===" -ForegroundColor Cyan
$CurrentBranch = Invoke-GitOutput branch --show-current

if ([string]::IsNullOrWhiteSpace($CurrentBranch)) {
    throw "Unable to determine the current branch. Make sure the repository is not in a detached HEAD state."
}

$Upstream = (git rev-parse --abbrev-ref --symbolic-full-name "@{u}" 2>$null | Out-String).Trim()

if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($Upstream)) {
    throw "Current branch '$CurrentBranch' does not have an upstream configured. Set it with: git branch --set-upstream-to=<remote>/<branch> $CurrentBranch"
}

Write-Host "Current branch: $CurrentBranch (upstream: $Upstream)"
Invoke-Git pull --rebase --autostash

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
