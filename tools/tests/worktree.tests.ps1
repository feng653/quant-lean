[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$script:Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$script:TestsDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
$script:SourceToolsDirectory = Split-Path -Parent $script:TestsDirectory
$script:Passed = 0

function Get-NormalizedPath {
    param([Parameter(Mandatory = $true)][string]$Path)
    return [System.IO.Path]::GetFullPath($Path).TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    )
}

function Assert-True {
    param(
        [Parameter(Mandatory = $true)][bool]$Condition,
        [Parameter(Mandatory = $true)][string]$Message
    )
    if (-not $Condition) {
        throw "ASSERTION FAILED: $Message"
    }
    $script:Passed++
}

function Invoke-NativeGit {
    param(
        [Parameter(Mandatory = $true)][string]$WorkingDirectory,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )
    $previousErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = @(& git -C $WorkingDirectory @Arguments 2>&1)
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    if ($exitCode -ne 0) {
        throw "git $($Arguments -join ' ') failed: $($output -join [Environment]::NewLine)"
    }
    return @($output | ForEach-Object { $_.ToString() })
}

function Invoke-ManagerProcess {
    param(
        [Parameter(Mandatory = $true)][string]$Repository,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][bool]$ShouldSucceed
    )
    $scriptPath = Join-Path $Repository "tools\worktree.ps1"
    $previousErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = @(& powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass `
            -File $scriptPath @Arguments 2>&1)
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    if ($ShouldSucceed -and $exitCode -ne 0) {
        throw "Manager unexpectedly failed ($exitCode): $($output -join [Environment]::NewLine)"
    }
    if (-not $ShouldSucceed -and $exitCode -eq 0) {
        throw "Manager unexpectedly succeeded: $($output -join [Environment]::NewLine)"
    }
    return [pscustomobject]@{
        ExitCode = $exitCode
        Output = @($output | ForEach-Object { $_.ToString() })
    }
}

function Get-Inventory {
    param([Parameter(Mandatory = $true)][string]$Repository)
    $result = Invoke-ManagerProcess -Repository $Repository `
        -Arguments @("inventory", "-Json") -ShouldSucceed $true
    return ($result.Output -join [Environment]::NewLine) | ConvertFrom-Json
}

$testRoot = Join-Path ([System.IO.Path]::GetTempPath()) (
    "pi-worktree-tests-{0}" -f [Guid]::NewGuid().ToString("N")
)
$repository = Join-Path $testRoot "repo"
$taskRoot = Join-Path $testRoot "pi-worktrees"

try {
    [System.IO.Directory]::CreateDirectory($repository) | Out-Null
    Invoke-NativeGit -WorkingDirectory $repository -Arguments @("init", "-b", "master") | Out-Null
    Invoke-NativeGit -WorkingDirectory $repository -Arguments @(
        "config", "user.name", "Worktree Test"
    ) | Out-Null
    Invoke-NativeGit -WorkingDirectory $repository -Arguments @(
        "config", "user.email", "worktree-test@example.invalid"
    ) | Out-Null

    [System.IO.Directory]::CreateDirectory((Join-Path $repository "tools")) | Out-Null
    Copy-Item -LiteralPath (Join-Path $script:SourceToolsDirectory "worktree.ps1") `
        -Destination (Join-Path $repository "tools\worktree.ps1")
    Copy-Item -LiteralPath (Join-Path $script:SourceToolsDirectory "worktree.config.json") `
        -Destination (Join-Path $repository "tools\worktree.config.json")
    [System.IO.File]::WriteAllText(
        (Join-Path $repository "base.txt"),
        "base`n",
        $script:Utf8NoBom
    )
    [System.IO.File]::WriteAllText(
        (Join-Path $repository ".gitignore"),
        "ignored.local`n",
        $script:Utf8NoBom
    )
    Invoke-NativeGit -WorkingDirectory $repository -Arguments @("add", ".") | Out-Null
    Invoke-NativeGit -WorkingDirectory $repository -Arguments @(
        "commit", "-m", "test: initialize repository"
    ) | Out-Null

    $stateDirectory = Join-Path $repository ".git\codex-worktrees"
    $headBeforeStatus = (Invoke-NativeGit -WorkingDirectory $repository -Arguments @(
        "rev-parse", "HEAD"
    ))[0]
    $inventory = Get-Inventory -Repository $repository
    $headAfterStatus = (Invoke-NativeGit -WorkingDirectory $repository -Arguments @(
        "rev-parse", "HEAD"
    ))[0]
    Assert-True ($inventory.worktreeCount -eq 1) "Initial inventory should contain only the primary worktree."
    Assert-True ($headBeforeStatus -eq $headAfterStatus) "Read-only inventory must not change HEAD."
    Assert-True (-not (Test-Path -LiteralPath $stateDirectory)) "Read-only inventory must not create manager state."

    $dirtyMarker = Join-Path $repository "dirty.txt"
    [System.IO.File]::WriteAllText($dirtyMarker, "dirty`n", $script:Utf8NoBom)
    Invoke-ManagerProcess -Repository $repository `
        -Arguments @("new", "dirty-primary", "-Agent", "tester") `
        -ShouldSucceed $false | Out-Null
    Assert-True (-not (Test-Path -LiteralPath $taskRoot)) "A dirty primary tree must block task creation."
    Remove-Item -LiteralPath $dirtyMarker

    Invoke-ManagerProcess -Repository $repository `
        -Arguments @("new", "sample task", "-Agent", "senior-dev") `
        -ShouldSucceed $true | Out-Null
    $inventory = Get-Inventory -Repository $repository
    $managedRows = @($inventory.worktrees | Where-Object { $_.managed })
    Assert-True ($managedRows.Count -eq 1) "New must register exactly one managed worktree."
    $taskId = $managedRows[0].taskId
    $taskPath = $managedRows[0].path
    $taskBranch = $managedRows[0].branch
    Assert-True ($taskBranch -like "codex/sample-task-senior-dev-*") "Task branch must follow the naming convention."
    Assert-True (Test-Path -LiteralPath $taskPath -PathType Container) "Task worktree must exist."

    Invoke-ManagerProcess -Repository $repository `
        -Arguments @("new", "sample task", "-Agent", "other-agent") `
        -ShouldSucceed $false | Out-Null
    Assert-True ((Get-Inventory -Repository $repository).worktreeCount -eq 2) "Duplicate task creation must not add a worktree."

    $taskFile = Join-Path $taskPath "feature.txt"
    [System.IO.File]::WriteAllText($taskFile, "feature`n", $script:Utf8NoBom)
    Invoke-NativeGit -WorkingDirectory $taskPath -Arguments @("add", "feature.txt") | Out-Null
    Invoke-NativeGit -WorkingDirectory $taskPath -Arguments @(
        "commit", "-m", "feat: add sample feature"
    ) | Out-Null
    Invoke-ManagerProcess -Repository $repository `
        -Arguments @(
            "handoff", $taskId,
            "-Agent", "senior-dev",
            "-ValidationSummary", "self-contained test passed"
        ) -ShouldSucceed $true | Out-Null
    Invoke-ManagerProcess -Repository $repository `
        -Arguments @("integrate", $taskId) -ShouldSucceed $true | Out-Null

    Assert-True (Test-Path -LiteralPath (Join-Path $repository "feature.txt")) "Integration must add the task result to master."
    $integratedInventory = Get-Inventory -Repository $repository
    $integratedRow = @($integratedInventory.worktrees | Where-Object {
        $_.taskId -eq $taskId
    })[0]
    Assert-True ($integratedRow.classification -eq "clean_integrated") "Integrated patch must be detected by patch equivalence."

    $ignoredSentinel = Join-Path $taskPath "ignored.local"
    [System.IO.File]::WriteAllText($ignoredSentinel, "preserve me`n", $script:Utf8NoBom)
    Invoke-ManagerProcess -Repository $repository `
        -Arguments @("retire", $taskId) -ShouldSucceed $false | Out-Null
    Assert-True (Test-Path -LiteralPath $ignoredSentinel -PathType Leaf) "Retire must preserve ignored files."
    Assert-True (Test-Path -LiteralPath $taskPath -PathType Container) "Retire must preserve a worktree containing ignored files."
    Remove-Item -LiteralPath $ignoredSentinel
    Invoke-ManagerProcess -Repository $repository `
        -Arguments @("retire", $taskId) -ShouldSucceed $true | Out-Null
    Assert-True (-not (Test-Path -LiteralPath $taskPath)) "Retire must remove the clean integrated worktree."
    $branchExists = @(& git -C $repository show-ref --verify --quiet "refs/heads/$taskBranch" 2>&1)
    Assert-True ($LASTEXITCODE -eq 0) "Retire must retain the task branch."

    Invoke-ManagerProcess -Repository $repository `
        -Arguments @("leader", "-Agent", "codex", "-Session", "session-one") `
        -ShouldSucceed $true | Out-Null
    Invoke-ManagerProcess -Repository $repository `
        -Arguments @("leader", "-Agent", "opencode", "-Session", "session-two") `
        -ShouldSucceed $false | Out-Null
    Assert-True ($true) "An unexpired leader lease must reject another session."

    $legacyPath = Join-Path $testRoot "存量 existing"
    Invoke-NativeGit -WorkingDirectory $repository -Arguments @(
        "worktree", "add", $legacyPath, "-b", "legacy/existing", "master"
    ) | Out-Null
    $legacyHeadBefore = (Invoke-NativeGit -WorkingDirectory $legacyPath -Arguments @(
        "rev-parse", "HEAD"
    ))[0]
    Invoke-ManagerProcess -Repository $repository `
        -Arguments @(
            "attach", "legacy task",
            "-Agent", "legacy-agent",
            "-WorktreePath", $legacyPath
        ) -ShouldSucceed $true | Out-Null
    $legacyHeadAfter = (Invoke-NativeGit -WorkingDirectory $legacyPath -Arguments @(
        "rev-parse", "HEAD"
    ))[0]
    $legacyStatus = @(Invoke-NativeGit -WorkingDirectory $legacyPath -Arguments @(
        "status", "--porcelain"
    ))
    Assert-True ($legacyHeadBefore -eq $legacyHeadAfter) "Attach must not move the legacy worktree HEAD."
    Assert-True ($legacyStatus.Count -eq 0) "Attach must not modify legacy worktree files."

    Invoke-ManagerProcess -Repository $repository `
        -Arguments @("new", "conflict task", "-Agent", "senior-dev") `
        -ShouldSucceed $true | Out-Null
    $conflictInventory = Get-Inventory -Repository $repository
    $conflictRow = @($conflictInventory.worktrees | Where-Object {
        $_.taskId -like "conflict-task-senior-dev-*"
    })[0]
    [System.IO.File]::WriteAllText(
        (Join-Path $conflictRow.path "base.txt"),
        "task version`n",
        $script:Utf8NoBom
    )
    Invoke-NativeGit -WorkingDirectory $conflictRow.path -Arguments @("add", "base.txt") | Out-Null
    Invoke-NativeGit -WorkingDirectory $conflictRow.path -Arguments @(
        "commit", "-m", "feat: conflicting task edit"
    ) | Out-Null
    Invoke-ManagerProcess -Repository $repository `
        -Arguments @(
            "handoff", $conflictRow.taskId,
            "-Agent", "senior-dev",
            "-ValidationSummary", "conflict fixture"
        ) -ShouldSucceed $true | Out-Null

    [System.IO.File]::WriteAllText(
        (Join-Path $repository "base.txt"),
        "master version`n",
        $script:Utf8NoBom
    )
    Invoke-NativeGit -WorkingDirectory $repository -Arguments @("add", "base.txt") | Out-Null
    Invoke-NativeGit -WorkingDirectory $repository -Arguments @(
        "commit", "-m", "test: conflicting master edit"
    ) | Out-Null
    $masterBeforeConflict = (Invoke-NativeGit -WorkingDirectory $repository -Arguments @(
        "rev-parse", "HEAD"
    ))[0]
    Invoke-ManagerProcess -Repository $repository `
        -Arguments @("integrate", $conflictRow.taskId) -ShouldSucceed $false | Out-Null
    $masterAfterConflict = (Invoke-NativeGit -WorkingDirectory $repository -Arguments @(
        "rev-parse", "HEAD"
    ))[0]
    $masterStatus = @(Invoke-NativeGit -WorkingDirectory $repository -Arguments @(
        "status", "--porcelain"
    ))
    Assert-True ($masterBeforeConflict -eq $masterAfterConflict) "Failed preflight must not move master."
    Assert-True ($masterStatus.Count -eq 0) "Failed preflight must leave the primary worktree clean."

    $concurrentOutOne = Join-Path $testRoot "concurrent-one.out"
    $concurrentErrOne = Join-Path $testRoot "concurrent-one.err"
    $concurrentOutTwo = Join-Path $testRoot "concurrent-two.out"
    $concurrentErrTwo = Join-Path $testRoot "concurrent-two.err"
    $managerPath = Join-Path $repository "tools\worktree.ps1"
    $concurrentArguments = @(
        "-NoLogo",
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", $managerPath,
        "new", "concurrent-task",
        "-Agent", "tester"
    )
    $processOne = Start-Process -FilePath "powershell.exe" `
        -ArgumentList $concurrentArguments `
        -WindowStyle Hidden `
        -RedirectStandardOutput $concurrentOutOne `
        -RedirectStandardError $concurrentErrOne `
        -PassThru
    $processTwo = Start-Process -FilePath "powershell.exe" `
        -ArgumentList $concurrentArguments `
        -WindowStyle Hidden `
        -RedirectStandardOutput $concurrentOutTwo `
        -RedirectStandardError $concurrentErrTwo `
        -PassThru
    $processOne.WaitForExit(30000) | Out-Null
    $processTwo.WaitForExit(30000) | Out-Null
    if (-not $processOne.HasExited -or -not $processTwo.HasExited) {
        throw "Concurrent manager test timed out."
    }
    $processOne.Refresh()
    $processTwo.Refresh()
    $successfulConcurrentProcesses = @(@(
        $concurrentErrOne,
        $concurrentErrTwo
    ) | Where-Object { (Get-Item -LiteralPath $_).Length -eq 0 })
    $concurrentDiagnostics = "err1=$(
        (Get-Content -LiteralPath $concurrentErrOne -Raw -ErrorAction SilentlyContinue)
    ), err2=$(
        (Get-Content -LiteralPath $concurrentErrTwo -Raw -ErrorAction SilentlyContinue)
    )"
    Assert-True ($successfulConcurrentProcesses.Count -eq 1) (
        "Concurrent task claims must have exactly one winner. $concurrentDiagnostics"
    )
    $concurrentRows = @((Get-Inventory -Repository $repository).worktrees | Where-Object {
        $_.taskId -like "concurrent-task-tester-*"
    })
    Assert-True ($concurrentRows.Count -eq 1) "Concurrent claims must create exactly one worktree."

    $configurationPath = Join-Path $repository "tools\worktree.config.json"
    $strictConfiguration = Get-Content -LiteralPath $configurationPath -Raw -Encoding UTF8 |
        ConvertFrom-Json
    $strictConfiguration.enforcementMode = "strict"
    [System.IO.File]::WriteAllText(
        $configurationPath,
        ($strictConfiguration | ConvertTo-Json -Depth 5) + [Environment]::NewLine,
        $script:Utf8NoBom
    )
    Invoke-NativeGit -WorkingDirectory $repository -Arguments @(
        "add", "tools/worktree.config.json"
    ) | Out-Null
    Invoke-NativeGit -WorkingDirectory $repository -Arguments @(
        "commit", "-m", "test: enable strict worktree mode"
    ) | Out-Null
    Invoke-ManagerProcess -Repository $repository `
        -Arguments @("new", "strict-blocked", "-Agent", "tester") `
        -ShouldSucceed $false | Out-Null
    Invoke-ManagerProcess -Repository $repository `
        -Arguments @(
            "new", "strict-authorized",
            "-Agent", "tester",
            "-LeaderSession", "session-one"
        ) -ShouldSucceed $true | Out-Null
    $strictRows = @((Get-Inventory -Repository $repository).worktrees | Where-Object {
        $_.taskId -like "strict-authorized-tester-*"
    })
    Assert-True ($strictRows.Count -eq 1) "Strict mode must allow the active leader session."

    Write-Output "PASS: $script:Passed assertions"
} finally {
    if (Test-Path -LiteralPath $testRoot) {
        $resolvedTestRoot = Get-NormalizedPath $testRoot
        $resolvedTempRoot = Get-NormalizedPath ([System.IO.Path]::GetTempPath())
        if (-not $resolvedTestRoot.StartsWith(
            $resolvedTempRoot,
            [System.StringComparison]::OrdinalIgnoreCase
        )) {
            throw "Refusing to remove test path outside the temporary directory: $resolvedTestRoot"
        }
        Remove-Item -LiteralPath $resolvedTestRoot -Recurse -Force
    }
}
