param(
    [Parameter(Mandatory = $true)]
    [string]$CondaExecutable,
    [Parameter(Mandatory = $true)]
    [string]$Destination
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$lockPath = Join-Path $root "environment-release-win-64.lock"
$cache = Join-Path $root "build\conda-package-cache"
$localLock = Join-Path $cache "verified-local-runtime.lock"
New-Item -ItemType Directory -Path $cache -Force | Out-Null

$localEntries = [System.Collections.Generic.List[string]]::new()
$localEntries.Add("@EXPLICIT")
foreach ($line in Get-Content -LiteralPath $lockPath) {
    $entry = $line.Trim()
    if (-not $entry -or $entry.StartsWith("#") -or $entry -eq "@EXPLICIT") {
        continue
    }
    if ($entry -notmatch "^(https://repo\.anaconda\.com/.+\.conda)#([0-9a-f]{64})$") {
        throw "Invalid SHA-256 Conda lock entry: $entry"
    }
    $url = $Matches[1]
    $expectedSha256 = $Matches[2]
    $decodedName = [Uri]::UnescapeDataString(([Uri]$url).Segments[-1])
    $packageName = [IO.Path]::GetFileName($decodedName)
    if ($packageName -ne $decodedName -or $packageName -notmatch "^[A-Za-z0-9._-]+\.conda$") {
        throw "Unsafe Conda package name in lock: $decodedName"
    }
    $packagePath = Join-Path $cache $packageName
    $needsDownload = -not (Test-Path -LiteralPath $packagePath)
    if (-not $needsDownload) {
        $cachedSha256 = (Get-FileHash -LiteralPath $packagePath -Algorithm SHA256).Hash.ToLowerInvariant()
        $needsDownload = $cachedSha256 -ne $expectedSha256
    }
    if ($needsDownload) {
        Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $packagePath
    }
    $actualSha256 = (Get-FileHash -LiteralPath $packagePath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualSha256 -ne $expectedSha256) {
        throw "Conda package checksum mismatch for $($packageName): $actualSha256"
    }
    $md5 = (Get-FileHash -LiteralPath $packagePath -Algorithm MD5).Hash.ToLowerInvariant()
    $localEntries.Add("$(([Uri]$packagePath).AbsoluteUri)#$md5")
}

[IO.File]::WriteAllLines($localLock, $localEntries, [Text.UTF8Encoding]::new($false))
& $CondaExecutable create --yes --prefix $Destination --file $localLock
if ($LASTEXITCODE -ne 0) {
    throw "Conda environment creation failed with exit code $LASTEXITCODE"
}
