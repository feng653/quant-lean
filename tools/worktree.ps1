[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet("new", "attach", "status", "inventory", "reconcile", "inspect", "handoff", "integrate", "retire", "leader")]
    [string]$Command,

    [Parameter(Position = 1)]
    [string]$Task,

    [string]$Agent,
    [string]$Session,
    [string]$LeaderSession,
    [string]$WorktreePath,
    [string]$ValidationSummary,
    [switch]$Json
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$script:Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$script:ScriptDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
$script:ConfigPath = Join-Path $script:ScriptDirectory "worktree.config.json"

function Invoke-Git {
    param(
        [Parameter(Mandatory = $true)][string]$WorkingDirectory,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [switch]$ReadOnly,
        [switch]$AllowFailure
    )

    $gitArguments = @()
    if ($ReadOnly) {
        $gitArguments += "--no-optional-locks"
    }
    $gitArguments += @("-C", $WorkingDirectory)
    $gitArguments += $Arguments

    $previousErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = @(& git @gitArguments 2>&1)
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    if ($exitCode -ne 0 -and -not $AllowFailure) {
        $message = ($output | ForEach-Object { $_.ToString() }) -join [Environment]::NewLine
        throw "git $($Arguments -join ' ') failed in '$WorkingDirectory' (exit $exitCode). $message"
    }

    [pscustomobject]@{
        ExitCode = $exitCode
        Lines = @($output | ForEach-Object { $_.ToString() })
    }
}

function Get-NormalizedPath {
    param([Parameter(Mandatory = $true)][string]$Path)
    return [System.IO.Path]::GetFullPath($Path).TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    )
}

function Test-SamePath {
    param(
        [Parameter(Mandatory = $true)][string]$Left,
        [Parameter(Mandatory = $true)][string]$Right
    )
    return [string]::Equals(
        (Get-NormalizedPath $Left),
        (Get-NormalizedPath $Right),
        [System.StringComparison]::OrdinalIgnoreCase
    )
}

function Get-IsoTimestamp {
    return [DateTimeOffset]::Now.ToString("o")
}

function Convert-ToSlug {
    param(
        [Parameter(Mandatory = $true)][string]$Value,
        [Parameter(Mandatory = $true)][string]$Fallback
    )

    $slug = $Value.ToLowerInvariant()
    $slug = [regex]::Replace($slug, "[^a-z0-9]+", "-")
    $slug = $slug.Trim("-")
    if ([string]::IsNullOrWhiteSpace($slug)) {
        return $Fallback
    }
    if ($slug.Length -gt 40) {
        return $slug.Substring(0, 40).TrimEnd("-")
    }
    return $slug
}

function Get-Configuration {
    if (-not (Test-Path -LiteralPath $script:ConfigPath -PathType Leaf)) {
        throw "Configuration not found: $script:ConfigPath"
    }
    return Get-Content -LiteralPath $script:ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
}

function Get-RepositoryContext {
    $repoResult = Invoke-Git -WorkingDirectory $script:ScriptDirectory -Arguments @(
        "rev-parse", "--show-toplevel"
    ) -ReadOnly
    $repoRoot = Get-NormalizedPath $repoResult.Lines[0]

    $commonResult = Invoke-Git -WorkingDirectory $repoRoot -Arguments @(
        "rev-parse", "--git-common-dir"
    ) -ReadOnly
    $commonValue = $commonResult.Lines[0]
    if ([System.IO.Path]::IsPathRooted($commonValue)) {
        $commonDirectory = Get-NormalizedPath $commonValue
    } else {
        $commonDirectory = Get-NormalizedPath (Join-Path $repoRoot $commonValue)
    }

    [pscustomobject]@{
        RepositoryRoot = $repoRoot
        CommonGitDirectory = $commonDirectory
        StateDirectory = Join-Path $commonDirectory "codex-worktrees"
        ManifestPath = Join-Path $commonDirectory "codex-worktrees\manifest.json"
        EventsPath = Join-Path $commonDirectory "codex-worktrees\events.jsonl"
        LockPath = Join-Path $commonDirectory "codex-worktrees\manager.lock"
    }
}

function Get-WorktreeRecords {
    param([Parameter(Mandatory = $true)]$Context)

    $result = Invoke-Git -WorkingDirectory $Context.RepositoryRoot -Arguments @(
        "-c", "core.quotePath=false", "worktree", "list", "--porcelain"
    ) -ReadOnly

    $records = @()
    $current = $null
    foreach ($line in $result.Lines) {
        if ($line -like "worktree *") {
            if ($null -ne $current) {
                $records += [pscustomobject]$current
            }
            $current = [ordered]@{
                Path = $line.Substring(9)
                Head = ""
                Branch = ""
                IsBare = $false
                IsDetached = $false
                IsLocked = $false
                IsPrunable = $false
            }
        } elseif ($null -ne $current -and $line -like "HEAD *") {
            $current.Head = $line.Substring(5)
        } elseif ($null -ne $current -and $line -like "branch *") {
            $current.Branch = $line.Substring(7) -replace "^refs/heads/", ""
        } elseif ($null -ne $current -and $line -eq "bare") {
            $current.IsBare = $true
        } elseif ($null -ne $current -and $line -eq "detached") {
            $current.IsDetached = $true
        } elseif ($null -ne $current -and $line -like "locked*") {
            $current.IsLocked = $true
        } elseif ($null -ne $current -and $line -like "prunable*") {
            $current.IsPrunable = $true
        }
    }
    if ($null -ne $current) {
        $records += [pscustomobject]$current
    }
    return @($records)
}

function Get-PrimaryWorktree {
    param(
        [Parameter(Mandatory = $true)]$Context,
        [Parameter(Mandatory = $true)]$Config
    )
    $matches = @(Get-WorktreeRecords -Context $Context | Where-Object {
        $_.Branch -eq $Config.primaryBranch
    })
    if ($matches.Count -ne 1) {
        throw "Expected exactly one worktree for '$($Config.primaryBranch)', found $($matches.Count)."
    }
    return $matches[0]
}

function Get-EmptyManifest {
    param([Parameter(Mandatory = $true)]$Context)
    return [pscustomobject]@{
        schemaVersion = 1
        repositoryRoot = $Context.PrimaryWorktreeRoot
        createdAt = Get-IsoTimestamp
        updatedAt = Get-IsoTimestamp
        leader = $null
        tasks = @()
        integrations = @()
    }
}

function Read-Manifest {
    param([Parameter(Mandatory = $true)]$Context)
    if (-not (Test-Path -LiteralPath $Context.ManifestPath -PathType Leaf)) {
        return Get-EmptyManifest -Context $Context
    }
    $manifest = Get-Content -LiteralPath $Context.ManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($manifest.schemaVersion -ne 1) {
        throw "Unsupported manifest schema version: $($manifest.schemaVersion)"
    }
    return $manifest
}

function Write-Manifest {
    param(
        [Parameter(Mandatory = $true)]$Context,
        [Parameter(Mandatory = $true)]$Manifest
    )
    [System.IO.Directory]::CreateDirectory($Context.StateDirectory) | Out-Null
    $Manifest.updatedAt = Get-IsoTimestamp
    $temporaryPath = Join-Path $Context.StateDirectory (
        "manifest.{0}.tmp" -f [Guid]::NewGuid().ToString("N")
    )
    $content = $Manifest | ConvertTo-Json -Depth 12
    [System.IO.File]::WriteAllText($temporaryPath, $content + [Environment]::NewLine, $script:Utf8NoBom)
    Move-Item -LiteralPath $temporaryPath -Destination $Context.ManifestPath -Force
}

function Add-Event {
    param(
        [Parameter(Mandatory = $true)]$Context,
        [Parameter(Mandatory = $true)][string]$Type,
        [Parameter(Mandatory = $true)]$Data
    )
    [System.IO.Directory]::CreateDirectory($Context.StateDirectory) | Out-Null
    $event = [ordered]@{
        at = Get-IsoTimestamp
        type = $Type
        data = $Data
    }
    $line = $event | ConvertTo-Json -Depth 10 -Compress
    [System.IO.File]::AppendAllText(
        $Context.EventsPath,
        $line + [Environment]::NewLine,
        $script:Utf8NoBom
    )
}

function Add-EventSafely {
    param(
        [Parameter(Mandatory = $true)]$Context,
        [Parameter(Mandatory = $true)][string]$Type,
        [Parameter(Mandatory = $true)]$Data
    )
    try {
        Add-Event -Context $Context -Type $Type -Data $Data
    } catch {
        Write-Warning "State was finalized, but the audit event could not be appended: $($_.Exception.Message)"
    }
}

function New-TransactionIntent {
    param(
        [Parameter(Mandatory = $true)]$Context,
        [Parameter(Mandatory = $true)][string]$Operation,
        [Parameter(Mandatory = $true)]$Data
    )
    $transactionDirectory = Join-Path $Context.StateDirectory "transactions"
    [System.IO.Directory]::CreateDirectory($transactionDirectory) | Out-Null
    $transactionId = [Guid]::NewGuid().ToString("N")
    $path = Join-Path $transactionDirectory "$transactionId.json"
    $intent = [ordered]@{
        schemaVersion = 1
        id = $transactionId
        operation = $Operation
        createdAt = Get-IsoTimestamp
        data = $Data
    }
    [System.IO.File]::WriteAllText(
        $path,
        ($intent | ConvertTo-Json -Depth 12) + [Environment]::NewLine,
        $script:Utf8NoBom
    )
    return $path
}

function Complete-TransactionIntent {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (Test-Path -LiteralPath $Path -PathType Leaf) {
        [System.IO.File]::Delete($Path)
    }
}

function Get-PendingTransactionRecords {
    param([Parameter(Mandatory = $true)]$Context)
    $transactionDirectory = Join-Path $Context.StateDirectory "transactions"
    if (-not (Test-Path -LiteralPath $transactionDirectory -PathType Container)) {
        return @()
    }
    $records = @()
    foreach ($file in Get-ChildItem -LiteralPath $transactionDirectory -Filter "*.json" -File) {
        try {
            $record = Get-Content -LiteralPath $file.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
            $records += [pscustomobject]@{
                id = $record.id
                operation = $record.operation
                createdAt = $record.createdAt
                path = $file.FullName
                data = $record.data
                parseError = $null
            }
        } catch {
            $records += [pscustomobject]@{
                id = $file.BaseName
                operation = "unknown"
                createdAt = $file.CreationTime.ToString("o")
                path = $file.FullName
                data = $null
                parseError = $_.Exception.Message
            }
        }
    }
    return @($records)
}

function Assert-LeaderAuthorization {
    param(
        [Parameter(Mandatory = $true)]$Config,
        [Parameter(Mandatory = $true)]$Manifest,
        [string]$SessionId
    )
    if ($Config.enforcementMode -ne "strict") {
        return
    }
    if ([string]::IsNullOrWhiteSpace($SessionId)) {
        throw "Strict mode requires -LeaderSession for this lifecycle mutation."
    }
    if ($null -eq $Manifest.leader) {
        throw "Strict mode requires an active leader lease."
    }
    $expiresAt = [DateTimeOffset]::Parse($Manifest.leader.expiresAt)
    if ($expiresAt -le [DateTimeOffset]::Now) {
        throw "Leader lease expired at $expiresAt."
    }
    if ($Manifest.leader.session -ne $SessionId) {
        throw "Leader session mismatch. This mutation is not authorized."
    }
}

function Use-ManagerLock {
    param(
        [Parameter(Mandatory = $true)]$Context,
        [Parameter(Mandatory = $true)][scriptblock]$Action
    )
    [System.IO.Directory]::CreateDirectory($Context.StateDirectory) | Out-Null
    $stream = $null
    try {
        $stream = New-Object System.IO.FileStream(
            $Context.LockPath,
            [System.IO.FileMode]::OpenOrCreate,
            [System.IO.FileAccess]::ReadWrite,
            [System.IO.FileShare]::None
        )
    } catch [System.IO.IOException] {
        throw "Another worktree-manager mutation is in progress. No state was changed."
    }
    try {
        & $Action
    } finally {
        if ($null -ne $stream) {
            $stream.Dispose()
        }
    }
}

function Get-TaskRecord {
    param(
        [Parameter(Mandatory = $true)]$Manifest,
        [Parameter(Mandatory = $true)][string]$Identifier
    )
    $matches = @($Manifest.tasks | Where-Object {
        $_.id -eq $Identifier -or
        $_.task -eq $Identifier -or
        $_.branch -eq $Identifier -or
        (Split-Path -Leaf $_.worktreePath) -eq $Identifier
    })
    if ($matches.Count -eq 0) {
        throw "Task not found in the local manifest: $Identifier"
    }
    if ($matches.Count -gt 1) {
        $ids = ($matches | ForEach-Object { $_.id }) -join ", "
        throw "Task identifier is ambiguous. Use one of: $ids"
    }
    return $matches[0]
}

function Assert-CleanWorktree {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Purpose
    )
    $status = Invoke-Git -WorkingDirectory $Path -Arguments @(
        "status", "--porcelain", "--untracked-files=normal"
    ) -ReadOnly
    if ($status.Lines.Count -gt 0) {
        throw "$Purpose requires a clean worktree. Uncommitted changes in '$Path' were preserved."
    }
}

function Assert-ExpectedBranch {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$ExpectedBranch,
        [Parameter(Mandatory = $true)][string]$Purpose
    )
    $branchResult = Invoke-Git -WorkingDirectory $Path -Arguments @(
        "branch", "--show-current"
    ) -ReadOnly
    $currentBranch = if ($branchResult.Lines.Count -gt 0) { $branchResult.Lines[0] } else { "" }
    if ($currentBranch -ne $ExpectedBranch) {
        throw "$Purpose requires branch '$ExpectedBranch', found '$currentBranch'."
    }
}

function Assert-LinearTaskHistory {
    param(
        [Parameter(Mandatory = $true)]$Record,
        [Parameter(Mandatory = $true)][string]$Purpose
    )
    $ancestor = Invoke-Git -WorkingDirectory $Record.worktreePath -Arguments @(
        "merge-base", "--is-ancestor", $Record.baseCommit, "HEAD"
    ) -ReadOnly -AllowFailure
    if ($ancestor.ExitCode -ne 0) {
        throw "$Purpose rejected task history because base commit '$($Record.baseCommit)' is not an ancestor of HEAD."
    }
    $merges = Invoke-Git -WorkingDirectory $Record.worktreePath -Arguments @(
        "rev-list", "--merges", "$($Record.baseCommit)..HEAD"
    ) -ReadOnly
    if ($merges.Lines.Count -gt 0) {
        throw "$Purpose requires linear task history. Merge commits were found after the task base."
    }
}

function Get-ObservedStatus {
    param(
        [Parameter(Mandatory = $true)]$Context,
        [Parameter(Mandatory = $true)]$Config,
        [Parameter(Mandatory = $true)]$Manifest
    )
    $primary = Get-PrimaryWorktree -Context $Context -Config $Config
    $rows = @()
    foreach ($worktree in (Get-WorktreeRecords -Context $Context)) {
        $managedMatches = @($Manifest.tasks | Where-Object {
            Test-SamePath $_.worktreePath $worktree.Path
        })
        $managed = $managedMatches.Count -eq 1
        $taskRecord = if ($managed) { $managedMatches[0] } else { $null }

        if (-not (Test-Path -LiteralPath $worktree.Path -PathType Container)) {
            $rows += [pscustomobject]@{
                classification = "missing"
                managed = $managed
                taskId = if ($managed) { $taskRecord.id } else { $null }
                owner = if ($managed) { $taskRecord.agent } else { "unknown" }
                dirtyFiles = -1
                pendingPatches = -1
                integratedPatches = -1
                branch = $worktree.Branch
                head = $worktree.Head
                path = $worktree.Path
                lastCommitAt = $null
            }
            continue
        }

        $status = Invoke-Git -WorkingDirectory $worktree.Path -Arguments @(
            "status", "--porcelain", "--untracked-files=normal"
        ) -ReadOnly -AllowFailure
        $dirtyCount = if ($status.ExitCode -eq 0) { $status.Lines.Count } else { -1 }
        $plus = 0
        $minus = 0
        if (-not (Test-SamePath $worktree.Path $primary.Path)) {
            $cherry = Invoke-Git -WorkingDirectory $worktree.Path -Arguments @(
                "cherry", $Config.primaryBranch, "HEAD"
            ) -ReadOnly -AllowFailure
            if ($cherry.ExitCode -eq 0) {
                $plus = @($cherry.Lines | Where-Object { $_ -like "+ *" }).Count
                $minus = @($cherry.Lines | Where-Object { $_ -like "- *" }).Count
            } else {
                $plus = -1
                $minus = -1
            }
        }

        $classification = if (Test-SamePath $worktree.Path $primary.Path) {
            "primary"
        } elseif ($dirtyCount -gt 0) {
            "dirty"
        } elseif ($plus -gt 0) {
            "clean_pending"
        } elseif ($plus -eq 0) {
            "clean_integrated"
        } else {
            "unknown"
        }

        $lastCommit = Invoke-Git -WorkingDirectory $worktree.Path -Arguments @(
            "log", "-1", "--format=%cI"
        ) -ReadOnly -AllowFailure

        $rows += [pscustomobject]@{
            classification = $classification
            managed = $managed
            taskId = if ($managed) { $taskRecord.id } else { $null }
            owner = if ($managed) { $taskRecord.agent } else { "unknown" }
            dirtyFiles = $dirtyCount
            pendingPatches = $plus
            integratedPatches = $minus
            branch = $worktree.Branch
            head = $worktree.Head
            path = $worktree.Path
            lastCommitAt = if ($lastCommit.Lines.Count -gt 0) { $lastCommit.Lines[0] } else { $null }
        }
    }
    return @($rows)
}

function Show-Status {
    param(
        [Parameter(Mandatory = $true)]$Context,
        [Parameter(Mandatory = $true)]$Config,
        [Parameter(Mandatory = $true)]$Manifest,
        [switch]$AsJson,
        [switch]$Inventory
    )
    $rows = @(Get-ObservedStatus -Context $Context -Config $Config -Manifest $Manifest)
    $transactions = @(Get-PendingTransactionRecords -Context $Context)
    $result = [pscustomobject]@{
        observedAt = Get-IsoTimestamp
        repositoryRoot = $Context.PrimaryWorktreeRoot
        primaryBranch = $Config.primaryBranch
        enforcementMode = $Config.enforcementMode
        worktreeCount = $rows.Count
        pendingTransactionCount = $transactions.Count
        summary = @($rows | Group-Object classification | Sort-Object Name | ForEach-Object {
            [pscustomobject]@{ classification = $_.Name; count = $_.Count }
        })
        worktrees = $rows
        pendingTransactions = $transactions
    }
    if ($AsJson) {
        $result | ConvertTo-Json -Depth 10
        return
    }

    Write-Output "Repository: $($result.repositoryRoot)"
    Write-Output "Primary:    $($result.primaryBranch)"
    Write-Output "Mode:       $($result.enforcementMode)"
    Write-Output "Observed:   $($result.observedAt)"
    Write-Output "Recovery:   $($result.pendingTransactionCount) pending transaction(s)"
    Write-Output ""
    $result.summary | Format-Table -AutoSize
    if ($Inventory) {
        $rows | Sort-Object classification, path | Format-Table `
            classification, managed, owner, dirtyFiles, pendingPatches, branch, path -AutoSize
    } else {
        $rows | Where-Object {
            $_.classification -in @("dirty", "clean_pending", "missing", "unknown")
        } | Sort-Object classification, path | Format-Table `
            classification, managed, owner, dirtyFiles, pendingPatches, branch, path -AutoSize
    }
}

function New-TaskWorktree {
    param(
        [Parameter(Mandatory = $true)]$Context,
        [Parameter(Mandatory = $true)]$Config,
        [Parameter(Mandatory = $true)][string]$TaskName,
        [Parameter(Mandatory = $true)][string]$AgentName,
        [string]$LeaderSessionId
    )
    Use-ManagerLock -Context $Context -Action {
        $manifest = Read-Manifest -Context $Context
        Assert-LeaderAuthorization -Config $Config -Manifest $manifest -SessionId $LeaderSessionId
        $primary = Get-PrimaryWorktree -Context $Context -Config $Config
        Assert-CleanWorktree -Path $primary.Path -Purpose "Creating a task"
        Assert-ExpectedBranch -Path $primary.Path -ExpectedBranch $Config.primaryBranch `
            -Purpose "Creating a task"

        $existingActive = @($manifest.tasks | Where-Object {
            $_.task -eq $TaskName -and $_.status -notin @("retired", "cancelled")
        })
        if ($existingActive.Count -gt 0) {
            $ids = ($existingActive | ForEach-Object { $_.id }) -join ", "
            throw "An active task with this name already exists: $ids"
        }

        $shortId = [Guid]::NewGuid().ToString("N").Substring(0, 6)
        $taskSlug = Convert-ToSlug -Value $TaskName -Fallback "task"
        $agentSlug = Convert-ToSlug -Value $AgentName -Fallback "agent"
        $taskId = "$taskSlug-$agentSlug-$shortId"
        $branch = "$($Config.branchPrefix)/$taskId"

        $configuredRoot = $Config.worktreeRoot
        if ([System.IO.Path]::IsPathRooted($configuredRoot)) {
            $worktreeRoot = Get-NormalizedPath $configuredRoot
        } else {
            $worktreeRoot = Get-NormalizedPath (Join-Path $primary.Path $configuredRoot)
        }
        $worktreePath = Join-Path $worktreeRoot $taskId

        if (Test-Path -LiteralPath $worktreePath) {
            throw "Target worktree path already exists: $worktreePath"
        }
        $branchCheck = Invoke-Git -WorkingDirectory $primary.Path -Arguments @(
            "show-ref", "--verify", "--quiet", "refs/heads/$branch"
        ) -ReadOnly -AllowFailure
        if ($branchCheck.ExitCode -eq 0) {
            throw "Target branch already exists: $branch"
        }

        [System.IO.Directory]::CreateDirectory($worktreeRoot) | Out-Null
        $baseCommit = (Invoke-Git -WorkingDirectory $primary.Path -Arguments @(
            "rev-parse", $Config.primaryBranch
        ) -ReadOnly).Lines[0]

        $record = [pscustomobject]@{
            id = $taskId
            task = $TaskName
            agent = $AgentName
            status = "active"
            worktreePath = $worktreePath
            branch = $branch
            baseBranch = $Config.primaryBranch
            baseCommit = $baseCommit
            createdAt = Get-IsoTimestamp
            lastActivityAt = Get-IsoTimestamp
            handoffHead = $null
            validationSummary = $null
            validationAt = $null
            integratedAt = $null
            retiredAt = $null
        }
        $intentPath = New-TransactionIntent -Context $Context -Operation "new" -Data ([ordered]@{
            task = $record
            primaryHead = $baseCommit
        })
        Invoke-Git -WorkingDirectory $primary.Path -Arguments @(
            "worktree", "add", $worktreePath, "-b", $branch, $baseCommit
        ) | Out-Null
        $manifest.tasks = @($manifest.tasks) + @($record)
        try {
            Write-Manifest -Context $Context -Manifest $manifest
        } catch {
            throw "Git created '$worktreePath', but manifest finalization failed. Preserve the worktree and run reconcile. Transaction: $intentPath. $($_.Exception.Message)"
        }
        Add-EventSafely -Context $Context -Type "task.created" -Data $record
        Complete-TransactionIntent -Path $intentPath

        [pscustomobject]@{
            taskId = $taskId
            task = $TaskName
            agent = $AgentName
            branch = $branch
            baseCommit = $baseCommit
            worktreePath = $worktreePath
        } | Format-List
    }
}

function Add-LegacyWorktree {
    param(
        [Parameter(Mandatory = $true)]$Context,
        [Parameter(Mandatory = $true)]$Config,
        [Parameter(Mandatory = $true)][string]$TaskName,
        [Parameter(Mandatory = $true)][string]$AgentName,
        [Parameter(Mandatory = $true)][string]$Path,
        [string]$LeaderSessionId
    )
    Use-ManagerLock -Context $Context -Action {
        $manifest = Read-Manifest -Context $Context
        Assert-LeaderAuthorization -Config $Config -Manifest $manifest -SessionId $LeaderSessionId
        $primary = Get-PrimaryWorktree -Context $Context -Config $Config
        if (-not [System.IO.Path]::IsPathRooted($Path)) {
            throw "attach requires an absolute worktree path."
        }
        $normalizedPath = Get-NormalizedPath $Path
        if (Test-SamePath $normalizedPath $primary.Path) {
            throw "The primary worktree cannot be attached as a task."
        }
        $worktreeMatches = @(Get-WorktreeRecords -Context $Context | Where-Object {
            Test-SamePath $_.Path $normalizedPath
        })
        if ($worktreeMatches.Count -ne 1) {
            throw "Path must match exactly one registered Git worktree: $normalizedPath"
        }
        $worktree = $worktreeMatches[0]
        if ([string]::IsNullOrWhiteSpace($worktree.Branch)) {
            throw "Detached or branchless legacy worktrees cannot be attached automatically."
        }
        $alreadyManaged = @($manifest.tasks | Where-Object {
            (Test-SamePath $_.worktreePath $normalizedPath) -or
            $_.branch -eq $worktree.Branch
        })
        if ($alreadyManaged.Count -gt 0) {
            throw "Worktree or branch is already managed as task '$($alreadyManaged[0].id)'."
        }
        $existingTask = @($manifest.tasks | Where-Object {
            $_.task -eq $TaskName -and $_.status -notin @("retired", "cancelled")
        })
        if ($existingTask.Count -gt 0) {
            throw "An active task with this name already exists."
        }

        $shortId = [Guid]::NewGuid().ToString("N").Substring(0, 6)
        $taskSlug = Convert-ToSlug -Value $TaskName -Fallback "legacy"
        $agentSlug = Convert-ToSlug -Value $AgentName -Fallback "agent"
        $taskId = "$taskSlug-$agentSlug-$shortId"
        $mergeBase = (Invoke-Git -WorkingDirectory $normalizedPath -Arguments @(
            "merge-base", $Config.primaryBranch, "HEAD"
        ) -ReadOnly).Lines[0]
        $record = [pscustomobject]@{
            id = $taskId
            task = $TaskName
            agent = $AgentName
            status = "active"
            worktreePath = $normalizedPath
            branch = $worktree.Branch
            baseBranch = $Config.primaryBranch
            baseCommit = $mergeBase
            createdAt = Get-IsoTimestamp
            lastActivityAt = Get-IsoTimestamp
            handoffHead = $null
            validationSummary = $null
            validationAt = $null
            integratedAt = $null
            retiredAt = $null
        }
        $manifest.tasks = @($manifest.tasks) + @($record)
        Write-Manifest -Context $Context -Manifest $manifest
        Add-EventSafely -Context $Context -Type "task.attached" -Data $record
        Write-Output "Attached existing worktree without modifying its files, branch, or commits."
        $record | Format-List id, task, agent, branch, baseCommit, worktreePath
    }
}

function Show-TaskInspection {
    param(
        [Parameter(Mandatory = $true)]$Context,
        [Parameter(Mandatory = $true)]$Config,
        [Parameter(Mandatory = $true)]$Manifest,
        [Parameter(Mandatory = $true)][string]$Identifier
    )
    $record = Get-TaskRecord -Manifest $Manifest -Identifier $Identifier
    if (-not (Test-Path -LiteralPath $record.worktreePath -PathType Container)) {
        throw "Task worktree is missing: $($record.worktreePath)"
    }

    Write-Output "Task:       $($record.task)"
    Write-Output "Task ID:    $($record.id)"
    Write-Output "Agent:      $($record.agent)"
    Write-Output "Status:     $($record.status)"
    Write-Output "Branch:     $($record.branch)"
    Write-Output "Worktree:   $($record.worktreePath)"
    Write-Output "Base:       $($record.baseBranch) @ $($record.baseCommit)"
    Write-Output "Validation: $($record.validationSummary)"
    Write-Output ""
    Write-Output "WORKTREE STATUS"
    (Invoke-Git -WorkingDirectory $record.worktreePath -Arguments @(
        "status", "--short", "--branch"
    ) -ReadOnly).Lines
    Write-Output ""
    Write-Output "COMMITS NOT PATCH-EQUIVALENT TO $($Config.primaryBranch)"
    $cherry = Invoke-Git -WorkingDirectory $record.worktreePath -Arguments @(
        "cherry", "-v", $Config.primaryBranch, "HEAD"
    ) -ReadOnly -AllowFailure
    if ($cherry.Lines.Count -eq 0) {
        Write-Output "(none)"
    } else {
        $cherry.Lines
    }
    Write-Output ""
    Write-Output "DIFF STAT"
    $diff = Invoke-Git -WorkingDirectory $record.worktreePath -Arguments @(
        "diff", "--stat", "$($Config.primaryBranch)...HEAD"
    ) -ReadOnly -AllowFailure
    if ($diff.Lines.Count -eq 0) {
        Write-Output "(none)"
    } else {
        $diff.Lines
    }
}

function Set-TaskHandoff {
    param(
        [Parameter(Mandatory = $true)]$Context,
        [Parameter(Mandatory = $true)]$Config,
        [Parameter(Mandatory = $true)][string]$Identifier,
        [Parameter(Mandatory = $true)][string]$AgentName,
        [Parameter(Mandatory = $true)][string]$Summary
    )
    Use-ManagerLock -Context $Context -Action {
        $manifest = Read-Manifest -Context $Context
        $record = Get-TaskRecord -Manifest $manifest -Identifier $Identifier
        if ($record.agent -ne $AgentName) {
            throw "Task owner is '$($record.agent)', not '$AgentName'. Use an explicit ownership handoff."
        }
        if ($record.status -notin @("active", "ready")) {
            throw "Task status '$($record.status)' cannot be handed off."
        }
        Assert-CleanWorktree -Path $record.worktreePath -Purpose "Handoff"
        Assert-ExpectedBranch -Path $record.worktreePath -ExpectedBranch $record.branch `
            -Purpose "Handoff"
        Assert-LinearTaskHistory -Record $record -Purpose "Handoff"
        $head = (Invoke-Git -WorkingDirectory $record.worktreePath -Arguments @(
            "rev-parse", "HEAD"
        ) -ReadOnly).Lines[0]
        if ($head -eq $record.baseCommit) {
            throw "Task has no committed changes to hand off."
        }
        $record.status = "ready"
        $record.handoffHead = $head
        $record.validationSummary = $Summary
        $record.validationAt = Get-IsoTimestamp
        $record.lastActivityAt = Get-IsoTimestamp
        Write-Manifest -Context $Context -Manifest $manifest
        Add-EventSafely -Context $Context -Type "task.handoff" -Data ([ordered]@{
            taskId = $record.id
            agent = $AgentName
            head = $head
            validationSummary = $Summary
        })
        Write-Output "Task '$($record.id)' is ready for lead-agent review at $head."
    }
}

function Acquire-Leader {
    param(
        [Parameter(Mandatory = $true)]$Context,
        [Parameter(Mandatory = $true)]$Config,
        [Parameter(Mandatory = $true)][string]$AgentName,
        [Parameter(Mandatory = $true)][string]$SessionId
    )
    Use-ManagerLock -Context $Context -Action {
        $manifest = Read-Manifest -Context $Context
        $now = [DateTimeOffset]::Now
        if ($null -ne $manifest.leader) {
            $expiresAt = [DateTimeOffset]::Parse($manifest.leader.expiresAt)
            $sameSession = (
                $manifest.leader.agent -eq $AgentName -and
                $manifest.leader.session -eq $SessionId
            )
            if ($expiresAt -gt $now -and -not $sameSession) {
                throw "Leader lease is held by '$($manifest.leader.agent)' session '$($manifest.leader.session)' until $expiresAt."
            }
        }
        $leader = [pscustomobject]@{
            agent = $AgentName
            session = $SessionId
            acquiredAt = Get-IsoTimestamp
            expiresAt = $now.AddMinutes([int]$Config.leaderLeaseMinutes).ToString("o")
        }
        $manifest.leader = $leader
        Write-Manifest -Context $Context -Manifest $manifest
        Add-EventSafely -Context $Context -Type "leader.acquired" -Data $leader
        $leader | Format-List
    }
}

function Get-PendingSourceCommits {
    param(
        [Parameter(Mandatory = $true)][string]$WorktreePath,
        [Parameter(Mandatory = $true)][string]$PrimaryBranch
    )
    $cherry = Invoke-Git -WorkingDirectory $WorktreePath -Arguments @(
        "cherry", $PrimaryBranch, "HEAD"
    ) -ReadOnly
    return @($cherry.Lines | Where-Object { $_ -like "+ *" } | ForEach-Object {
        $_.Substring(2)
    })
}

function Integrate-Task {
    param(
        [Parameter(Mandatory = $true)]$Context,
        [Parameter(Mandatory = $true)]$Config,
        [Parameter(Mandatory = $true)][string]$Identifier,
        [string]$LeaderSessionId
    )
    Use-ManagerLock -Context $Context -Action {
        $manifest = Read-Manifest -Context $Context
        Assert-LeaderAuthorization -Config $Config -Manifest $manifest -SessionId $LeaderSessionId
        $record = Get-TaskRecord -Manifest $manifest -Identifier $Identifier
        $primary = Get-PrimaryWorktree -Context $Context -Config $Config
        Assert-CleanWorktree -Path $primary.Path -Purpose "Integration"
        Assert-CleanWorktree -Path $record.worktreePath -Purpose "Integration"
        Assert-ExpectedBranch -Path $primary.Path -ExpectedBranch $Config.primaryBranch `
            -Purpose "Integration"
        Assert-ExpectedBranch -Path $record.worktreePath -ExpectedBranch $record.branch `
            -Purpose "Integration"
        Assert-LinearTaskHistory -Record $record -Purpose "Integration"

        if ($record.status -ne "ready") {
            throw "Task must be handed off with validation evidence before integration. Current status: $($record.status)"
        }
        $currentHead = (Invoke-Git -WorkingDirectory $record.worktreePath -Arguments @(
            "rev-parse", "HEAD"
        ) -ReadOnly).Lines[0]
        if ($record.handoffHead -ne $currentHead) {
            throw "Task HEAD changed after handoff. Run handoff again after validation."
        }
        if ([string]::IsNullOrWhiteSpace($record.validationSummary)) {
            throw "Validation evidence is required before integration."
        }

        $sourceCommits = @(Get-PendingSourceCommits `
            -WorktreePath $record.worktreePath `
            -PrimaryBranch $Config.primaryBranch)
        if ($sourceCommits.Count -eq 0) {
            $record.status = "integrated"
            $record.integratedAt = Get-IsoTimestamp
            Write-Manifest -Context $Context -Manifest $manifest
            Add-EventSafely -Context $Context -Type "task.already-integrated" -Data ([ordered]@{
                taskId = $record.id
                sourceHead = $currentHead
            })
            Write-Output "No pending patches. Task marked integrated; no commit was created."
            return
        }

        $primaryStart = (Invoke-Git -WorkingDirectory $primary.Path -Arguments @(
            "rev-parse", $Config.primaryBranch
        ) -ReadOnly).Lines[0]
        $configuredRoot = $Config.worktreeRoot
        if ([System.IO.Path]::IsPathRooted($configuredRoot)) {
            $worktreeRoot = Get-NormalizedPath $configuredRoot
        } else {
            $worktreeRoot = Get-NormalizedPath (Join-Path $primary.Path $configuredRoot)
        }
        $stagingPath = Join-Path $worktreeRoot (
            ".integration-{0}-{1}" -f $record.id, [Guid]::NewGuid().ToString("N").Substring(0, 6)
        )
        if (Test-Path -LiteralPath $stagingPath) {
            throw "Integration staging path unexpectedly exists: $stagingPath"
        }

        [System.IO.Directory]::CreateDirectory($worktreeRoot) | Out-Null
        Invoke-Git -WorkingDirectory $primary.Path -Arguments @(
            "worktree", "add", "--detach", $stagingPath, $primaryStart
        ) | Out-Null
        $preflightSucceeded = $false
        try {
            Invoke-Git -WorkingDirectory $stagingPath -Arguments (
                @("cherry-pick") + $sourceCommits
            ) | Out-Null
            $preflightSucceeded = $true
        } finally {
            if (-not $preflightSucceeded) {
                Invoke-Git -WorkingDirectory $stagingPath -Arguments @(
                    "cherry-pick", "--abort"
                ) -AllowFailure | Out-Null
            }
            $stagingStatus = Invoke-Git -WorkingDirectory $stagingPath -Arguments @(
                "status", "--porcelain", "--untracked-files=normal"
            ) -ReadOnly -AllowFailure
            if ($stagingStatus.ExitCode -eq 0 -and $stagingStatus.Lines.Count -eq 0) {
                Invoke-Git -WorkingDirectory $primary.Path -Arguments @(
                    "worktree", "remove", $stagingPath
                ) -AllowFailure | Out-Null
            }
        }
        if (-not $preflightSucceeded) {
            throw "Cherry-pick preflight failed. The primary worktree was not changed."
        }

        $primaryNow = (Invoke-Git -WorkingDirectory $primary.Path -Arguments @(
            "rev-parse", $Config.primaryBranch
        ) -ReadOnly).Lines[0]
        if ($primaryNow -ne $primaryStart) {
            throw "Primary branch changed during preflight. Integration was not attempted."
        }
        Assert-CleanWorktree -Path $primary.Path -Purpose "Integration"
        Assert-ExpectedBranch -Path $primary.Path -ExpectedBranch $Config.primaryBranch `
            -Purpose "Integration"
        $taskHeadAfterPreflight = (Invoke-Git -WorkingDirectory $record.worktreePath -Arguments @(
            "rev-parse", "HEAD"
        ) -ReadOnly).Lines[0]
        if ($taskHeadAfterPreflight -ne $currentHead) {
            throw "Task HEAD changed during preflight. Integration was not attempted."
        }

        $intentPath = New-TransactionIntent -Context $Context -Operation "integrate" -Data ([ordered]@{
            taskId = $record.id
            primaryStart = $primaryStart
            sourceCommits = $sourceCommits
            taskHead = $currentHead
        })
        try {
            Invoke-Git -WorkingDirectory $primary.Path -Arguments (
                @("cherry-pick") + $sourceCommits
            ) | Out-Null
        } catch {
            $abort = Invoke-Git -WorkingDirectory $primary.Path -Arguments @(
                "cherry-pick", "--abort"
            ) -AllowFailure
            $headAfterAbort = (Invoke-Git -WorkingDirectory $primary.Path -Arguments @(
                "rev-parse", "HEAD"
            ) -ReadOnly -AllowFailure)
            $statusAfterAbort = Invoke-Git -WorkingDirectory $primary.Path -Arguments @(
                "status", "--porcelain", "--untracked-files=normal"
            ) -ReadOnly -AllowFailure
            $rollbackVerified = (
                $abort.ExitCode -eq 0 -and
                $headAfterAbort.ExitCode -eq 0 -and
                $headAfterAbort.Lines.Count -gt 0 -and
                $headAfterAbort.Lines[0] -eq $primaryStart -and
                $statusAfterAbort.ExitCode -eq 0 -and
                $statusAfterAbort.Lines.Count -eq 0
            )
            if ($rollbackVerified) {
                Complete-TransactionIntent -Path $intentPath
                throw "Final cherry-pick failed after preflight; rollback to the clean starting HEAD was verified. $($_.Exception.Message)"
            }
            throw "CRITICAL PARTIAL STATE: final cherry-pick failed and rollback could not be verified. Do not modify master; run reconcile. Transaction: $intentPath. $($_.Exception.Message)"
        }

        $targetCommits = (Invoke-Git -WorkingDirectory $primary.Path -Arguments @(
            "rev-list", "--reverse", "$primaryStart..HEAD"
        ) -ReadOnly).Lines
        if ($targetCommits.Count -ne $sourceCommits.Count) {
            throw "CRITICAL PARTIAL STATE: master changed, but integrated commit count does not match the source count. Do not retry cherry-pick; run reconcile. Transaction: $intentPath."
        }
        $mappings = @()
        for ($index = 0; $index -lt $sourceCommits.Count; $index++) {
            $mapping = [pscustomobject]@{
                taskId = $record.id
                source = $sourceCommits[$index]
                target = $targetCommits[$index]
                integratedAt = Get-IsoTimestamp
            }
            $mappings += $mapping
        }
        $manifest.integrations = @($manifest.integrations) + $mappings
        $record.status = "integrated"
        $record.integratedAt = Get-IsoTimestamp
        $record.lastActivityAt = Get-IsoTimestamp
        try {
            Write-Manifest -Context $Context -Manifest $manifest
        } catch {
            throw "Git integration succeeded, but manifest finalization failed. Do not repeat cherry-pick; run reconcile. Transaction: $intentPath. $($_.Exception.Message)"
        }
        Add-EventSafely -Context $Context -Type "task.integrated" -Data ([ordered]@{
            taskId = $record.id
            mappings = $mappings
        })
        Complete-TransactionIntent -Path $intentPath
        Write-Output "Integrated $($sourceCommits.Count) commit(s) into $($Config.primaryBranch). No push was performed."
        $mappings | Format-Table -AutoSize
    }
}

function Retire-Task {
    param(
        [Parameter(Mandatory = $true)]$Context,
        [Parameter(Mandatory = $true)]$Config,
        [Parameter(Mandatory = $true)][string]$Identifier,
        [string]$LeaderSessionId
    )
    Use-ManagerLock -Context $Context -Action {
        $manifest = Read-Manifest -Context $Context
        Assert-LeaderAuthorization -Config $Config -Manifest $manifest -SessionId $LeaderSessionId
        $record = Get-TaskRecord -Manifest $manifest -Identifier $Identifier
        $primary = Get-PrimaryWorktree -Context $Context -Config $Config
        if (Test-SamePath $record.worktreePath $primary.Path) {
            throw "The primary worktree can never be retired."
        }
        if (-not (Test-Path -LiteralPath $record.worktreePath -PathType Container)) {
            throw "Task worktree is already missing; no removal was attempted."
        }
        Assert-CleanWorktree -Path $primary.Path -Purpose "Retirement"
        Assert-CleanWorktree -Path $record.worktreePath -Purpose "Retirement"
        Assert-ExpectedBranch -Path $primary.Path -ExpectedBranch $Config.primaryBranch `
            -Purpose "Retirement"
        Assert-ExpectedBranch -Path $record.worktreePath -ExpectedBranch $record.branch `
            -Purpose "Retirement"
        Assert-LinearTaskHistory -Record $record -Purpose "Retirement"
        $ignored = Invoke-Git -WorkingDirectory $record.worktreePath -Arguments @(
            "clean", "-n", "-d", "-X"
        ) -ReadOnly
        if ($ignored.Lines.Count -gt 0) {
            throw "Retirement found ignored files or directories. They were preserved; remove or archive them explicitly before retrying."
        }
        $pending = @(Get-PendingSourceCommits `
            -WorktreePath $record.worktreePath `
            -PrimaryBranch $Config.primaryBranch)
        if ($pending.Count -gt 0) {
            throw "Task has $($pending.Count) patch(es) not present on $($Config.primaryBranch). The worktree was preserved."
        }
        if ($record.status -ne "integrated") {
            throw "Task status must be 'integrated' before retirement. Current status: $($record.status)"
        }

        $intentPath = New-TransactionIntent -Context $Context -Operation "retire" -Data ([ordered]@{
            taskId = $record.id
            worktreePath = $record.worktreePath
            retainedBranch = $record.branch
        })
        Invoke-Git -WorkingDirectory $primary.Path -Arguments @(
            "worktree", "remove", $record.worktreePath
        ) | Out-Null
        $record.status = "retired"
        $record.retiredAt = Get-IsoTimestamp
        $record.lastActivityAt = Get-IsoTimestamp
        try {
            Write-Manifest -Context $Context -Manifest $manifest
        } catch {
            throw "Git removed the worktree, but manifest finalization failed. The branch was retained; run reconcile. Transaction: $intentPath. $($_.Exception.Message)"
        }
        Add-EventSafely -Context $Context -Type "task.retired" -Data ([ordered]@{
            taskId = $record.id
            worktreePath = $record.worktreePath
            retainedBranch = $record.branch
        })
        Complete-TransactionIntent -Path $intentPath
        Write-Output "Retired worktree '$($record.worktreePath)'. Branch '$($record.branch)' was retained."
    }
}

$config = Get-Configuration
$context = Get-RepositoryContext
$primaryForContext = Get-PrimaryWorktree -Context $context -Config $config
$context | Add-Member -NotePropertyName PrimaryWorktreeRoot -NotePropertyValue (
    Get-NormalizedPath $primaryForContext.Path
)

switch ($Command) {
    "status" {
        $manifest = Read-Manifest -Context $context
        Show-Status -Context $context -Config $config -Manifest $manifest -AsJson:$Json
    }
    "inventory" {
        $manifest = Read-Manifest -Context $context
        Show-Status -Context $context -Config $config -Manifest $manifest -AsJson:$Json -Inventory
    }
    "reconcile" {
        $transactions = @(Get-PendingTransactionRecords -Context $context)
        if ($Json) {
            $transactions | ConvertTo-Json -Depth 10
        } elseif ($transactions.Count -eq 0) {
            Write-Output "No pending manager transactions."
        } else {
            Write-Output "Pending transactions require lead-agent inspection. No automatic recovery was attempted."
            $transactions | Format-List id, operation, createdAt, path, data, parseError
        }
    }
    "new" {
        if ([string]::IsNullOrWhiteSpace($Task)) { throw "new requires <task>." }
        if ([string]::IsNullOrWhiteSpace($Agent)) { throw "new requires -Agent <agent>." }
        New-TaskWorktree -Context $context -Config $config -TaskName $Task `
            -AgentName $Agent -LeaderSessionId $LeaderSession
    }
    "attach" {
        if ([string]::IsNullOrWhiteSpace($Task)) { throw "attach requires <task>." }
        if ([string]::IsNullOrWhiteSpace($Agent)) { throw "attach requires -Agent <agent>." }
        if ([string]::IsNullOrWhiteSpace($WorktreePath)) {
            throw "attach requires -WorktreePath <absolute-path>."
        }
        Add-LegacyWorktree -Context $context -Config $config -TaskName $Task `
            -AgentName $Agent -Path $WorktreePath -LeaderSessionId $LeaderSession
    }
    "inspect" {
        if ([string]::IsNullOrWhiteSpace($Task)) { throw "inspect requires <task>." }
        $manifest = Read-Manifest -Context $context
        Show-TaskInspection -Context $context -Config $config -Manifest $manifest -Identifier $Task
    }
    "handoff" {
        if ([string]::IsNullOrWhiteSpace($Task)) { throw "handoff requires <task>." }
        if ([string]::IsNullOrWhiteSpace($Agent)) { throw "handoff requires -Agent <agent>." }
        if ([string]::IsNullOrWhiteSpace($ValidationSummary)) {
            throw "handoff requires -ValidationSummary with checks and results."
        }
        Set-TaskHandoff -Context $context -Config $config -Identifier $Task `
            -AgentName $Agent -Summary $ValidationSummary
    }
    "integrate" {
        if ([string]::IsNullOrWhiteSpace($Task)) { throw "integrate requires <task>." }
        Integrate-Task -Context $context -Config $config -Identifier $Task `
            -LeaderSessionId $LeaderSession
    }
    "retire" {
        if ([string]::IsNullOrWhiteSpace($Task)) { throw "retire requires <task>." }
        Retire-Task -Context $context -Config $config -Identifier $Task `
            -LeaderSessionId $LeaderSession
    }
    "leader" {
        if ([string]::IsNullOrWhiteSpace($Agent)) { throw "leader requires -Agent <agent>." }
        if ([string]::IsNullOrWhiteSpace($Session)) { throw "leader requires -Session <session-id>." }
        Acquire-Leader -Context $context -Config $config -AgentName $Agent -SessionId $Session
    }
}
